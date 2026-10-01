# -*- coding: utf-8 -*-
"""M3UParser, PlaylistHeaderManager."""

from __future__ import annotations
import re
from typing import List, Dict, Optional
from .constants import DEFAULT_GROUP
from .models import ChannelData
from .utils import ChannelNameNormalizer


class M3UParser:
    _ATTR_RE = re.compile(
        r'(tvg-id|tvg-name|tvg-logo|group-title|tvg-country|tvg-language|'
        r'tvg-shift|timeshift|catchup|catchup-source|catchup-days|'
        r'tvg-rec|tvg-chno|audio-track)\s*=\s*'
        r'(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|([^\s,]+))'
    )

    _EXTINF_CLEAN_RE = re.compile(
        r'(?:tvg-id|tvg-name|tvg-logo|group-title|tvg-country|tvg-language|'
        r'tvg-shift|timeshift|catchup|catchup-source|catchup-days|'
        r'tvg-rec|tvg-chno|audio-track)\s*=\s*'
        r'(?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,]+)\s*'
    )

    @classmethod
    def extract_name(cls, line: str) -> str:
        if ',' not in line:
            return ""
        clean = cls._EXTINF_CLEAN_RE.sub('', line)
        parts = clean.split(',', 1)
        return parts[1].strip() if len(parts) > 1 else ""

    @classmethod
    def parse_attrs(cls, line: str) -> Dict[str, str]:
        attrs = {}
        for m in cls._ATTR_RE.finditer(line):
            key = m.group(1)
            v = m.group(2) or m.group(3) or m.group(4) or ''
            v = v.replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
            attrs[key] = v
        return attrs

    @classmethod
    def _build_channel_from_extinf(cls, extinf_line: str,
                                   source_name: str) -> ChannelData:
        channel = ChannelData()
        channel.link.extinf = ChannelNameNormalizer.fix_encoding(extinf_line)
        channel.link.link_source = source_name
        attrs = cls.parse_attrs(channel.link.extinf)
        channel.meta.tvg_id = ChannelNameNormalizer.fix_encoding(attrs.get('tvg-id', ''))
        channel.meta.tvg_name = ChannelNameNormalizer.fix_encoding(attrs.get('tvg-name', ''))
        channel.meta.tvg_logo = ChannelNameNormalizer.fix_encoding(attrs.get('tvg-logo', ''))
        channel.meta.tvg_shift = ChannelNameNormalizer.fix_encoding(attrs.get('tvg-shift', ''))
        channel.meta.timeshift = ChannelNameNormalizer.fix_encoding(attrs.get('timeshift', ''))
        channel.meta.catchup = ChannelNameNormalizer.fix_encoding(attrs.get('catchup', ''))
        channel.meta.catchup_source = ChannelNameNormalizer.fix_encoding(
            attrs.get('catchup-source', ''))
        channel.meta.catchup_days = ChannelNameNormalizer.fix_encoding(
            attrs.get('catchup-days', ''))
        channel.meta.tvg_rec = ChannelNameNormalizer.fix_encoding(attrs.get('tvg-rec', ''))
        channel.meta.tvg_chno = ChannelNameNormalizer.fix_encoding(attrs.get('tvg-chno', ''))
        channel.meta.audio_track = ChannelNameNormalizer.fix_encoding(
            attrs.get('audio-track', ''))
        grp_attr = ChannelNameNormalizer.fix_encoding(attrs.get('group-title', ''))
        if grp_attr:
            channel.meta.group = grp_attr

        raw = cls.extract_name(channel.link.extinf)
        if raw:
            channel.meta.original_name = ChannelNameNormalizer.fix_encoding(raw)
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

    @classmethod
    def parse(cls, content: str, source_name: str = "",
              assign_original_index: bool = True) -> List[ChannelData]:
        channels: List[ChannelData] = []
        lines = content.splitlines()
        i = 0
        n = len(lines)
        pending_extgrp: Optional[str] = None
        pending_vlcopts: List[str] = []
        parse_index = 0

        while i < n:
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if line.startswith('#EXTGRP:'):
                grp = ChannelNameNormalizer.fix_encoding(
                    line[len('#EXTGRP:'):].strip())
                if grp:
                    pending_extgrp = grp
                i += 1
                continue
            if line.startswith('#EXTVLCOPT:'):
                pending_vlcopts.append(ChannelNameNormalizer.fix_encoding(line))
                i += 1
                continue
            if not line.startswith('#EXTINF:'):
                i += 1
                continue

            channel = cls._build_channel_from_extinf(line, source_name)
            if pending_extgrp and (not channel.meta.group
                                   or channel.meta.group == DEFAULT_GROUP):
                channel.meta.group = pending_extgrp
            pending_extgrp = None

            local_vlcopts = list(pending_vlcopts)
            pending_vlcopts = []

            i += 1
            while i < n:
                nl = lines[i].strip()
                if not nl:
                    i += 1
                    continue
                if nl.startswith('#EXTINF:'):
                    break
                if nl.startswith('#EXTVLCOPT:'):
                    local_vlcopts.append(
                        ChannelNameNormalizer.fix_encoding(nl))
                    i += 1
                    continue
                if nl.startswith('#EXTGRP:'):
                    g = nl[len('#EXTGRP:'):].strip()
                    if g and (not channel.meta.group
                              or channel.meta.group == DEFAULT_GROUP):
                        channel.meta.group = ChannelNameNormalizer.fix_encoding(g)
                    i += 1
                    continue
                if nl.startswith('#'):
                    i += 1
                    continue
                # v0.9.5 fix: URL не прогоняем через fix_encoding —
                # процентное кодирование и UTF-8 в query могут
                # пострадать. fix_encoding оставляем только для
                # строк, не начинающихся со схемы.
                if nl.startswith(('http://', 'https://', 'rtmp://',
                                   'rtsp://', 'udp://', 'tcp://',
                                   'rtp://', 'srt://', 'rist://')):
                    channel.link.url = nl
                else:
                    channel.link.url = ChannelNameNormalizer.fix_encoding(nl)
                channel.link.has_url = True
                i += 1
                break

            if channel.link.url:
                channel.link.extvlcopt_lines.extend(local_vlcopts)
            else:
                pending_vlcopts = pending_vlcopts + local_vlcopts
                channel.link.has_url = False

            channel.parse_extvlcopt_headers()
            if channel.meta.name or channel.link.url or channel.link.extinf:
                if not channel.meta.name:
                    channel.meta.name = "(без имени)"
                if assign_original_index:
                    object.__setattr__(channel, 'original_index', parse_index)
                    parse_index += 1
                channels.append(channel)

        return channels


class PlaylistHeaderManager:
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
                    attrs = re.findall(
                        r'(\S+?)\s*=\s*["\']?([^"\'\s]+)["\']?', attrs_line)
                    for k, v in attrs:
                        if k.lower() == 'url-tvg':
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
        return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')

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
        epg_sources = list(self.epg_sources)
        if epg_sources:
            escaped = ",".join(self._escape_attr(x) for x in epg_sources)
            parts.append(f'url-tvg="{escaped}"')
        for k, v in self.custom_attributes.items():
            parts.append(f'{self._escape_attr(k)}="{self._escape_attr(v)}"')
        for k, v in self._original_attrs.items():
            if k not in self.custom_attributes and k.lower() != 'url-tvg':
                parts.append(f'{self._escape_attr(k)}="{self._escape_attr(v)}"')
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
            self.header_lines.insert(idx + 1, f'#PLAYLIST:{self.playlist_name}')
        else:
            self.header_lines.append(f'#PLAYLIST:{self.playlist_name}')

    def get_header_text(self) -> str:
        if not self.header_lines:
            return "#EXTM3U\n\n"
        return '\n'.join(self.header_lines).rstrip('\n') + '\n\n'
