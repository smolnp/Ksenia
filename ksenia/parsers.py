# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""M3UParser, PlaylistHeaderManager."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Any

from constants import DEFAULT_GROUP, MAX_CHANNELS_PER_PLAYLIST
from logging_setup import logger
from models import ChannelData
from name_normalizer import (ChannelNameNormalizer,
                              normalize_with_settings, NormMode)
from utils import InputValidationError, validate_m3u_lines


_EXTINF_ATTRS = (
    'tvg-id', 'tvg-name', 'tvg-logo', 'group-title', 'tvg-country',
    'tvg-language', 'tvg-shift', 'timeshift', 'catchup',
    'catchup-source', 'catchup-days', 'tvg-rec', 'tvg-chno',
    'audio-track',
)

_ATTR_RE = re.compile(
    r'(?:^|\s)('
    + '|'.join(re.escape(a) for a in _EXTINF_ATTRS)
    + r')\s*=\s*'
    r'(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|([^\s,]+))'
)

_GLUE_RE = re.compile(r'^(-?\d+)(?=[a-zA-Z])')

_PROTOCOLS = ('http://', 'https://', 'rtmp://', 'rtsp://', 'udp://',
              'tcp://', 'rtp://', 'srt://', 'rist://')


def _find_name_comma(line: str) -> int:
    in_quotes = False
    quote_char = ''
    escaped = False
    for i, c in enumerate(line):
        if escaped:
            escaped = False
            continue
        if c == '\\':
            escaped = True
            continue
        if c == '"' or c == "'":
            if not in_quotes:
                in_quotes = True
                quote_char = c
            elif quote_char == c:
                in_quotes = False
        elif c == ',' and not in_quotes:
            return i
    return -1


def _is_valid_url_candidate(url: str) -> bool:
    if not url:
        return False
    u = url.strip()
    if not u:
        return False
    low = u.lower()
    if low.startswith(_PROTOCOLS):
        return True
    if u.startswith('//'):
        return True
    if u.startswith('/'):
        return True
    # Ужесточение: без схемы/слэшей принимаем только длинные
    # строки без пробелов, похожие на домен/путь.
    if len(u) > 10 and ' ' not in u and (
            '/' in u or '.' in u):
        return True
    return False


def _unescape_extinf_name(raw: str) -> str:
    """Снимает экранирование, которое накладывает update_extinf.

    В имени канала (после последней запятой в #EXTINF) мы
    экранируем `\\` → `\\\\` и `"` → `\\"`. При чтении файла
    надо вернуть как было.
    """
    if not raw:
        return ""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == '\\' and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == '\\':
                out.append('\\')
                i += 2
                continue
            if nxt == '"':
                out.append('"')
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


