# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""ChannelData и связанные модели."""
from __future__ import annotations

import threading
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from PyQt6.QtGui import QColor

from constants import StatusText
from logging_setup import logger
from name_normalizer import (canonical_key as nn_canonical_key,
    is_stub_tvg_id, normalize_with_settings, NormMode)
from paths import parse_datetime
from theme import semantic_colors
from utils import sanitize_user_agent


_MAX_NAME_LEN = 1024
_MAX_URL_LEN = 8192
_MAX_GROUP_LEN = 512
_MAX_TVG_ID_LEN = 512
_MAX_LOGO_LEN = 2048
_MAX_SHORT_LEN = 256
_MAX_ALT_NAMES = 32
_MAX_ALT_NAME_LEN = 512


def _clip(v: Any, limit: int) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    return s[:limit]


class LinkQuality(Enum):
    UNKNOWN = 0
    WORKING = 1
    NOT_WORKING = 2
    UNSUPPORTED = 3


class SlotsCopyMixin:
    def copy(self):
        new = type(self)()
        for s in self.__slots__:
            v = getattr(self, s)
            if isinstance(v, (list, dict)):
                v = v.copy()
            setattr(new, s, v)
        return new


class ChannelMetadata(SlotsCopyMixin):
    __slots__ = (
        'name', 'original_name', 'group', 'tvg_id', 'tvg_name',
        'tvg_logo', 'region', 'quality',
        'tvg_shift', 'timeshift', 'catchup', 'catchup_source',
        'catchup_days', 'tvg_rec', 'tvg_chno', 'audio_track',
        'epg_source', 'epg_alternative_names',
        'preferred_audio_lang', 'preferred_video_lang',
    )

    def __init__(self):
        self.name: str = ""
        self.original_name: str = ""
        self.group: str = ""
        self.tvg_id: str = ""
        self.tvg_name: str = ""
        self.tvg_logo: str = ""
        self.region: str = ""
        self.quality: int = 0
        self.tvg_shift: str = ""
        self.timeshift: str = ""
        self.catchup: str = ""
        self.catchup_source: str = ""
        self.catchup_days: str = ""
        self.tvg_rec: str = ""
        self.tvg_chno: str = ""
        self.audio_track: str = ""
        self.epg_source: str = ""
        self.epg_alternative_names: List[str] = []
        self.preferred_audio_lang: str = ""
        self.preferred_video_lang: str = ""


class ChannelLink(SlotsCopyMixin):
    __slots__ = ('url', 'extinf', 'user_agent', 'extvlcopt_lines',
                 'extra_headers', 'has_url', 'alternative_urls',
                 'link_source')

    def __init__(self):
        self.url: str = ""
        self.extinf: str = ""
        self.user_agent: str = ""
        self.extvlcopt_lines: list = []
        self.extra_headers: Dict[str, str] = {}
        self.has_url: bool = True
        self.alternative_urls: list = []
        self.link_source: str = ""


class ChannelStatus(SlotsCopyMixin):
    __slots__ = ('url_status', 'url_check_time', 'link_quality',
                 'link_response_time', 'status_text', 'status_code')

    def __init__(self):
        self.url_status: Optional[bool] = None
        self.url_check_time: Optional[datetime] = None
        self.link_quality: LinkQuality = LinkQuality.UNKNOWN
        self.link_response_time: Optional[float] = None
        self.status_text: str = StatusText.UNCHECKED
        self.status_code: Optional[int] = None

    def reset(self):
        self.url_status = None
        self.url_check_time = None
        self.link_quality = LinkQuality.UNKNOWN
        self.link_response_time = None
        self.status_text = StatusText.UNCHECKED
        self.status_code = None


