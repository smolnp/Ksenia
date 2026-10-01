# -*- coding: utf-8 -*-
"""ChannelData, Metadata, Link, Status, EPG-модели."""

from __future__ import annotations
import itertools
import hashlib
import json
import threading
from contextlib import suppress
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List
from enum import Enum
from PyQt6.QtGui import QColor
from .constants import (DEFAULT_GROUP, StatusText, URL_FG_COLORS,
    EPG_ALLOWED_META_FIELDS, EPG_FUZZY_MIN_LENGTH_DEFAULT,
    EPG_FUZZY_MIN_GAP_DEFAULT, EPG_FUZZY_CACHE_LIMIT)
from .paths import logger, parse_datetime
from .utils import ChannelNameNormalizer


class LinkQuality(Enum):
    UNKNOWN = 0
    WORKING = 1
    NOT_WORKING = 2
    UNSUPPORTED = 3


class ChannelMetadata:
    __slots__ = ('name', 'original_name', 'group', 'tvg_id', 'tvg_name',
                 'tvg_logo', 'region', 'quality',
                 'tvg_shift', 'timeshift', 'catchup', 'catchup_source',
                 'catchup_days', 'tvg_rec', 'tvg_chno', 'audio_track',
                 'epg_source')

    def __init__(self):
        self.name: str = ""
        self.original_name: str = ""
        self.group: str = DEFAULT_GROUP
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

    def copy(self) -> 'ChannelMetadata':
        m = ChannelMetadata()
        for s in self.__slots__:
            setattr(m, s, getattr(self, s))
        return m


class ChannelLink:
    __slots__ = ('url', 'extinf', 'user_agent', 'extvlcopt_lines',
                 'extra_headers', 'has_url', 'alternative_urls',
                 'link_source')

    def __init__(self):
        self.url: str = ""
        self.extinf: str = ""
        self.user_agent: str = ""
        self.extvlcopt_lines: List[str] = []
        self.extra_headers: Dict[str, str] = {}
        self.has_url: bool = True
        self.alternative_urls: List[str] = []
        self.link_source: str = ""

    def copy(self) -> 'ChannelLink':
        l = ChannelLink()
        for s in self.__slots__:
            v = getattr(self, s)
            if isinstance(v, list):
                v = v.copy()
            elif isinstance(v, dict):
                v = v.copy()
            setattr(l, s, v)
        return l


class ChannelStatus:
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

    def copy(self) -> 'ChannelStatus':
        s = ChannelStatus()
        for sl in self.__slots__:
            setattr(s, sl, getattr(self, sl))
        return s