class M3UParser:
    @classmethod
    def extract_name(cls, line: str) -> str:
        comma = _find_name_comma(line)
        if comma < 0:
            return ""
        return _unescape_extinf_name(line[comma + 1:].strip())

    @classmethod
    def parse_attrs(cls, line: str) -> Dict[str, str]:
        comma = _find_name_comma(line)
        if comma > 0:
            line = line[:comma]
        attrs: Dict[str, str] = {}
        for m in _ATTR_RE.finditer(line):
            key = m.group(1)
            v = m.group(2) or m.group(3) or m.group(4) or ''
            v = (v.replace('\\"', '"')
                 .replace("\\'", "'")
                 .replace('\\\\', '\\'))
            attrs[key] = v
        return attrs

    @classmethod
    def _build_channel_from_extinf(cls, extinf_line: str,
                                    source_name: str) -> ChannelData:
        channel = ChannelData()
        channel.link.extinf = ChannelNameNormalizer.fix_encoding(
            extinf_line)
        channel.link.link_source = source_name
        attrs = cls.parse_attrs(channel.link.extinf)
        fix = ChannelNameNormalizer.fix_encoding
        channel.meta.tvg_id = fix(attrs.get('tvg-id', ''))
        channel.meta.tvg_name = fix(attrs.get('tvg-name', ''))
        channel.meta.tvg_logo = fix(attrs.get('tvg-logo', ''))
        channel.meta.tvg_shift = fix(attrs.get('tvg-shift', ''))
        channel.meta.timeshift = fix(attrs.get('timeshift', ''))
        channel.meta.catchup = fix(attrs.get('catchup', ''))
        channel.meta.catchup_source = fix(
            attrs.get('catchup-source', ''))
        channel.meta.catchup_days = fix(
            attrs.get('catchup-days', ''))
        channel.meta.tvg_rec = fix(attrs.get('tvg-rec', ''))
        channel.meta.tvg_chno = fix(attrs.get('tvg-chno', ''))
        channel.meta.audio_track = fix(attrs.get('audio-track', ''))
        grp_attr = fix(attrs.get('group-title', ''))
        if grp_attr:
            channel.meta.group = grp_attr

        raw = cls.extract_name(channel.link.extinf)
        if raw:
            channel.meta.original_name = fix(raw)
            channel.meta.name = channel.meta.original_name

        low = channel.link.extinf.lower()
        if re.search(r'\b(?:4k|uhd|2160)\b', low):
            channel.meta.quality = 4
        elif re.search(r'\b(?:1080|fhd)\b', low):
            channel.meta.quality = 3
        elif re.search(r'\b(?:720|hd)\b', low):
            channel.meta.quality = 2
        elif re.search(r'\b(?:480|sd)\b', low):
            channel.meta.quality = 1
        return channel

    @staticmethod
    def _finalize_channel_without_url(
            pending: ChannelData,
            parse_index: int,
            assign_original_index: bool,
    ) -> int:
        if not pending.meta.name:
            pending.meta.name = "(без имени)"
        pending.link.url = ""
        pending.link.has_url = False
        try:
            pending.update_extinf()
        except Exception:
            logger.exception(
                "update_extinf failed for channel without URL")
        if assign_original_index:
            pending.original_index = parse_index
            parse_index += 1
        return parse_index

    @classmethod
    def parse(cls, content: str, source_name: str = "",
              assign_original_index: bool = True,
              settings: Optional[Any] = None
              ) -> List[ChannelData]:
        validate_m3u_lines(content)

        channels: List[ChannelData] = []
        lines = content.splitlines()
        i = 0
        n = len(lines)

        pending_extgrp: Optional[str] = None
        pending_vlcopts: List[str] = []
        pending_channel: Optional[ChannelData] = None
        parse_index = 0
        fix = ChannelNameNormalizer.fix_encoding

        def _flush_pending():
            nonlocal pending_channel, parse_index
            if pending_channel is None:
                return
            parse_index = cls._finalize_channel_without_url(
                pending_channel, parse_index,
                assign_original_index)
            channels.append(pending_channel)
            pending_channel = None
            # Очищаем EXTVLCOPT, чтобы они не «прилипали» к
            # следующему #EXTINF.
            pending_vlcopts.clear()

        while i < n:
            raw_line = lines[i]
            line = raw_line.strip()

            if not line:
                i += 1
                continue

            if line.startswith('#EXTGRP:'):
                grp = fix(line[len('#EXTGRP:'):].strip())
                if grp:
                    if pending_channel is None:
                        pending_extgrp = grp
                    else:
                        # EXTGRP после #EXTINF — применяем сразу.
                        if (not pending_channel.meta.group
                                or pending_channel.meta.group
                                == DEFAULT_GROUP):
                            pending_channel.meta.group = grp
                i += 1
                continue

            if line.startswith('#EXTVLCOPT:'):
                pending_vlcopts.append(fix(line))
                i += 1
                continue

            if line.startswith('#EXTINF:'):
                if pending_channel is not None:
                    _flush_pending()

                payload = line[len('#EXTINF:'):]
                m = _GLUE_RE.match(payload)
                if m:
                    payload = (payload[:m.end()] + ' '
                               + payload[m.end():])
                    line = '#EXTINF:' + payload

                channel = cls._build_channel_from_extinf(
                    line, source_name)
                if pending_extgrp and (not channel.meta.group
                                        or channel.meta.group
                                        == DEFAULT_GROUP):
                    channel.meta.group = pending_extgrp
                pending_extgrp = None

                channel.link.extvlcopt_lines.extend(pending_vlcopts)
                pending_vlcopts.clear()
                pending_channel = channel
                i += 1
                continue

            if pending_channel is not None:
                url = fix(line)
                if url.startswith('http:\\\\'):
                    url = url.replace('\\\\', '/').replace('\\', '/')
                elif url.startswith('https:\\\\'):
                    url = url.replace('\\\\', '/').replace('\\', '/')

                if _is_valid_url_candidate(url):
                    pending_channel.link.url = url
                    pending_channel.link.has_url = True
                    if not pending_channel.meta.name:
                        pending_channel.meta.name = "(без имени)"
                    if assign_original_index:
                        pending_channel.original_index = parse_index
                        parse_index += 1
                    channels.append(pending_channel)
                    pending_channel = None
                    if len(channels) >= MAX_CHANNELS_PER_PLAYLIST:
                        logger.warning(
                            "M3UParser: reached max channels %d",
                            MAX_CHANNELS_PER_PLAYLIST)
                        break
                else:
                    logger.debug(
                        "M3UParser: non-URL line after "
                        "#EXTINF: %r", url[:80])
            i += 1

        if pending_channel is not None:
            _flush_pending()

        with_url = sum(1 for c in channels if c.has_valid_url)
        logger.info(
            "M3UParser: parsed %d channels from %r "
            "(with_url=%d, without_url=%d)",
            len(channels), source_name, with_url,
            len(channels) - with_url)
        return channels