class ChannelData:
    _uid_next: int = 1
    _uid_lock = threading.Lock()

    __slots__ = ('uid', 'meta', 'link', 'status',
                 'modified_date', 'original_index',
                 '__weakref__')

    def __init__(self, uid: Optional[int] = None):
        if uid is not None:
            self.uid = uid
            ChannelData._reserve_uid(uid)
        else:
            self.uid = ChannelData._next_uid()
        self.meta = ChannelMetadata()
        self.link = ChannelLink()
        self.status = ChannelStatus()
        self.modified_date = datetime.now()
        self.original_index = -1

    @classmethod
    def _next_uid(cls) -> int:
        with cls._uid_lock:
            uid = cls._uid_next
            cls._uid_next += 1
            return uid

    @classmethod
    def _reserve_uid(cls, uid: int) -> None:
        with cls._uid_lock:
            if uid >= cls._uid_next:
                cls._uid_next = uid + 1

    @property
    def name(self) -> str:
        return self.meta.name

    @name.setter
    def name(self, value: str):
        self.meta.name = value

    @property
    def url(self) -> str:
        return self.link.url

    @url.setter
    def url(self, value: str):
        self.link.url = value
        self.link.has_url = bool(value)

    @property
    def group(self) -> str:
        return self.meta.group

    @group.setter
    def group(self, value: str):
        self.meta.group = value

    @property
    def tvg_id(self) -> str:
        return self.meta.tvg_id

    @tvg_id.setter
    def tvg_id(self, value: str):
        self.meta.tvg_id = value

    @property
    def user_agent(self) -> str:
        return self.link.user_agent

    @user_agent.setter
    def user_agent(self, value: str):
        self.link.user_agent = value

    def touch(self):
        self.modified_date = datetime.now()

    @property
    def has_valid_url(self) -> bool:
        return bool(self.link.url and self.link.url.strip())

    def clear_url(self):
        self.link.url = ""
        self.link.has_url = False
        self.status.reset()
        self.touch()

    def normalized_name(self, settings=None) -> str:
        return nn_canonical_key(
            self.meta.name or "", settings,
            tvg_id=self.meta.tvg_id, keep_shift=False)

    def stable_key(self, settings=None) -> str:
        if self.meta.tvg_id and not is_stub_tvg_id(self.meta.tvg_id):
            return f"tvg:{self.meta.tvg_id.strip().lower()}"
        return f"name:{self.normalized_name(settings)}"

    def search_keys(self, settings=None,
                    epg_db=None) -> Set[str]:
        keys: Set[str] = set()

        def _add(raw: str) -> None:
            if not raw or not raw.strip():
                return
            norm = normalize_with_settings(
                raw, settings, NormMode.FULL)
            if norm:
                keys.add(norm)

        _add(self.meta.name or "")
        _add(self.meta.tvg_name or "")
        for alt in (self.meta.epg_alternative_names or []):
            _add(alt)

        if epg_db is not None:
            try:
                if getattr(epg_db, 'has_channel_info', False):
                    info = epg_db.find_channel_info(
                        self, settings=settings)
                    if info is not None:
                        _add(info.display_name or "")
                        for alt in (info.alternative_names or []):
                            _add(alt)
            except Exception:
                logger.exception("search_keys: epg_db")

        if self.meta.tvg_id and not is_stub_tvg_id(
                self.meta.tvg_id):
            keys.add(f"tvg:{self.meta.tvg_id.strip().lower()}")
        return keys

    def needs_replacement(self, settings) -> bool:
        if not self.has_valid_url:
            return bool(settings.auto_replace_missing)
        if self.status.url_status is False:
            return bool(settings.auto_replace_broken)
        return False

    def copy(self) -> 'ChannelData':
        c = ChannelData()
        c.meta = self.meta.copy()
        c.link = self.link.copy()
        c.status = self.status.copy()
        c.modified_date = self.modified_date
        c.original_index = self.original_index
        return c

    @staticmethod
    def _escape(v: str) -> str:
        if not v:
            return ""
        s = (str(v).replace('\r', ' ').replace('\n', ' ')
             .replace('\t', ' '))
        return s.replace('\\', '\\\\').replace('"', '\\"')

    def update_extinf(self):
        parts = ["#EXTINF:-1"]
        if self.meta.tvg_id:
            parts.append(f'tvg-id="{self._escape(self.meta.tvg_id)}"')
        if self.meta.tvg_name:
            parts.append(
                f'tvg-name="{self._escape(self.meta.tvg_name)}"')
        if self.meta.tvg_logo:
            parts.append(
                f'tvg-logo="{self._escape(self.meta.tvg_logo)}"')
        from constants import DEFAULT_GROUP
        if self.meta.group and self.meta.group != DEFAULT_GROUP:
            parts.append(
                f'group-title="{self._escape(self.meta.group)}"')
        for fld in ('tvg_shift', 'timeshift', 'catchup',
                    'catchup_source', 'catchup_days', 'tvg_rec',
                    'tvg_chno', 'audio_track'):
            v = getattr(self.meta, fld)
            if v:
                attr = fld.replace('_', '-')
                parts.append(f'{attr}="{self._escape(v)}"')
        parts.append(f',{self.meta.name}')
        self.link.extinf = ' '.join(parts)

    def parse_extvlcopt_headers(self):
        self.link.extra_headers = {}
        self.link.user_agent = ""
        for line in self.link.extvlcopt_lines:
            if not line or '=' not in line:
                continue
            if line.startswith('#EXTVLCOPT:http-user-agent='):
                ua = line[len(
                    '#EXTVLCOPT:http-user-agent='):].strip('"')
                self.link.extra_headers['User-Agent'] = ua
                self.link.user_agent = ua
            elif (line.startswith('#EXTVLCOPT:http-referrer=')
                  or line.startswith('#EXTVLCOPT:http-referer=')):
                prefix = ('#EXTVLCOPT:http-referrer='
                          if line.startswith(
                              '#EXTVLCOPT:http-referrer=')
                          else '#EXTVLCOPT:http-referer=')
                self.link.extra_headers['Referer'] = \
                    line[len(prefix):].strip('"')
            elif line.startswith('#EXTVLCOPT:http-header='):
                hl = line[len(
                    '#EXTVLCOPT:http-header='):].strip('"')
                if ':' in hl:
                    k, v = hl.split(':', 1)
                    self.link.extra_headers[k.strip()] = v.strip()

    def update_extvlcopt_from_headers(self):
        self.link.extvlcopt_lines = []
        if self.link.user_agent:
            ua = self._escape(self.link.user_agent)
            self.link.extvlcopt_lines.append(
                f'#EXTVLCOPT:http-user-agent="{ua}"')
        for k, v in self.link.extra_headers.items():
            if k.lower() == 'user-agent':
                continue
            ev = self._escape(v)
            if k.lower() == 'referer':
                self.link.extvlcopt_lines.append(
                    f'#EXTVLCOPT:http-referrer="{ev}"')
            else:
                self.link.extvlcopt_lines.append(
                    f'#EXTVLCOPT:http-header="{k}: {ev}"')

    def get_url_foreground(self) -> QColor:
        colors = semantic_colors()
        if not self.has_valid_url:
            return colors['no_url']
        lq = self.status.link_quality
        if lq == LinkQuality.WORKING:
            return colors['working']
        if lq == LinkQuality.NOT_WORKING:
            return colors['not_working']
        if lq == LinkQuality.UNSUPPORTED:
            return colors['unsupported']
        if self.status.url_status is True:
            return colors['working']
        if self.status.url_status is False:
            return colors['not_working']
        return colors['unchecked']

    def get_status_text(self) -> str:
        if not self.has_valid_url:
            return StatusText.NO_URL
        lq = self.status.link_quality
        if lq == LinkQuality.UNSUPPORTED:
            return StatusText.UNSUPPORTED
        if lq == LinkQuality.WORKING:
            return StatusText.WORKING
        if lq == LinkQuality.NOT_WORKING:
            return StatusText.NOT_WORKING
        if (self.status.status_text
                and self.status.status_text != StatusText.UNCHECKED):
            return self.status.status_text
        return StatusText.UNCHECKED

    def get_status_tooltip(self) -> str:
        t = f"Канал: {self.meta.name}\n"
        if (self.meta.original_name
                and self.meta.original_name != self.meta.name):
            t += f"Исходное имя: {self.meta.original_name}\n"
        if self.meta.epg_alternative_names:
            alt = self.meta.epg_alternative_names[:5]
            t += f"EPG альтернативы: {', '.join(alt)}\n"
        t += f"Группа: {self.meta.group}\n"
        if self.meta.tvg_id:
            t += f"TVG-ID: {self.meta.tvg_id}\n"
        if self.original_index >= 0:
            t += f"Позиция в оригинале: {self.original_index}\n"
        if self.status.status_code is not None:
            t += f"HTTP-код: {self.status.status_code}\n"
        t += f"Статус: {self.get_status_text()}\n"
        if self.status.link_response_time is not None:
            t += (f"Время ответа: "
                  f"{self.status.link_response_time:.2f} сек\n")
        if self.link.alternative_urls:
            t += (f"Альтернативных ссылок: "
                  f"{len(self.link.alternative_urls)}\n")
        if self.link.user_agent:
            t += f"User-Agent: {self.link.user_agent[:50]}...\n"
        if self.link.link_source:
            t += f"Источник: {self.link.link_source}\n"
        if self.meta.epg_source:
            t += f"EPG-источник: {self.meta.epg_source}\n"
        return t

    def to_dict(self) -> Dict[str, Any]:
        return {
            'uid': self.uid,
            **{k: getattr(self.meta, k)
               for k in ChannelMetadata.__slots__},
            'url': self.link.url,
            'extinf': self.link.extinf,
            'user_agent': self.link.user_agent,
            'extvlcopt_lines': list(self.link.extvlcopt_lines),
            'extra_headers': dict(self.link.extra_headers),
            'has_url': self.link.has_url,
            'alternative_urls': list(self.link.alternative_urls),
            'link_source': self.link.link_source,
            'url_status': self.status.url_status,
            'url_check_time': (
                self.status.url_check_time.isoformat()
                if self.status.url_check_time else None),
            'link_quality': self.status.link_quality.value,
            'link_response_time': self.status.link_response_time,
            'status_text': self.status.status_text,
            'status_code': self.status.status_code,
            'modified_date': self.modified_date.isoformat(),
            'original_index': self.original_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChannelData':
        uid_raw = data.get('uid')
        uid: Optional[int] = None
        if isinstance(uid_raw, int):
            uid = uid_raw
        elif isinstance(uid_raw, str):
            try:
                uid = int(uid_raw)
            except (ValueError, TypeError):
                uid = None
        c = cls(uid=uid)
        c.restore_from_dict(data)
        return c

    def restore_from_dict(self, data: Dict[str, Any]):
        from constants import DEFAULT_GROUP
        for k in ChannelMetadata.__slots__:
            v = data.get(k)
            if k == 'epg_alternative_names':
                raw = v or []
                if not isinstance(raw, list):
                    raw = []
                self.meta.epg_alternative_names = [
                    _clip(x, _MAX_ALT_NAME_LEN)
                    for x in raw[:_MAX_ALT_NAMES] if x
                ]
                continue
            if v is None:
                if k == 'group':
                    self.meta.group = DEFAULT_GROUP
                elif k == 'quality':
                    self.meta.quality = 0
                else:
                    setattr(self.meta, k, "")
                continue
            if k == 'quality':
                try:
                    self.meta.quality = int(v)
                except (ValueError, TypeError):
                    self.meta.quality = 0
                continue
            if k in ('name', 'original_name', 'tvg_name'):
                setattr(self.meta, k, _clip(v, _MAX_NAME_LEN))
            elif k == 'group':
                setattr(self.meta, k, _clip(v, _MAX_GROUP_LEN))
            elif k == 'tvg_id':
                setattr(self.meta, k, _clip(v, _MAX_TVG_ID_LEN))
            elif k in ('tvg_logo', 'epg_source'):
                setattr(self.meta, k, _clip(v, _MAX_LOGO_LEN))
            else:
                setattr(self.meta, k, _clip(v, _MAX_SHORT_LEN))

        self.link.url = _clip(data.get('url', '') or '', _MAX_URL_LEN)
        self.link.extinf = _clip(
            data.get('extinf', '') or '', _MAX_NAME_LEN)
        self.link.user_agent = sanitize_user_agent(
            data.get('user_agent', '') or '')
        self.link.extvlcopt_lines = list(
            data.get('extvlcopt_lines', []) or [])
        self.link.extra_headers = dict(
            data.get('extra_headers', {}) or {})
        self.link.has_url = bool(data.get('has_url', True))
        alt_raw = data.get('alternative_urls', []) or []
        self.link.alternative_urls = [
            _clip(u, _MAX_URL_LEN) for u in alt_raw if u
        ]
        self.link.link_source = _clip(
            data.get('link_source', '') or '', _MAX_SHORT_LEN)

        self.status.url_status = data.get('url_status')
        st = data.get('status_text', StatusText.UNCHECKED)
        # Валидация: если status_text не из известного набора
        # StatusText — сбрасываем в UNCHECKED.
        _valid = {
            StatusText.UNCHECKED, StatusText.NO_URL,
            StatusText.WORKING, StatusText.NOT_WORKING,
            StatusText.UNSUPPORTED, StatusText.TIMEOUT,
            StatusText.DNS_FAIL, StatusText.CONN_ERROR,
            StatusText.GEOBLOCK, StatusText.NOT_FOUND,
            StatusText.CANCELLED, StatusText.BLOCKED_BY_DOMAIN,
            StatusText.EMPTY_RESPONSE,
        }
        if not isinstance(st, str) or st not in _valid:
            st = StatusText.UNCHECKED
        self.status.status_text = st
        sc = data.get('status_code')
        if sc is not None:
            try:
                self.status.status_code = int(sc)
            except (ValueError, TypeError):
                self.status.status_code = None
        rt = data.get('link_response_time')
        if rt is not None:
            try:
                self.status.link_response_time = float(rt)
            except (ValueError, TypeError):
                pass
        for fld in ('url_check_time', 'modified_date'):
            v = data.get(fld)
            if v:
                dt = parse_datetime(v)
                if dt is not None:
                    if fld == 'url_check_time':
                        self.status.url_check_time = dt
                    else:
                        self.modified_date = dt
        try:
            self.status.link_quality = LinkQuality(
                int(data.get('link_quality', 0)))
        except (ValueError, TypeError):
            pass
        try:
            self.original_index = int(data.get('original_index', -1))
        except (ValueError, TypeError):
            pass


class EPGEntry:
    __slots__ = ('channel_id', 'start', 'stop', 'title', 'desc',
                 'category')

    def __init__(self):
        self.channel_id: str = ""
        self.start: Optional[datetime] = None
        self.stop: Optional[datetime] = None
        self.title: str = ""
        self.desc: str = ""
        self.category: str = ""


class EPGChannelInfo:
    __slots__ = ('channel_id', 'display_name', 'display_names',
                 'display_names_lang', 'alternative_names',
                 'icon', 'lcn', 'source', 'all_sources', 'priority')

    def __init__(self):
        self.channel_id: str = ""
        self.display_name: str = ""
        self.display_names: List[str] = []
        self.display_names_lang: Dict[str, str] = {}
        self.alternative_names: List[str] = []
        self.icon: str = ""
        self.lcn: str = ""
        self.source: str = ""
        self.all_sources: List[str] = []
        self.priority: int = 0

    def update_canonical(self, canonical: str):
        self.display_name = canonical
        self.alternative_names = [
            n for n in self.display_names
            if n and n != canonical]

    def copy(self) -> 'EPGChannelInfo':
        c = EPGChannelInfo()
        c.channel_id = self.channel_id
        c.display_name = self.display_name
        c.display_names = list(self.display_names)
        c.display_names_lang = dict(self.display_names_lang)
        c.alternative_names = list(self.alternative_names)
        c.icon = self.icon
        c.lcn = self.lcn
        c.source = self.source
        c.all_sources = list(self.all_sources)
        c.priority = self.priority
        return c