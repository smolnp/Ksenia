# -*- coding: utf-8 -*-
"""LinkSource, LinkSourceManager.

ПРИНЦИПИАЛЬНО: приложение не содержит встроенных ссылок на
IPTV-плейлисты. Пользователь добавляет источники сам или через
авто-поиск (GitHub API, live-запрос).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict
from contextlib import suppress
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from config import Config, LinkReplacementSettings
from constants import (
    LOADED_CHANNELS_TTL_SEC, MAX_LOADED_SOURCES,
    MAX_SOURCE_FILE_BYTES, SEARCH_WORKER_MAX,
    SOURCE_LOAD_TIMEOUT_SEC, FALLBACK_DAYS_DEFAULT,
    DEFAULT_TIMEOUT, CHECK_RESULT_CACHE_TTL_HOURS,
    ALIVE_INDEX_CACHE_MAX, StatusText,
    SEARCH_FUTURES_TIMEOUT_SEC, MAX_CHANNELS_PER_PLAYLIST,
    NORMALIZATION_SETTINGS_KEYS)
from logging_setup import logger
from models import ChannelData
from parsers import M3UParser
from paths import parse_datetime
from storage import BaseJsonStore
from name_normalizer import (
    NormMode, normalize, normalize_with_settings,
    similarity as name_similarity, is_stub_tvg_id)
from utils import (_StopToken, cancelled, decode_m3u_bytes,
                    InputValidationError, URLUtils)


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36',
        'Accept': '*/*',
    })
    return s


def _now_naive() -> datetime:
    return datetime.now()


def _normalize_key(name: str,
                    settings: Optional[LinkReplacementSettings] = None
                    ) -> str:
    if not name:
        return ""
    return normalize_with_settings(name, settings, NormMode.FULL)


class LinkSource:
    __slots__ = (
        'name', 'path', 'source_type', 'last_updated', 'total_links',
        'total_with_url', 'priority', 'enabled', 'auto_update',
        'update_interval_hours', 'encoding',
        'last_error', 'last_attempt', 'consecutive_errors',
        'apply_blacklist', 'apply_domain_blacklist',
        'total_working', 'working_checked_at',
        'raw_total_links', 'raw_total_with_url',
        'generated')

    def __init__(self):
        self.name: str = ""
        self.path: str = ""
        self.source_type: str = "local"
        self.last_updated: Optional[datetime] = None
        self.total_links: int = 0
        self.total_with_url: int = 0
        self.priority: int = 5
        self.enabled: bool = True
        self.auto_update: bool = False
        self.update_interval_hours: int = 24
        self.encoding: str = "utf-8"
        self.last_error: str = ""
        self.last_attempt: Optional[datetime] = None
        self.consecutive_errors: int = 0
        self.apply_blacklist: bool = True
        self.apply_domain_blacklist: bool = True
        self.total_working: int = 0
        self.working_checked_at: Optional[float] = None
        self.raw_total_links: int = 0
        self.raw_total_with_url: int = 0
        self.generated: bool = False

    def should_refresh(self) -> bool:
        if not self.auto_update:
            return False
        now = _now_naive()
        if self.consecutive_errors > 0 and self.last_attempt:
            err_age_h = (
                (now - self.last_attempt).total_seconds() / 3600.0)
            min_delay = min(8, 2 ** min(self.consecutive_errors, 3))
            if err_age_h < min_delay:
                return False
        if not self.last_updated:
            return True
        age_h = (now - self.last_updated).total_seconds() / 3600.0
        return age_h >= self.update_interval_hours

    def copy(self) -> 'LinkSource':
        s = LinkSource()
        for slot in self.__slots__:
            setattr(s, slot, getattr(self, slot))
        return s

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name, 'path': self.path,
            'source_type': self.source_type,
            'last_updated': self.last_updated.isoformat()
                if self.last_updated else None,
            'total_links': self.total_links,
            'total_with_url': self.total_with_url,
            'priority': self.priority, 'enabled': self.enabled,
            'auto_update': self.auto_update,
            'update_interval_hours': self.update_interval_hours,
            'encoding': self.encoding,
            'last_error': self.last_error,
            'last_attempt': self.last_attempt.isoformat()
                if self.last_attempt else None,
            'consecutive_errors': self.consecutive_errors,
            'apply_blacklist': self.apply_blacklist,
            'apply_domain_blacklist': self.apply_domain_blacklist,
            'total_working': self.total_working,
            'working_checked_at': self.working_checked_at,
            'raw_total_links': self.raw_total_links,
            'raw_total_with_url': self.raw_total_with_url,
            'generated': self.generated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LinkSource':
        s = cls()
        s.name = data.get('name', '') or ''
        s.path = data.get('path', '') or ''
        s.source_type = data.get('source_type', 'local') or 'local'
        for fld, default in (('total_links', 0),
                             ('total_with_url', 0),
                             ('priority', 5),
                             ('update_interval_hours', 24),
                             ('consecutive_errors', 0)):
            with suppress(ValueError, TypeError):
                setattr(s, fld,
                        int(data.get(fld, default) or default))
        s.priority = max(1, min(10, s.priority))
        s.enabled = bool(data.get('enabled', True))
        s.auto_update = bool(data.get('auto_update', False))
        s.encoding = data.get('encoding', 'utf-8') or 'utf-8'
        s.last_error = data.get('last_error', '') or ''
        s.apply_blacklist = bool(
            data.get('apply_blacklist', True))
        s.apply_domain_blacklist = bool(
            data.get('apply_domain_blacklist', True))
        s.generated = bool(data.get('generated', False))
        for fld in ('last_updated', 'last_attempt'):
            dt = parse_datetime(data.get(fld))
            if dt is not None:
                setattr(s, fld, dt)
        with suppress(ValueError, TypeError):
            s.total_working = int(
                data.get('total_working', 0) or 0)
        wca = data.get('working_checked_at')
        if wca is not None:
            with suppress(ValueError, TypeError):
                s.working_checked_at = float(wca)
        with suppress(ValueError, TypeError):
            s.raw_total_links = int(
                data.get('raw_total_links',
                         s.total_links) or 0)
        with suppress(ValueError, TypeError):
            s.raw_total_with_url = int(
                data.get('raw_total_with_url',
                         s.total_with_url) or 0)
        return s


class LinkSourceManager:
    def __init__(self, config_dir: str,
                 cache_manager: Optional['CacheManager'] = None):
        self._store = BaseJsonStore(
            os.path.join(config_dir, "link_sources.json"), [])
        self.cache_manager = cache_manager
        self._lock = threading.RLock()
        self._sources: List[LinkSource] = [
            LinkSource.from_dict(x) for x in self._store._data
            if isinstance(x, dict)
        ]
        # Никакого бутстрапа: пустой список — норма.
        # Пользователь добавляет источники вручную или через
        # «🔍 Найти источники автоматически» (GitHub API).

        self._loaded: "OrderedDict[str, Tuple[float, str, List[ChannelData]]]" = \
            OrderedDict()
        self._name_index: "OrderedDict[str, Dict[str, List[ChannelData]]]" = \
            OrderedDict()
        self._alive_index: "OrderedDict[str, Dict[str, List[ChannelData]]]" = \
            OrderedDict()
        self._alive_index_cache: "OrderedDict[frozenset, List[str]]" = \
            OrderedDict()
        self._alive_index_cache_key: Optional[str] = None
        self._search_pool: Optional[
            concurrent.futures.ThreadPoolExecutor] = None
        self._search_pool_lock = threading.Lock()

    @staticmethod
    def _settings_fingerprint(
            settings: Optional[LinkReplacementSettings]
    ) -> str:
        if settings is None:
            base = {k: True for k in NORMALIZATION_SETTINGS_KEYS}
        else:
            base = {}
            for k in NORMALIZATION_SETTINGS_KEYS:
                try:
                    base[k] = bool(getattr(settings, k, True))
                except Exception:
                    base[k] = True
        raw = json.dumps(base, sort_keys=True).encode('utf-8')
        return hashlib.sha1(raw).hexdigest()[:16]

    @staticmethod
    def get_settings_fingerprint(
            settings: Optional[LinkReplacementSettings] = None,
    ) -> str:
        return LinkSourceManager._settings_fingerprint(settings)

    def _get_search_pool(
            self) -> concurrent.futures.ThreadPoolExecutor:
        with self._search_pool_lock:
            if self._search_pool is None:
                self._search_pool = \
                    concurrent.futures.ThreadPoolExecutor(
                        max_workers=SEARCH_WORKER_MAX,
                        thread_name_prefix="search")
            return self._search_pool

    def shutdown(self):
        with self._search_pool_lock:
            pool = self._search_pool
            self._search_pool = None
        if pool is not None:
            with suppress(Exception):
                pool.shutdown(wait=True, cancel_futures=True)

    def invalidate_cache(self,
                          source_name: Optional[str] = None):
        with self._lock:
            if source_name is None:
                self._loaded.clear()
                self._name_index.clear()
                self._alive_index.clear()
            else:
                self._loaded.pop(source_name, None)
                self._name_index.pop(source_name, None)
                self._alive_index.pop(source_name, None)
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
        if self.cache_manager is not None and source_name is not None:
            try:
                self.cache_manager.clear_normalized_index(
                    source_name)
            except Exception:
                logger.exception(
                    "clear_normalized_index(%s)", source_name)

    def _persist(self) -> bool:
        with self._lock:
            self._store._data = [s.to_dict()
                                  for s in self._sources]
        return self._store.save()

    def add_source(self, source: LinkSource) -> bool:
        with self._lock:
            for s in self._sources:
                if s.name == source.name:
                    return False
            self._sources.append(source)
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
        return self._persist()

    def remove_source(self, source_name: str) -> bool:
        with self._lock:
            self._sources = [s for s in self._sources
                             if s.name != source_name]
            self._loaded.pop(source_name, None)
            self._name_index.pop(source_name, None)
            self._alive_index.pop(source_name, None)
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
        if self.cache_manager is not None:
            try:
                self.cache_manager.clear_normalized_index(
                    source_name)
            except Exception:
                logger.exception("clear_normalized_index")
        return self._persist()

    def remove_generated_sources(self) -> int:
        with self._lock:
            before = len(self._sources)
            removed_names = [s.name for s in self._sources
                             if s.generated]
            self._sources = [s for s in self._sources
                             if not s.generated]
            for name in removed_names:
                self._loaded.pop(name, None)
                self._name_index.pop(name, None)
                self._alive_index.pop(name, None)
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
            removed = before - len(self._sources)
        if removed and self.cache_manager is not None:
            for name in removed_names:
                try:
                    self.cache_manager.clear_normalized_index(name)
                except Exception:
                    logger.exception("clear_normalized_index")
        if removed:
            self._persist()
        return removed

    def update_source(self, old_name: str,
                      new_source: LinkSource) -> bool:
        with self._lock:
            found = False
            for i, s in enumerate(self._sources):
                if s.name == old_name:
                    self._sources[i] = new_source
                    for k in (old_name, new_source.name):
                        self._loaded.pop(k, None)
                        self._name_index.pop(k, None)
                        self._alive_index.pop(k, None)
                    self._alive_index_cache.clear()
                    self._alive_index_cache_key = None
                    found = True
                    break
            if not found:
                return False
        if self.cache_manager is not None:
            for k in (old_name, new_source.name):
                try:
                    self.cache_manager.clear_normalized_index(k)
                except Exception:
                    logger.exception("clear_normalized_index")
        return self._persist()

    def get_all_sources(self) -> List[LinkSource]:
        with self._lock:
            return list(self._sources)

    def get_enabled_sources(self) -> List[LinkSource]:
        with self._lock:
            return [s for s in self._sources if s.enabled]

    def get_source_by_name(
            self, name: str) -> Optional[LinkSource]:
        with self._lock:
            for s in self._sources:
                if s.name == name:
                    return s
        return None

    def update_source_health(
            self, name: str, working: int,
            checked_at: Optional[float] = None):
        with self._lock:
            for s in self._sources:
                if s.name == name:
                    s.total_working = max(0, int(working))
                    s.working_checked_at = (
                        float(checked_at)
                        if checked_at is not None
                        else time.time())
                    if int(working) == 0:
                        s.total_links = 0
                        s.total_with_url = 0
                    break
            else:
                return
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
        self._persist()

    @staticmethod
    def _try_previous_days(
            url: str, days: int = FALLBACK_DAYS_DEFAULT,
            stop_token: Optional['_StopToken'] = None
    ) -> Optional[str]:
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', url)
        if not m:
            return None
        try:
            base = datetime.strptime(m.group(0), "%Y-%m-%d")
        except ValueError:
            return None
        with _new_session() as session:
            for d in range(1, days + 1):
                if cancelled(stop_token):
                    return None
                prev = (base - timedelta(
                    days=d)).strftime("%Y-%m-%d")
                candidate = url.replace(m.group(0), prev, 1)
                try:
                    with session.get(candidate,
                                     timeout=DEFAULT_TIMEOUT,
                                     verify=False,
                                     stream=True) as r:
                        if r.status_code == 200:
                            return candidate
                except Exception:
                    continue
        return None

    @staticmethod
    def _post_process_channels(
            channels: List[ChannelData],
            source: LinkSource,
            blacklist_manager=None,
            domain_blacklist_manager=None,
            domain_user_agent_manager=None
    ) -> List[ChannelData]:
        if (blacklist_manager is not None
                and source.apply_blacklist):
            channels, _ = blacklist_manager.filter_channels(
                channels)
        if (domain_blacklist_manager is not None
                and source.apply_domain_blacklist):
            channels, _ = \
                domain_blacklist_manager.clean_channels(channels)

        for ch in channels:
            ch.link.link_source = source.name
            if not ch.link.extinf:
                ch.update_extinf()
            ch.parse_extvlcopt_headers()

        if (domain_user_agent_manager is not None
                and domain_user_agent_manager.get_all_rules()):
            domain_user_agent_manager.apply_rules_to_channels(
                channels)
        return channels

    @staticmethod
    def _is_url_blocked(url: str,
                        temp_domains: List[str],
                        unsafe_domains: List[str]) -> bool:
        host = URLUtils.extract_host(url) or ''
        if not host:
            return False
        return URLUtils.host_in_domains(
            host, list(temp_domains) + list(unsafe_domains))

    def _build_alive_index(
            self, channels: List[ChannelData], bl_mgr,
            temp_domains: List[str],
            unsafe_domains: List[str],
            settings: Optional[LinkReplacementSettings] = None
    ) -> Dict[str, List[ChannelData]]:
        alive_index: Dict[str, List[ChannelData]] = defaultdict(
            list)
        if self.cache_manager is None or not channels:
            return dict(alive_index)

        pairs: List[Tuple[str, str]] = []
        ch_by_pair: Dict[Tuple[str, str],
                          List[ChannelData]] = defaultdict(list)
        for ch in channels:
            url = ch.link.url
            if not url:
                continue
            if bl_mgr is not None:
                try:
                    if bl_mgr.matches(ch.meta.name,
                                       ch.meta.tvg_id):
                        continue
                except Exception:
                    logger.exception(
                        "bl_mgr.matches in alive_index")
            if settings is not None:
                try:
                    if settings.is_blacklisted(url):
                        continue
                    if settings.is_filtered_domain(url):
                        continue
                except Exception:
                    logger.exception(
                        "settings filter in alive_index")
            pair = (ch.meta.name.lower(), url)
            pairs.append(pair)
            ch_by_pair[pair].append(ch)

        if not pairs:
            return dict(alive_index)

        try:
            cached = self.cache_manager.get_check_results_batch(
                pairs,
                max_age_hours=CHECK_RESULT_CACHE_TTL_HOURS)
        except Exception:
            logger.exception("alive_index batch read")
            cached = {}

        for pair, info in cached.items():
            if not info.get('alive'):
                continue
            for ch in ch_by_pair.get(pair, ()):
                for key in ch.search_keys(settings):
                    alive_index[key].append(ch)
        return dict(alive_index)

    def _store_normalized_index(
            self, source_name: str,
            channels: List[ChannelData],
            settings: Optional[LinkReplacementSettings] = None,
    ) -> int:
        if self.cache_manager is None or not channels:
            return 0
        settings_fp = self._settings_fingerprint(settings)
        pairs: List[Tuple[str, str]] = []
        ch_by_pair: Dict[Tuple[str, str],
                          List[ChannelData]] = defaultdict(list)
        for ch in channels:
            url = ch.link.url
            if not url:
                continue
            pair = (ch.meta.name.lower(), url)
            pairs.append(pair)
            ch_by_pair[pair].append(ch)
        if not pairs:
            try:
                self.cache_manager.save_normalized_index(
                    [], source_name, settings_fp,
                    replace_source=True)
            except Exception:
                logger.exception(
                    "save_normalized_index(empty)")
            return 0
        try:
            cached = self.cache_manager.get_check_results_batch(
                pairs,
                max_age_hours=CHECK_RESULT_CACHE_TTL_HOURS)
        except Exception:
            logger.exception("normalized_index batch read")
            cached = {}

        rows: List[Dict[str, Any]] = []
        for pair, info in cached.items():
            if not info.get('alive'):
                continue
            for ch in ch_by_pair.get(pair, ()):
                name = (ch.meta.name or '').strip()
                tvg_id = (ch.meta.tvg_id or '').strip()
                url = (ch.link.url or '').strip()
                if not name or not url:
                    continue
                for sk in ch.search_keys(settings):
                    if not sk:
                        continue
                    rows.append({
                        'search_key': sk,
                        'name': name,
                        'tvg_id': tvg_id,
                        'url': url,
                        'priority': 5,
                    })
        try:
            src = self.get_source_by_name(source_name)
            prio = int(src.priority) if src else 5
        except Exception:
            prio = 5
        for r in rows:
            r['priority'] = prio
        try:
            return self.cache_manager.save_normalized_index(
                rows, source_name, settings_fp,
                replace_source=True)
        except Exception:
            logger.exception("save_normalized_index")
            return 0

    def _store_loaded(
            self, name: str, path: str,
            channels: List[ChannelData],
            bl_mgr=None,
            temp_domains: Optional[List[str]] = None,
            unsafe_domains: Optional[List[str]] = None,
            settings: Optional[LinkReplacementSettings] = None
    ):
        if not name:
            logger.warning(
                "_store_loaded: empty source name, skipping")
            return

        settings_fp = self._settings_fingerprint(settings)
        from_db: Optional[Dict[str, List[Dict[str, Any]]]] = None
        if self.cache_manager is not None:
            try:
                loaded = self.cache_manager.load_normalized_index(
                    settings_fp, max_age_hours=24, source=name)
                from_db = loaded
            except Exception:
                logger.exception("load_normalized_index")

        index: Dict[str, List[ChannelData]] = defaultdict(list)
        alive_index: Dict[str, List[ChannelData]] = defaultdict(list)

        if from_db and name in from_db and from_db[name]:
            by_url: Dict[str, ChannelData] = {}
            for row in from_db[name]:
                url = (row.get('url') or '').strip()
                if not url:
                    continue
                ch = by_url.get(url)
                if ch is None:
                    ch = ChannelData()
                    ch.meta.name = (row.get('name') or '')
                    ch.meta.tvg_id = (row.get('tvg_id') or '')
                    ch.link.url = url
                    ch.link.has_url = True
                    ch.link.link_source = name
                    ch.update_extinf()
                    by_url[url] = ch
                sk = (row.get('search_key') or '').strip()
                if not sk:
                    continue
                index[sk].append(ch)
                alive_index[sk].append(ch)
        else:
            for ch in channels:
                for key in ch.search_keys(settings):
                    index[key].append(ch)
            alive_index = self._build_alive_index(
                channels, bl_mgr,
                temp_domains or [],
                unsafe_domains or [],
                settings=settings)

        with self._lock:
            self._loaded[name] = (time.time(), path, channels)
            self._loaded.move_to_end(name)
            self._name_index[name] = dict(index)
            self._name_index.move_to_end(name)
            self._alive_index[name] = dict(alive_index)
            self._alive_index.move_to_end(name)
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
            while len(self._loaded) > MAX_LOADED_SOURCES:
                old_name, _ = self._loaded.popitem(last=False)
                self._name_index.pop(old_name, None)
                self._alive_index.pop(old_name, None)

        if self.cache_manager is not None:
            try:
                self._store_normalized_index(
                    name, channels, settings=settings)
            except Exception:
                logger.exception("_store_normalized_index(%s)",
                                  name)

    def rebuild_alive_index(
            self, source_name: Optional[str] = None,
            blacklist_manager=None,
            temp_domains: Optional[List[str]] = None,
            unsafe_domains: Optional[List[str]] = None,
            settings: Optional[LinkReplacementSettings] = None
    ) -> int:
        if self.cache_manager is None:
            return 0
        with self._lock:
            if source_name is None:
                names = list(self._loaded.keys())
            else:
                names = ([source_name]
                         if source_name in self._loaded else [])
        rebuilt = 0
        for name in names:
            with self._lock:
                entry = self._loaded.get(name)
            if not entry:
                continue
            _ts, _path, channels = entry
            if not channels:
                continue
            alive_index = self._build_alive_index(
                channels, blacklist_manager,
                temp_domains or [], unsafe_domains or [],
                settings=settings)
            with self._lock:
                self._alive_index[name] = alive_index
                self._alive_index.move_to_end(name)
            try:
                self._store_normalized_index(
                    name, channels, settings=settings)
            except Exception:
                logger.exception("_store_normalized_index(%s)",
                                  name)
            rebuilt += 1
        with self._lock:
            self._alive_index_cache.clear()
            self._alive_index_cache_key = None
        logger.info(
            "rebuild_alive_index: rebuilt %d sources (source=%s)",
            rebuilt, source_name or 'ALL')
        return rebuilt

    def has_alive_index(self) -> bool:
        with self._lock:
            return any(bool(idx)
                       for idx in self._alive_index.values())

    def has_normalized_index(
            self, settings: Optional[LinkReplacementSettings] = None,
            max_age_hours: int = 24,
    ) -> bool:
        if self.cache_manager is None:
            return False
        fp = self._settings_fingerprint(settings)
        try:
            data = self.cache_manager.load_normalized_index(
                fp, max_age_hours=max_age_hours)
            if not data:
                return False
            for rows in data.values():
                if rows:
                    return True
            return False
        except Exception:
            logger.exception("has_normalized_index")
            return False

    def get_normalized_index_stats(
            self, settings: Optional[LinkReplacementSettings] = None,
            max_age_hours: int = 24,
    ) -> Dict[str, Any]:
        base = {
            'count': 0, 'sources': 0, 'fresh_count': 0,
            'total_count': 0, 'is_fresh': False,
            'settings_match': False,
        }
        if self.cache_manager is None:
            return base
        fp = self._settings_fingerprint(settings)
        try:
            st = self.cache_manager.get_normalized_index_stats(
                max_age_hours=max_age_hours)
        except Exception:
            logger.exception("get_normalized_index_stats")
            return base
        base['total_count'] = int(st.get('count', 0) or 0)
        try:
            data = self.cache_manager.load_normalized_index(
                fp, max_age_hours=max_age_hours)
        except Exception:
            logger.exception("load_normalized_index")
            data = {}
        if data:
            base['count'] = sum(len(v) for v in data.values())
            base['sources'] = len(data)
            base['fresh_count'] = base['count']
            base['settings_match'] = True
            base['is_fresh'] = base['fresh_count'] > 0
        return base

    def load_links_from_source(
            self, source: LinkSource,
            use_cache: bool = True,
            config: Optional[Config] = None,
            update_meta: bool = True,
            stop_token: Optional['_StopToken'] = None,
            *,
            blacklist_manager=None,
            domain_blacklist_manager=None,
            domain_user_agent_manager=None,
            settings: Optional[LinkReplacementSettings] = None
    ) -> List[ChannelData]:
        if use_cache:
            with self._lock:
                entry = self._loaded.get(source.name)
                if entry:
                    ts, cached_path, chs = entry
                    if (time.time() - ts
                            < LOADED_CHANNELS_TTL_SEC
                            and cached_path == source.path):
                        return chs

        channels: List[ChannelData] = []
        source.last_attempt = _now_naive()
        try:
            if source.source_type == "local":
                channels = self._load_local(
                    source, blacklist_manager,
                    domain_blacklist_manager,
                    domain_user_agent_manager,
                    settings)
            else:
                channels = self._load_remote(
                    source, use_cache, config, stop_token,
                    blacklist_manager, domain_blacklist_manager,
                    domain_user_agent_manager, settings)
            if channels:
                source.last_error = ""
                source.consecutive_errors = 0
            else:
                if source.last_error == StatusText.CANCELLED:
                    return []
                if not source.last_error:
                    source.last_error = "Empty result"
                source.consecutive_errors += 1
                source.total_working = 0
                source.working_checked_at = None
            if update_meta:
                self._finalize_source(source, channels)
            self._store_loaded(
                source.name, source.path, channels,
                blacklist_manager,
                list(config.get('temporary_domains', []))
                if config else [],
                list(config.get('unsafe_domains', []))
                if config else [],
                settings=settings)
            self._persist()
        except InputValidationError as e:
            source.last_error = str(e)[:200]
            source.last_attempt = _now_naive()
            source.consecutive_errors += 1
            self._persist()
            logger.warning("Source %s rejected: %s",
                            source.name, e)
            return []
        except Exception as e:
            source.last_error = str(e)[:200]
            source.last_attempt = _now_naive()
            source.consecutive_errors += 1
            self._persist()
            logger.exception("Failed to load source %s",
                             source.name)
            return []
        return channels

    def _load_local(
            self, source: LinkSource,
            blacklist_manager=None,
            domain_blacklist_manager=None,
            domain_user_agent_manager=None,
            settings: Optional[LinkReplacementSettings] = None
    ) -> List[ChannelData]:
        if not os.path.exists(source.path):
            source.last_error = f"File not found: {source.path}"
            return []
        try:
            if os.path.getsize(source.path) > MAX_SOURCE_FILE_BYTES:
                source.last_error = "File is too large"
                return []
            with open(source.path, 'rb') as f:
                raw = f.read()
        except PermissionError as e:
            source.last_error = f"Access denied: {e}"
            return []
        except OSError as e:
            source.last_error = f"Read error: {e}"
            return []

        if source.encoding and source.encoding != 'auto':
            try:
                content = raw.decode(source.encoding,
                                      errors='replace')
            except LookupError:
                content, _ = decode_m3u_bytes(raw)
        else:
            content, _ = decode_m3u_bytes(raw)

        parsed = M3UParser.parse(content, source.name,
                                 settings=settings)

        if len(parsed) > MAX_CHANNELS_PER_PLAYLIST:
            logger.warning(
                "%s: playlist has %d channels, truncating to %d",
                source.name, len(parsed),
                MAX_CHANNELS_PER_PLAYLIST)
            parsed = parsed[:MAX_CHANNELS_PER_PLAYLIST]

        source.raw_total_links = len(parsed)
        source.raw_total_with_url = sum(
            1 for c in parsed if c.has_valid_url)
        return self._post_process_channels(
            parsed, source,
            blacklist_manager, domain_blacklist_manager,
            domain_user_agent_manager)

    def _load_remote(
            self, source: LinkSource, use_cache: bool,
            config: Optional[Config],
            stop_token: Optional['_StopToken'] = None,
            blacklist_manager=None,
            domain_blacklist_manager=None,
            domain_user_agent_manager=None,
            settings: Optional[LinkReplacementSettings] = None
    ) -> List[ChannelData]:
        if use_cache:
            with self._lock:
                entry = self._loaded.get(source.name)
                if entry:
                    ts, cached_path, chs = entry
                    if (time.time() - ts
                            < LOADED_CHANNELS_TTL_SEC
                            and cached_path == source.path):
                        return chs
        if (use_cache and config
                and self.cache_manager is not None
                and config.get('use_link_cache', True)):
            cached = self.cache_manager.get_link_cache(
                source.path,
                config.get('link_cache_hours', 6))
            if cached:
                chs = self._dicts_to_channels(
                    cached, source.name)
                source.raw_total_links = len(chs)
                source.raw_total_with_url = sum(
                    1 for c in chs if c.has_valid_url)
                return self._post_process_channels(
                    chs, source,
                    blacklist_manager, domain_blacklist_manager,
                    domain_user_agent_manager)

        channels: List[ChannelData] = []
        primary_error = ""
        was_stopped = False
        with _new_session() as session:
            try:
                with session.get(
                        source.path,
                        timeout=(5, SOURCE_LOAD_TIMEOUT_SEC),
                        verify=False, stream=True) as r:
                    if r.status_code == 200:
                        buf = bytearray()
                        for chunk in r.iter_content(
                                chunk_size=256 * 1024):
                            if cancelled(stop_token):
                                was_stopped = True
                                break
                            buf.extend(chunk)
                            if len(buf) > MAX_SOURCE_FILE_BYTES:
                                primary_error = "File is too large"
                                break
                        if not primary_error and not was_stopped:
                            content, _ = decode_m3u_bytes(
                                bytes(buf))
                            try:
                                channels = M3UParser.parse(
                                    content, source.name,
                                    settings=settings)
                            except InputValidationError as e:
                                primary_error = str(e)[:100]
                                channels = []
                    else:
                        primary_error = f"HTTP {r.status_code}"
            except Exception as e:
                primary_error = str(e)[:100]
                logger.exception("Failed to load %s",
                                  source.name)

            if was_stopped:
                source.last_error = StatusText.CANCELLED
                return []

            if not channels:
                days_raw = (config.get('days_to_check',
                                        FALLBACK_DAYS_DEFAULT)
                            if config else FALLBACK_DAYS_DEFAULT)
                try:
                    days = int(days_raw)
                except (ValueError, TypeError):
                    days = FALLBACK_DAYS_DEFAULT
                fb = self._try_previous_days(
                    source.path, days, stop_token)
                if fb:
                    try:
                        with session.get(
                                fb,
                                timeout=(5, SOURCE_LOAD_TIMEOUT_SEC),
                                verify=False) as r:
                            if r.status_code == 200:
                                text, _ = decode_m3u_bytes(
                                    r.content)
                                channels = M3UParser.parse(
                                    text, source.name,
                                    settings=settings)
                    except Exception as e:
                        source.last_error = (
                            f"Fallback: {str(e)[:100]}")
                if not channels and primary_error:
                    source.last_error = primary_error

            if len(channels) > MAX_CHANNELS_PER_PLAYLIST:
                channels = channels[:MAX_CHANNELS_PER_PLAYLIST]

            source.raw_total_links = len(channels)
            source.raw_total_with_url = sum(
                1 for c in channels if c.has_valid_url)

            channels = self._post_process_channels(
                channels, source,
                blacklist_manager, domain_blacklist_manager,
                domain_user_agent_manager)

            if (channels and use_cache and config
                    and self.cache_manager is not None
                    and config.get('use_link_cache', True)):
                try:
                    self.cache_manager.put_link_cache(
                        source.path,
                        [c.to_dict() for c in channels])
                except Exception:
                    logger.warning(
                        "put_link_cache failed for %s",
                        source.path, exc_info=True)
            return channels

    def _finalize_source(self, source: LinkSource,
                          channels: List[ChannelData]):
        source.total_links = len(channels)
        source.total_with_url = sum(
            1 for ch in channels if ch.has_valid_url)
        if source.raw_total_links == 0:
            source.raw_total_links = source.total_links
        if source.raw_total_with_url == 0:
            source.raw_total_with_url = source.total_with_url
        source.last_updated = _now_naive()

    @classmethod
    def _dicts_to_channels(
            cls, dicts: List[Dict[str, Any]],
            source_name: str) -> List[ChannelData]:
        result: List[ChannelData] = []
        for d in dicts:
            if not isinstance(d, dict):
                continue
            ch = ChannelData.from_dict(d)
            ch.link.link_source = source_name
            if not ch.link.extinf:
                ch.update_extinf()
            result.append(ch)
        return result

    def _is_working_url(self, ch: ChannelData,
                        cache_manager=None) -> bool:
        if ch.status.url_status is True:
            return True
        if ch.status.url_status is False:
            return False
        cm = cache_manager or self.cache_manager
        if cm is not None and ch.link.url:
            try:
                hit = cm.get_check_result(
                    ch.meta.name.lower(), ch.link.url,
                    max_age_hours=CHECK_RESULT_CACHE_TTL_HOURS)
            except Exception:
                hit = None
            if hit is not None:
                return bool(hit.get('alive'))
        return False

    def _get_alive_urls_for_keys_locked(
            self, keys: frozenset,
            settings: LinkReplacementSettings,
            limit: int) -> List[str]:
        results: List[str] = []
        seen: Set[str] = set()
        sources = sorted(
            self._sources,
            key=lambda s: (not s.enabled, s.priority))
        for src in sources:
            if not src.enabled:
                continue
            if len(results) >= limit:
                break
            alive_index = self._alive_index.get(src.name)
            if not alive_index:
                continue
            for key in keys:
                for ch in alive_index.get(key, ()):
                    url = ch.link.url
                    if not url or url in seen:
                        continue
                    if settings.is_blacklisted(url):
                        continue
                    if settings.is_filtered_domain(url):
                        continue
                    seen.add(url)
                    results.append(url)
                    if len(results) >= limit:
                        break
                if len(results) >= limit:
                    break
        return results

    def get_alive_urls_for_keys(
            self, keys: Set[str],
            settings: LinkReplacementSettings,
            limit: Optional[int] = None
    ) -> List[str]:
        if not keys:
            return []
        if limit is None:
            limit = max(1, int(
                settings.max_urls_to_check_per_channel))
        cache_key = frozenset(keys)

        with self._lock:
            fp = self._settings_fingerprint(settings)
            if self._alive_index_cache_key != fp:
                self._alive_index_cache.clear()
                self._alive_index_cache_key = fp
            cached = self._alive_index_cache.get(cache_key)
            if cached is not None:
                self._alive_index_cache.move_to_end(cache_key)
                return list(cached[:limit])
            results = self._get_alive_urls_for_keys_locked(
                cache_key, settings, limit)
            self._alive_index_cache[cache_key] = list(results)
            self._alive_index_cache.move_to_end(cache_key)
            while (len(self._alive_index_cache)
                   > ALIVE_INDEX_CACHE_MAX):
                self._alive_index_cache.popitem(last=False)
        return list(results)

    def get_alive_urls(self, channel_name: str,
                       settings: LinkReplacementSettings,
                       limit: Optional[int] = None
                       ) -> List[str]:
        norm = _normalize_key(channel_name, settings)
        if not norm:
            return []
        return self.get_alive_urls_for_keys({norm}, settings, limit)

    def _source_priority(self, source_name: str) -> int:
        s = self.get_source_by_name(source_name)
        return s.priority if s else 5

    def _is_match_normalized(
            self, search_norm: str, cnorm: str,
            settings: LinkReplacementSettings) -> bool:
        search_type = settings.search_type
        if search_type == "exact":
            return search_norm == cnorm
        threshold = max(0.0, min(
            1.0, settings.match_threshold_percent / 100.0))
        if search_type == "similar":
            if not settings.use_fuzzy_matching:
                return search_norm == cnorm
            ratio = SequenceMatcher(
                None, search_norm, cnorm).ratio()
            return ratio >= max(
                settings.min_name_similarity, threshold)
        if search_type == "fuzzy":
            if not settings.use_fuzzy_matching:
                return search_norm == cnorm
            from name_normalizer import jaccard as _j
            return _j(search_norm, cnorm, settings=None) >= max(
                settings.min_name_similarity, threshold)
        return False

    def _match_score(
            self, search_keys: Set[str], cnorm: str,
            settings: LinkReplacementSettings) -> float:
        search_type = settings.search_type
        if search_type == "exact":
            return 1.0 if cnorm in search_keys else 0.0

        threshold = max(0.0, min(
            1.0, settings.match_threshold_percent / 100.0))
        min_sim = settings.min_name_similarity
        best = 0.0
        if search_type == "similar":
            if not settings.use_fuzzy_matching:
                return 1.0 if cnorm in search_keys else 0.0
            for sk in search_keys:
                ratio = SequenceMatcher(
                    None, sk, cnorm).ratio()
                if ratio >= max(min_sim, threshold) and ratio > best:
                    best = ratio
            return best
        if search_type == "fuzzy":
            if not settings.use_fuzzy_matching:
                return 1.0 if cnorm in search_keys else 0.0
            from name_normalizer import jaccard as _j
            for sk in search_keys:
                j = _j(sk, cnorm, settings=None)
                if j >= max(min_sim, threshold) and j > best:
                    best = j
            return best
        return 0.0

    def search_channel(
            self, search_keys: Set[str],
            settings: LinkReplacementSettings,
            stop_token: Optional['_StopToken'] = None,
            config: Optional[Config] = None,
            blacklist_manager=None,
            domain_blacklist_manager=None,
            domain_user_agent_manager=None,
            only_working: bool = True
    ) -> List[ChannelData]:
        if not search_keys:
            return []
        results: List[ChannelData] = []
        enabled = self.get_enabled_sources()

        dm = domain_blacklist_manager

        def _url_allowed(url: str) -> bool:
            if not url or not url.strip():
                return False
            if settings.is_blacklisted(url):
                return False
            if dm is not None and dm.matches_url(url):
                return False
            if settings.is_filtered_domain(url):
                return False
            return True

        def _accept(ch: ChannelData) -> bool:
            if not _url_allowed(ch.link.url):
                return False
            if only_working and not self._is_working_url(
                    ch, cache_manager=self.cache_manager):
                return False
            if blacklist_manager is not None:
                if blacklist_manager.matches(
                        ch.meta.name, ch.meta.tvg_id):
                    return False
            return True

        def _collect_from_index(
                index: Dict[str, List[ChannelData]],
                search_type: str) -> List[ChannelData]:
            out: List[ChannelData] = []
            if search_type == "exact":
                seen_uids: Set[int] = set()
                for key in search_keys:
                    for ch in index.get(key, ()):
                        if ch.uid in seen_uids:
                            continue
                        if cancelled(stop_token):
                            break
                        if _accept(ch):
                            seen_uids.add(ch.uid)
                            out.append(ch)
                return out

            seen_uids = set()
            for cnorm, chs in index.items():
                score = self._match_score(
                    search_keys, cnorm, settings)
                if score <= 0.0:
                    continue
                for ch in chs:
                    if ch.uid in seen_uids:
                        continue
                    if cancelled(stop_token):
                        break
                    if _accept(ch):
                        seen_uids.add(ch.uid)
                        out.append(ch)
            return out

        if settings.search_type == 'exact':
            for source in enabled:
                if cancelled(stop_token):
                    break
                self.load_links_from_source(
                    source, True, config, True, stop_token,
                    blacklist_manager=blacklist_manager,
                    domain_blacklist_manager=
                        domain_blacklist_manager,
                    domain_user_agent_manager=
                        domain_user_agent_manager,
                    settings=settings)
                with self._lock:
                    index = self._name_index.get(source.name)
                if index is None:
                    continue
                results.extend(
                    _collect_from_index(index, 'exact'))
        else:
            ex = self._get_search_pool()
            futures = {
                ex.submit(
                    self.load_links_from_source,
                    s, True, config, True, stop_token,
                    blacklist_manager=blacklist_manager,
                    domain_blacklist_manager=
                        domain_blacklist_manager,
                    domain_user_agent_manager=
                        domain_user_agent_manager,
                    settings=settings
                ): s
                for s in enabled
            }
            try:
                for fut in concurrent.futures.as_completed(
                        futures,
                        timeout=SEARCH_FUTURES_TIMEOUT_SEC):
                    if cancelled(stop_token):
                        continue
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(
                            "Source search failed")
                        continue
                    src = futures[fut]
                    with self._lock:
                        index = self._name_index.get(src.name)
                    if not index:
                        continue
                    results.extend(
                        _collect_from_index(
                            index, settings.search_type))
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "search_channel: timeout %ds, cancelling "
                    "pending futures",
                    SEARCH_FUTURES_TIMEOUT_SEC)
                for f in futures:
                    with suppress(Exception):
                        f.cancel()

        def key(ch: ChannelData):
            sp = self._source_priority(ch.link.link_source)
            ms = 0.0
            for k in ch.search_keys(settings):
                s = name_similarity(
                    ch.meta.name or "", k)
                if s > ms:
                    ms = s
            return (-sp, -ms)

        results.sort(key=key)
        seen: Set[str] = set()
        unique: List[ChannelData] = []
        for r in results:
            if r.link.url not in seen:
                seen.add(r.link.url)
                unique.append(r)
        return unique[:settings.max_alternative_urls]

    def find_matches_for_channel(
            self,
            channel: ChannelData,
            settings: LinkReplacementSettings,
            *,
            config: Optional[Config] = None,
            stop_token: Optional['_StopToken'] = None,
            only_working: bool = False,
            limit: Optional[int] = None,
            epg_db=None,
    ) -> List[ChannelData]:
        if channel is None:
            return []
        search_keys = channel.search_keys(
            settings, epg_db=epg_db)
        if not search_keys:
            return []
        if only_working:
            alive_urls = self.get_alive_urls_for_keys(
                search_keys, settings,
                limit=limit or settings.max_urls_to_check_per_channel)
            if alive_urls:
                by_url: Dict[str, ChannelData] = {}
                for src in self.get_enabled_sources():
                    with self._lock:
                        idx = self._name_index.get(src.name)
                    if not idx:
                        continue
                    for lst in idx.values():
                        for ch in lst:
                            u = ch.link.url
                            if u and u not in by_url:
                                by_url[u] = ch
                out: List[ChannelData] = []
                seen: Set[str] = set()
                for u in alive_urls:
                    if u in seen:
                        continue
                    seen.add(u)
                    ch = by_url.get(u)
                    if ch is not None:
                        out.append(ch)
                if out:
                    if limit is not None:
                        out = out[:limit]
                    return out
        matches = self.search_channel(
            search_keys, settings,
            stop_token=stop_token,
            config=config,
            blacklist_manager=settings.channel_blacklist_manager,
            domain_blacklist_manager=
                settings.domain_blacklist_manager,
            domain_user_agent_manager=
                settings.domain_user_agent_manager,
            only_working=only_working)
        if limit is not None:
            matches = matches[:limit]
        return matches