class ChannelData:
    _uid_counter = itertools.count(1)
    _uid_lock = threading.Lock()
    _uid_max = 0

    _ATTR_MAP: Dict[str, Tuple[str, str]] = {}
    _ATTR_MAP_LOCK = threading.RLock()

    __slots__ = ('uid', 'meta', 'link', 'status',
                 'modified_date', 'original_index',
                 '_initialized', '_cached_hash', '_cached_hash_mod',
                 '_cached_norm_key', '_cached_norm_mod',
                 '__weakref__')

    _INTERNAL = frozenset((
        'meta', 'link', 'status', 'uid', '_initialized',
        'modified_date', 'original_index',
    ))

    @classmethod
    def _build_attr_map(cls):
        with cls._ATTR_MAP_LOCK:
            if cls._ATTR_MAP:
                return
            m = {}
            for f in ChannelMetadata.__slots__:
                m[f] = ('meta', f)
            for f in ChannelLink.__slots__:
                m[f] = ('link', f)
            for f in ChannelStatus.__slots__:
                m[f] = ('status', f)
            cls._ATTR_MAP = m

    @classmethod
    def _next_uid(cls) -> int:
        # v0.9.4 fix: под lock, иначе race с _reserve_uid.
        with cls._uid_lock:
            return next(cls._uid_counter)

    @classmethod
    def _reserve_uid(cls, uid: int):
        with cls._uid_lock:
            if uid >= cls._uid_max:
                cls._uid_max = uid
                cls._uid_counter = itertools.count(uid + 1)

    def __init__(self, uid: Optional[int] = None):
        object.__setattr__(self, '_initialized', False)
        if uid is not None:
            object.__setattr__(self, 'uid', uid)
            ChannelData._reserve_uid(uid)
        else:
            object.__setattr__(self, 'uid', ChannelData._next_uid())
        object.__setattr__(self, 'meta', ChannelMetadata())
        object.__setattr__(self, 'link', ChannelLink())
        object.__setattr__(self, 'status', ChannelStatus())
        object.__setattr__(self, 'modified_date', datetime.now())
        object.__setattr__(self, 'original_index', -1)
        object.__setattr__(self, '_cached_hash', None)
        object.__setattr__(self, '_cached_hash_mod', None)
        object.__setattr__(self, '_cached_norm_key', None)
        object.__setattr__(self, '_cached_norm_mod', None)
        object.__setattr__(self, '_initialized', True)

    def __getattr__(self, name: str):
        # v0.9.4 fix: одна проверка на dunder (было две избыточных).
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        entry = ChannelData._ATTR_MAP.get(name)
        if entry is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}")
        return getattr(object.__getattribute__(self, entry[0]), entry[1])

    def __setattr__(self, name: str, value):
        if name in ChannelData._INTERNAL or name.startswith('_'):
            object.__setattr__(self, name, value)
            return
        entry = ChannelData._ATTR_MAP.get(name)
        if entry is None:
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, entry[0]), entry[1], value)

    @property
    def has_valid_url(self) -> bool:
        return bool(self.link.url and self.link.url.strip())

    def clear_url(self):
        """Очистить ссылку. Метаданные НЕ трогаются."""
        self.link.url = ""
        self.link.has_url = False
        self.status.reset()

    def normalized_name(self) -> str:
        mod = self.modified_date
        if self._cached_norm_key is not None and self._cached_norm_mod == mod:
            return self._cached_norm_key
        key = ChannelNameNormalizer.normalize(self.meta.name or "")
        object.__setattr__(self, '_cached_norm_key', key)
        object.__setattr__(self, '_cached_norm_mod', mod)
        return key

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
        object.__setattr__(c, 'modified_date', self.modified_date)
        object.__setattr__(c, 'original_index', self.original_index)
        return c

    def update_extinf(self):
        parts = ["#EXTINF:-1"]
        if self.tvg_id:
            parts.append(f'tvg-id="{self._escape(self.tvg_id)}"')
        if self.tvg_name:
            parts.append(f'tvg-name="{self._escape(self.tvg_name)}"')
        if self.tvg_logo:
            parts.append(f'tvg-logo="{self._escape(self.tvg_logo)}"')
        if self.group:
            parts.append(f'group-title="{self._escape(self.group)}"')
        if self.tvg_shift:
            parts.append(f'tvg-shift="{self._escape(self.tvg_shift)}"')
        if self.timeshift:
            parts.append(f'timeshift="{self._escape(self.timeshift)}"')
        if self.catchup:
            parts.append(f'catchup="{self._escape(self.catchup)}"')
        if self.catchup_source:
            parts.append(f'catchup-source="{self._escape(self.catchup_source)}"')
        if self.catchup_days:
            parts.append(f'catchup-days="{self._escape(self.catchup_days)}"')
        if self.tvg_rec:
            parts.append(f'tvg-rec="{self._escape(self.tvg_rec)}"')
        if self.tvg_chno:
            parts.append(f'tvg-chno="{self._escape(self.tvg_chno)}"')
        if self.audio_track:
            parts.append(f'audio-track="{self._escape(self.audio_track)}"')
        parts.append(f',{self.name}')
        self.link.extinf = ' '.join(parts)

    @staticmethod
    def _escape(v: str) -> str:
        if not v:
            return ""
        s = str(v)
        s = s.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
        s = s.replace('\\', '\\\\').replace('"', '\\"')
        return s

    def parse_extvlcopt_headers(self):
        self.link.extra_headers = {}
        self.link.user_agent = ""
        for line in self.link.extvlcopt_lines:
            if not line or '=' not in line:
                continue
            if line.startswith('#EXTVLCOPT:http-user-agent='):
                ua = line[len('#EXTVLCOPT:http-user-agent='):].strip('"')
                self.link.extra_headers['User-Agent'] = ua
                self.link.user_agent = ua
            elif (line.startswith('#EXTVLCOPT:http-referrer=') or
                  line.startswith('#EXTVLCOPT:http-referer=')):
                prefix = ('#EXTVLCOPT:http-referrer='
                          if line.startswith('#EXTVLCOPT:http-referrer=')
                          else '#EXTVLCOPT:http-referer=')
                ref = line[len(prefix):].strip('"')
                self.link.extra_headers['Referer'] = ref
            elif line.startswith('#EXTVLCOPT:http-header='):
                hl = line[len('#EXTVLCOPT:http-header='):].strip('"')
                if ':' in hl:
                    k, v = hl.split(':', 1)
                    self.link.extra_headers[k.strip()] = v.strip()

    def update_extvlcopt_from_headers(self):
        self.link.extvlcopt_lines = []
        if self.link.user_agent:
            self.link.extvlcopt_lines.append(
                f'#EXTVLCOPT:http-user-agent="{self._escape(self.link.user_agent)}"')
        for k, v in self.link.extra_headers.items():
            if k.lower() == 'user-agent':
                continue
            if k.lower() == 'referer':
                self.link.extvlcopt_lines.append(
                    f'#EXTVLCOPT:http-referrer="{self._escape(v)}"')
            else:
                self.link.extvlcopt_lines.append(
                    f'#EXTVLCOPT:http-header="{k}: {self._escape(v)}"')

    def get_url_foreground(self) -> QColor:
        if not self.has_valid_url:
            return URL_FG_COLORS['no_url']
        lq = self.status.link_quality
        if lq == LinkQuality.WORKING:
            return URL_FG_COLORS['working']
        if lq == LinkQuality.NOT_WORKING:
            return URL_FG_COLORS['not_working']
        if lq == LinkQuality.UNSUPPORTED:
            return URL_FG_COLORS['unsupported']
        if self.status.url_status is None:
            return URL_FG_COLORS['unchecked']
        return URL_FG_COLORS['neutral']

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
        if self.status.status_text and self.status.status_text != StatusText.UNCHECKED:
            return self.status.status_text
        return StatusText.UNCHECKED

    def get_status_tooltip(self) -> str:
        t = f"Канал: {self.meta.name}\n"
        if self.meta.original_name and self.meta.original_name != self.meta.name:
            t += f"Исходное имя: {self.meta.original_name}\n"
        t += f"Группа: {self.meta.group}\n"
        if self.meta.tvg_id:
            t += f"TVG-ID: {self.meta.tvg_id}\n"
        if self.original_index >= 0:
            t += f"Позиция в оригинале: {self.original_index}\n"
        if self.status.status_code is not None:
            t += f"HTTP-код: {self.status.status_code}\n"
        t += f"Статус: {self.get_status_text()}\n"
        if self.status.link_response_time is not None:
            t += f"Время ответа: {self.status.link_response_time:.2f} сек\n"
        if self.link.alternative_urls:
            t += f"Альтернативных ссылок: {len(self.link.alternative_urls)}\n"
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
            'name': self.meta.name, 'original_name': self.meta.original_name,
            'group': self.meta.group, 'tvg_id': self.meta.tvg_id,
            'tvg_name': self.meta.tvg_name, 'tvg_logo': self.meta.tvg_logo,
            'region': self.meta.region, 'quality': self.meta.quality,
            'tvg_shift': self.meta.tvg_shift,
            'timeshift': self.meta.timeshift,
            'catchup': self.meta.catchup,
            'catchup_source': self.meta.catchup_source,
            'catchup_days': self.meta.catchup_days,
            'tvg_rec': self.meta.tvg_rec,
            'tvg_chno': self.meta.tvg_chno,
            'audio_track': self.meta.audio_track,
            'epg_source': self.meta.epg_source,
            'url': self.link.url, 'extinf': self.link.extinf,
            'user_agent': self.link.user_agent,
            'extvlcopt_lines': list(self.link.extvlcopt_lines),
            'extra_headers': dict(self.link.extra_headers),
            'has_url': self.link.has_url,
            'alternative_urls': list(self.link.alternative_urls),
            'link_source': self.link.link_source,
            'url_status': self.status.url_status,
            'url_check_time': self.status.url_check_time.isoformat()
                if self.status.url_check_time else None,
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
            with suppress(ValueError, TypeError):
                uid = int(uid_raw)
        c = cls(uid=uid)
        c.restore_from_dict(data)
        return c

    def restore_from_dict(self, data: Dict[str, Any]):
        self.meta.name = data.get('name', '') or ''
        self.meta.original_name = data.get('original_name', '') or ''
        self.meta.group = data.get('group', DEFAULT_GROUP) or DEFAULT_GROUP
        self.meta.tvg_id = data.get('tvg_id', '') or ''
        self.meta.tvg_name = data.get('tvg_name', '') or ''
        self.meta.tvg_logo = data.get('tvg_logo', '') or ''
        self.meta.region = data.get('region', '') or ''
        with suppress(ValueError, TypeError):
            self.meta.quality = int(data.get('quality', 0) or 0)
        self.meta.tvg_shift = data.get('tvg_shift', '') or ''
        self.meta.timeshift = data.get('timeshift', '') or ''
        self.meta.catchup = data.get('catchup', '') or ''
        self.meta.catchup_source = data.get('catchup_source', '') or ''
        self.meta.catchup_days = data.get('catchup_days', '') or ''
        self.meta.tvg_rec = data.get('tvg_rec', '') or ''
        self.meta.tvg_chno = data.get('tvg_chno', '') or ''
        self.meta.audio_track = data.get('audio_track', '') or ''
        self.meta.epg_source = data.get('epg_source', '') or ''
        self.link.url = data.get('url', '') or ''
        self.link.extinf = data.get('extinf', '') or ''
        self.link.user_agent = data.get('user_agent', '') or ''
        self.link.extvlcopt_lines = list(data.get('extvlcopt_lines', []) or [])
        self.link.extra_headers = dict(data.get('extra_headers', {}) or {})
        self.link.has_url = bool(data.get('has_url', True))
        self.link.alternative_urls = list(data.get('alternative_urls', []) or [])
        self.link.link_source = data.get('link_source', '') or ''
        self.status.url_status = data.get('url_status')
        self.status.status_text = (data.get('status_text', StatusText.UNCHECKED)
                                    or StatusText.UNCHECKED)
        sc = data.get('status_code')
        if sc is not None:
            with suppress(ValueError, TypeError):
                self.status.status_code = int(sc)
        else:
            self.status.status_code = None
        rt = data.get('link_response_time')
        if rt is not None:
            with suppress(ValueError, TypeError):
                self.status.link_response_time = float(rt)
        for fld in ('url_check_time', 'modified_date'):
            v = data.get(fld)
            if v:
                dt = parse_datetime(v)
                if dt is None:
                    continue
                if fld == 'url_check_time':
                    self.status.url_check_time = dt
                else:
                    object.__setattr__(self, fld, dt)
        with suppress(ValueError, TypeError):
            self.status.link_quality = LinkQuality(int(data.get('link_quality', 0)))
        with suppress(ValueError, TypeError):
            object.__setattr__(self, 'original_index',
                               int(data.get('original_index', -1)))


# v0.9.5 fix: без этого вызова _ATTR_MAP остаётся пустым, и
# любой доступ к ch.tvg_id / ch.group / ch.url / ch.user_agent
# падает с AttributeError (update_extinf, get_status_tooltip,
# LinkSourceManager._post_process_channels и т.д.).
ChannelData._build_attr_map()


class EPGEntry:
    __slots__ = ('channel_id', 'start', 'stop', 'title', 'desc', 'category')

    def __init__(self):
        self.channel_id: str = ""
        self.start: Optional[datetime] = None
        self.stop: Optional[datetime] = None
        self.title: str = ""
        self.desc: str = ""
        self.category: str = ""


class EPGChannelInfo:
    __slots__ = ('channel_id', 'display_name', 'icon', 'lcn')

    def __init__(self):
        self.channel_id: str = ""
        self.display_name: str = ""
        self.icon: str = ""
        self.lcn: str = ""