class PlaylistHeaderManager:
    _ATTR_RE = re.compile(
        r'(\S+?)\s*=\s*'
        r'(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')'
    )

    def __init__(self):
        self.header_lines: List[str] = []
        self.epg_sources: List[str] = []
        self.custom_attributes: Dict[str, str] = {}
        self.playlist_name: str = ""
        self.has_extm3u: bool = False
        self._original_attrs: Dict[str, str] = {}
        self.original_encoding: str = "utf-8"

    def copy(self) -> 'PlaylistHeaderManager':
        h = PlaylistHeaderManager()
        h.header_lines = list(self.header_lines)
        h.epg_sources = list(self.epg_sources)
        h.custom_attributes = dict(self.custom_attributes)
        h.playlist_name = self.playlist_name
        h.has_extm3u = self.has_extm3u
        h._original_attrs = dict(self._original_attrs)
        h.original_encoding = self.original_encoding
        return h

    def parse_header(self, content: str):
        self.header_lines = []
        self.epg_sources = []
        self.custom_attributes = {}
        self._original_attrs = {}
        self.playlist_name = ""
        self.has_extm3u = False

        for line in content.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('#EXTINF:'):
                break
            if line.startswith('#EXTM3U'):
                self.has_extm3u = True
                self.header_lines.append(line)
                if ' ' in line:
                    attrs_line = line[8:]
                    for m in self._ATTR_RE.finditer(attrs_line):
                        k = m.group(1)
                        v = m.group(2) or m.group(3) or ''
                        v = (v.replace('\\"', '"')
                             .replace('\\\\', '\\'))
                        kl = k.lower()
                        if kl in ('url-tvg', 'x-tvg-url'):
                            for part in v.split(','):
                                part = part.strip()
                                if part:
                                    self.epg_sources.append(part)
                        else:
                            self.custom_attributes[k] = v
                            self._original_attrs[k] = v
            elif line.startswith('#PLAYLIST:'):
                self.playlist_name = line[10:]
                self.header_lines.append(line)
            elif line.startswith('#'):
                self.header_lines.append(line)

    @staticmethod
    def _escape_attr(v: str) -> str:
        if v is None:
            return ""
        return (str(v).replace('\\', '\\\\')
                .replace('"', '\\"')
                .replace('\n', ' '))

    def update_epg_sources(self, sources: List[str]):
        self.epg_sources = list(sources)
        self._update_extm3u_line()

    def set_playlist_name(self, name: str):
        self.playlist_name = name
        if name and not self.has_extm3u:
            self._update_extm3u_line()
        self._update_playlist_name_line()

    def add_custom_attribute(self, key: str, value: str):
        self.custom_attributes[key] = value
        self._update_extm3u_line()

    def remove_custom_attribute(self, key: str):
        self.custom_attributes.pop(key, None)
        self._update_extm3u_line()

    def _update_extm3u_line(self):
        self.header_lines = [l for l in self.header_lines
                             if not l.startswith('#EXTM3U')]
        parts = ["#EXTM3U"]
        if self.epg_sources:
            escaped = ",".join(self._escape_attr(x)
                               for x in self.epg_sources)
            parts.append(f'url-tvg="{escaped}"')
        for k, v in self.custom_attributes.items():
            parts.append(
                f'{self._escape_attr(k)}="{self._escape_attr(v)}"')
        for k, v in self._original_attrs.items():
            if (k not in self.custom_attributes
                    and k.lower() not in ('url-tvg', 'x-tvg-url')):
                parts.append(
                    f'{self._escape_attr(k)}="{self._escape_attr(v)}"')
        self.header_lines.insert(0, ' '.join(parts))
        self.has_extm3u = True

    def _update_playlist_name_line(self):
        self.header_lines = [l for l in self.header_lines
                             if not l.startswith('#PLAYLIST:')]
        if not self.playlist_name:
            return
        idx = -1
        for i, line in enumerate(self.header_lines):
            if line.startswith('#EXTM3U'):
                idx = i
                break
        if idx >= 0:
            self.header_lines.insert(
                idx + 1, f'#PLAYLIST:{self.playlist_name}')
        else:
            self.header_lines.append(
                f'#PLAYLIST:{self.playlist_name}')

    def get_header_text(self) -> str:
        if not self.header_lines:
            return "#EXTM3U\n\n"
        return '\n'.join(self.header_lines).rstrip('\n') + '\n\n'