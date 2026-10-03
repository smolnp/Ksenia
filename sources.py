# -*- coding: utf-8 -*-
"""LinkSource, LinkSourceManager."""

from __future__ import annotations
import os
import re
import time
import threading
import concurrent.futures
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import OrderedDict, defaultdict
import requests
from difflib import SequenceMatcher
from config import Config, LinkReplacementSettings
from constants import (URL_CHECK_MAX_WORKERS, LOADED_CHANNELS_TTL_SEC,
    MAX_LOADED_SOURCES, MAX_SOURCE_FILE_BYTES, SEARCH_WORKER_MAX,
    SOURCE_LOAD_TIMEOUT_SEC, FALLBACK_DAYS_DEFAULT, DEFAULT_TIMEOUT,
    CHECK_RESULT_CACHE_TTL_HOURS, ALIVE_INDEX_CACHE_MAX, StatusText)
from models import ChannelData
from parsers import M3UParser
from paths import logger, parse_datetime


def _new_session() -> requests.Session:
    """Простая сессия, как в генераторе."""
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36',
        'Accept': '*/*',
    })
    return s

from storage import BaseJsonStore
from utils import ChannelNameNormalizer, URLUtils, _StopToken, cancelled


def _new_session() -> requests.Session:
    """Простая сессия, как в генераторе."""
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36',
        'Accept': '*/*',
    })
    return s


class LinkSource:
    """Источник ссылок (локальный файл или онлайн)."""

    __slots__ = ('name', 'path', 'source_type', 'last_updated', 'total_links',
                 'total_with_url', 'priority', 'enabled', 'auto_update',
                 'update_interval_hours', 'encoding',
                 'last_error', 'last_attempt', 'consecutive_errors',
                 'apply_blacklist', 'apply_domain_blacklist',
                 'total_working', 'working_checked_at',
                 'raw_total_links', 'raw_total_with_url')

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

    def should_refresh(self) -> bool:
        if not self.auto_update:
            return False
        if self.consecutive_errors > 0 and self.last_attempt:
            err_age_h = (datetime.now() - self.last_attempt).total_seconds() / 3600.0
            min_delay = min(8, 2 ** min(self.consecutive_errors, 3))
            if err_age_h < min_delay:
                return False
        if not self.last_updated:
            return True
        age_h = (datetime.now() - self.last_updated).total_seconds() / 3600.0
        return age_h >= self.update_interval_hours

    def copy(self) -> 'LinkSource':
        s = LinkSource()
        for slot in self.__slots__:
            v = getattr(self, slot)
            if isinstance(v, list):
                v = v.copy()
            setattr(s, slot, v)
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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LinkSource':
        s = cls()
        s.name = data.get('name', '') or ''
        s.path = data.get('path', '') or ''
        s.source_type = data.get('source_type', 'local') or 'local'
        for fld, default in (('total_links', 0), ('total_with_url', 0),
                             ('priority', 5), ('update_interval_hours', 24),
                             ('consecutive_errors', 0)):
            with suppress(ValueError, TypeError):
                setattr(s, fld, int(data.get(fld, default) or default))
        s.enabled = bool(data.get('enabled', True))
        s.auto_update = bool(data.get('auto_update', False))
        s.encoding = data.get('encoding', 'utf-8') or 'utf-8'
        s.last_error = data.get('last_error', '') or ''
        s.apply_blacklist = bool(data.get('apply_blacklist', True))
        s.apply_domain_blacklist = bool(data.get('apply_domain_blacklist', True))
        for fld in ('last_updated', 'last_attempt'):
            dt = parse_datetime(data.get(fld))
            if dt is not None:
                setattr(s, fld, dt)
        with suppress(ValueError, TypeError):
            s.total_working = int(data.get('total_working', 0) or 0)
        wca = data.get('working_checked_at')
        if wca is not None:
            with suppress(ValueError, TypeError):
                s.working_checked_at = float(wca)
        with suppress(ValueError, TypeError):
            s.raw_total_links = int(
                data.get('raw_total_links', s.total_links) or 0)
        with suppress(ValueError, TypeError):
            s.raw_total_with_url = int(
                data.get('raw_total_with_url', s.total_with_url) or 0)
        return s


class LinkSourceManager:
    """Менеджер источников: загрузка, индексы, поиск, alive_index."""

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
        self._persist()
        self._loaded: "OrderedDict[str, Tuple[float, str, List[ChannelData]]]" = OrderedDict()
        self._name_index: "OrderedDict[str, Dict[str, List[ChannelData]]]" = OrderedDict()
        self._alive_index: "OrderedDict[str, Dict[str, List[ChannelData]]]" = OrderedDict()
        self._alive_index_cache: "OrderedDict[str, List[str]]" = OrderedDict()
        self._search_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._search_pool_lock = threading.Lock()

    def _get_search_pool(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._search_pool_lock:
            if self._search_pool is None:
                self._search_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=SEARCH_WORKER_MAX,
                    thread_name_prefix="search")
            return self._search_pool

    def shutdown(self):
        with self._search_pool_lock:
            pool = self._search_pool
            self._search_pool = None
        if pool is not None:
            with suppress(TypeError):
                pool.shutdown(wait=False, cancel_futures=True)
                return
            pool.shutdown(wait=False)

    def invalidate_cache(self, source_name: Optional[str] = None):
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

    def _persist(self) -> bool:
        with self._lock:
            self._store._data = [s.to_dict() for s in self._sources]
        return self._store.save()

    def add_source(self, source: LinkSource) -> bool:
        with self._lock:
            for s in self._sources:
                if s.name == source.name:
                    return False
            self._sources.append(source)
            self._alive_index_cache.clear()
        return self._persist()

    def remove_source(self, source_name: str) -> bool:
        with self._lock:
            self._sources = [s for s in self._sources if s.name != source_name]
            self._loaded.pop(source_name, None)
            self._name_index.pop(source_name, None)
            self._alive_index.pop(source_name, None)
            self._alive_index_cache.clear()
        return self._persist()

    def update_source(self, old_name: str, new_source: LinkSource) -> bool:
        with self._lock:
            found = False
            for i, s in enumerate(self._sources):
                if s.name == old_name:
                    self._sources[i] = new_source
                    self._loaded.pop(old_name, None)
                    self._name_index.pop(old_name, None)
                    self._alive_index.pop(old_name, None)
                    self._loaded.pop(new_source.name, None)
                    self._name_index.pop(new_source.name, None)
                    self._alive_index.pop(new_source.name, None)
                    self._alive_index_cache.clear()
                    found = True
                    break
            if not found:
                return False
        return self._persist()

    def get_all_sources(self) -> List[LinkSource]:
        with self._lock:
            return list(self._sources)

    def get_enabled_sources(self) -> List[LinkSource]:
        with self._lock:
            return [s for s in self._sources if s.enabled]

    def get_source_by_name(self, name: str) -> Optional[LinkSource]:
        with self._lock:
            for s in self._sources:
                if s.name == name:
                    return s
        return None

    def update_source_health(self, name: str, working: int,
                             checked_at: Optional[float] = None):
        with self._lock:
            for s in self._sources:
                if s.name == name:
                    s.total_working = max(0, int(working))
                    s.working_checked_at = (float(checked_at)
                                            if checked_at is not None
                                            else time.time())
                    break
            else:
                return
        self._persist()

    @staticmethod
    def _detect_encoding(content: bytes) -> str:
        for enc in ('utf-8', 'windows-1251', 'cp1251', 'koi8-r', 'cp866'):
            try:
                content.decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
        return 'utf-8'

    @staticmethod
    def _try_previous_days(url: str, days: int = FALLBACK_DAYS_DEFAULT,
                           stop_token: Optional['_StopToken'] = None
                           ) -> Optional[str]:
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', url)
        if not m:
            return None
        try:
            base = datetime.strptime(m.group(0), "%Y-%m-%d")
        except ValueError:
            return None
        session = _new_session()
        for d in range(1, days + 1):
            if cancelled(stop_token):
                return None
            prev = (base - timedelta(days=d)).strftime("%Y-%m-%d")
            candidate = url.replace(m.group(0), prev, 1)
            try:
                with session.get(candidate, timeout=DEFAULT_TIMEOUT,
                                 verify=False, stream=True) as r:
                    if r.status_code == 200:
                        return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _post_process_channels(channels: List[ChannelData],
                               source: LinkSource) -> List[ChannelData]:
        try:
            from ksenia_window import ApplicationCore
            core = ApplicationCore.instance()
        except Exception:
            core = None

        if core is not None:
            if source.apply_blacklist and core.blacklist_manager.get_all():
                channels, _ = core.apply_blacklist_to_channels(channels)
            if (source.apply_domain_blacklist
                    and core.domain_blacklist_manager.get_all()):
                channels, _ = core.apply_domain_blacklist_clean(channels)

        for ch in channels:
            ch.link.link_source = source.name
            if not ch.link.extinf:
                ch.update_extinf()
            ch.parse_extvlcopt_headers()

        if core is not None and core.domain_user_agent_manager.get_all_rules():
            core.apply_domain_user_agent(channels)
        return channels

    @staticmethod
    def _is_url_blocked(url: str, bl_mgr,
                        temp_domains: List[str],
                        unsafe_domains: List[str]) -> bool:
        if bl_mgr is not None and bl_mgr.matches_url(url):
            return True
        try:
            host = URLUtils.extract_host(url) or ''
        except Exception:
            host = ''
        if not host:
            return False
        for d in temp_domains + unsafe_domains:
            dn = (d or '').strip().lower().strip('.')
            if dn and (host == dn or host.endswith('.' + dn)):
                return True
        return False

    @staticmethod
    def _get_blocking_context():
        settings = None
        bl_mgr = None
        try:
            from ksenia_window import ApplicationCore
            core = ApplicationCore.instance()
            settings = core.get_replacement_settings()
            bl_mgr = core.domain_blacklist_manager
        except Exception:
            pass
        temp_domains = list(settings.temporary_domains) if settings else []
        unsafe_domains = list(settings.unsafe_domains) if settings else []
        return bl_mgr, temp_domains, unsafe_domains

    def _build_alive_index(self, channels: List[ChannelData],
                           bl_mgr,
                           temp_domains: List[str],
                           unsafe_domains: List[str]
                           ) -> Dict[str, List[ChannelData]]:
        alive_index: Dict[str, List[ChannelData]] = defaultdict(list)
        if self.cache_manager is None or not channels:
            return dict(alive_index)

        pairs: List[Tuple[str, str]] = []
        ch_by_key: Dict[Tuple[str, str], List[ChannelData]] = defaultdict(list)
        for ch in channels:
            url = ch.link.url
            if not url:
                continue
            if self._is_url_blocked(url, bl_mgr, temp_domains, unsafe_domains):
                continue
            key = (ch.meta.name.lower(), url)
            pairs.append(key)
            ch_by_key[key].append(ch)

        if not pairs:
            return dict(alive_index)

        try:
            cached = self.cache_manager.get_check_results_batch(
                pairs, max_age_hours=CHECK_RESULT_CACHE_TTL_HOURS)
        except Exception:
            logger.exception("alive_index batch read")
            cached = {}

        for key, info in cached.items():
            if not info.get('alive'):
                continue
            for ch in ch_by_key.get(key, ()):
                norm = ch.normalized_name()
                if norm:
                    alive_index[norm].append(ch)
        return dict(alive_index)

    def _store_loaded(self, name: str, path: str,
                      channels: List[ChannelData]):
        index: Dict[str, List[ChannelData]] = defaultdict(list)
        for ch in channels:
            norm = ch.normalized_name()
            if norm:
                index[norm].append(ch)

        bl_mgr, temp_domains, unsafe_domains = self._get_blocking_context()
        alive_index = self._build_alive_index(
            channels, bl_mgr, temp_domains, unsafe_domains)

        with self._lock:
            self._loaded[name] = (time.time(), path, channels)
            self._loaded.move_to_end(name)
            self._name_index[name] = dict(index)
            self._name_index.move_to_end(name)
            self._alive_index[name] = alive_index
            self._alive_index.move_to_end(name)
            self._alive_index_cache.clear()
            while len(self._loaded) > MAX_LOADED_SOURCES:
                old_name, _ = self._loaded.popitem(last=False)
                self._name_index.pop(old_name, None)
                self._alive_index.pop(old_name, None)

    def rebuild_alive_index(self, source_name: Optional[str] = None) -> int:
        """Перестроить _alive_index из url_status_cache без сети."""
        if self.cache_manager is None:
            return 0

        bl_mgr, temp_domains, unsafe_domains = self._get_blocking_context()

        with self._lock:
            if source_name is None:
                names = list(self._loaded.keys())
            else:
                names = [source_name] if source_name in self._loaded else []

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
                channels, bl_mgr, temp_domains, unsafe_domains)

            with self._lock:
                self._alive_index[name] = alive_index
                self._alive_index.move_to_end(name)
                self._alive_index_cache.clear()
            rebuilt += 1

        with self._lock:
            self._alive_index_cache.clear()
        logger.info(
            f"rebuild_alive_index: перестроено {rebuilt} источников "
            f"(source={source_name or 'ALL'})")
        return rebuilt

    def load_links_from_source(self, source: LinkSource,
                               use_cache: bool = True,
                               config: Optional[Config] = None,
                               update_meta: bool = True,
                               stop_token: Optional['_StopToken'] = None
                               ) -> List[ChannelData]:
        if use_cache:
            with self._lock:
                entry = self._loaded.get(source.name)
                if entry:
                    ts, cached_path, chs = entry
                    if (time.time() - ts < LOADED_CHANNELS_TTL_SEC
                            and cached_path == source.path):
                        return chs

        channels: List[ChannelData] = []
        source.last_attempt = datetime.now()
        try:
            if source.source_type == "local":
                channels = self._load_local(source)
            else:
                channels = self._load_remote(source, use_cache, config, stop_token)
            if channels:
                source.last_error = ""
                source.consecutive_errors = 0
            else:
                if source.last_error == StatusText.CANCELLED:
                    return []
                if not source.last_error:
                    source.last_error = "Пустой результат"
                source.consecutive_errors += 1
            if update_meta:
                self._finalize_source(source, channels)
            self._store_loaded(source.name, source.path, channels)
        except Exception as e:
            source.last_error = str(e)[:200]
            source.last_attempt = datetime.now()
            source.consecutive_errors += 1
            self._persist()
            logger.exception(f"Ошибка загрузки источника {source.name}")
            return []
        return channels

    def _load_local(self, source: LinkSource) -> List[ChannelData]:
        if not os.path.exists(source.path):
            source.last_error = f"Файл не найден: {source.path}"
            return []
        try:
            if os.path.getsize(source.path) > MAX_SOURCE_FILE_BYTES:
                source.last_error = "Файл слишком большой"
                return []
            with open(source.path, 'rb') as f:
                raw = f.read()
        except PermissionError as e:
            source.last_error = f"Нет доступа: {e}"
            return []
        except OSError as e:
            source.last_error = f"Ошибка чтения: {e}"
            return []
        enc = source.encoding or self._detect_encoding(raw)
        try:
            content = raw.decode(enc, errors='replace')
        except (LookupError, UnicodeDecodeError):
            content = raw.decode('utf-8', errors='replace')
        parsed = M3UParser.parse(content, source.name)

        source.raw_total_links = len(parsed)
        source.raw_total_with_url = sum(
            1 for c in parsed if c.has_valid_url)
        return self._post_process_channels(parsed, source)

    def _load_remote(self, source: LinkSource, use_cache: bool,
                     config: Optional[Config],
                     stop_token: Optional['_StopToken'] = None
                     ) -> List[ChannelData]:
        if use_cache:
            with self._lock:
                entry = self._loaded.get(source.name)
                if entry:
                    ts, cached_path, chs = entry
                    if (time.time() - ts < LOADED_CHANNELS_TTL_SEC
                            and cached_path == source.path):
                        return chs
        if (use_cache and config and self.cache_manager is not None
                and config.get('use_link_cache', True)):
            cached = self.cache_manager.get_link_cache(
                source.path, config.get('link_cache_hours', 6))
            if cached:
                chs = self._dicts_to_channels(cached, source.name)
                source.raw_total_links = len(chs)
                source.raw_total_with_url = sum(
                    1 for c in chs if c.has_valid_url)
                logger.info(f"LinkCache hit: {len(chs)} из {source.name}")
                return self._post_process_channels(chs, source)

        channels: List[ChannelData] = []
        session = _new_session()
        primary_error = ""
        was_stopped = False
        try:
            with session.get(source.path, timeout=SOURCE_LOAD_TIMEOUT_SEC,
                             verify=False, stream=True) as r:
                if r.status_code == 200:
                    buf = bytearray()
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if cancelled(stop_token):
                            was_stopped = True
                            break
                        buf.extend(chunk)
                        if len(buf) > MAX_SOURCE_FILE_BYTES:
                            primary_error = "Файл слишком большой"
                            break
                    if not primary_error and not was_stopped:
                        content = bytes(buf).decode('utf-8', errors='replace')
                        channels = M3UParser.parse(content, source.name)
                else:
                    primary_error = f"HTTP {r.status_code}"
        except Exception as e:
            primary_error = str(e)[:100]
            logger.exception(f"Ошибка загрузки {source.name}")

        if was_stopped:
            source.last_error = StatusText.CANCELLED
            logger.info(f"Загрузка {source.name} прервана, кеш не обновляем")
            return []

        if not channels:
            days_raw = config.get('days_to_check', FALLBACK_DAYS_DEFAULT) \
                if config else FALLBACK_DAYS_DEFAULT
            try:
                days = int(days_raw)
            except (ValueError, TypeError):
                days = FALLBACK_DAYS_DEFAULT
            fb = self._try_previous_days(source.path, days, stop_token)
            if fb:
                try:
                    with session.get(fb, timeout=SOURCE_LOAD_TIMEOUT_SEC,
                                     verify=False) as r:
                        if r.status_code == 200:
                            channels = M3UParser.parse(r.text, source.name)
                            logger.info(f"Fallback: {fb}")
                except Exception as e:
                    source.last_error = f"Fallback: {str(e)[:100]}"
            if not channels and primary_error:
                source.last_error = primary_error

        source.raw_total_links = len(channels)
        source.raw_total_with_url = sum(
            1 for c in channels if c.has_valid_url)

        channels = self._post_process_channels(channels, source)

        if (channels and use_cache and config
                and self.cache_manager is not None
                and config.get('use_link_cache', True)):
            self.cache_manager.put_link_cache(
                source.path, [c.to_dict() for c in channels])
        return channels

    def _finalize_source(self, source: LinkSource,
                         channels: List[ChannelData]):
        source.total_links = len(channels)
        source.total_with_url = sum(1 for c in channels if c.has_valid_url)
        source.last_updated = datetime.now()
        self._persist()

    @classmethod
    def _dicts_to_channels(cls, dicts: List[Dict[str, Any]],
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

    def has_alive_index(self) -> bool:
        with self._lock:
            for idx in self._alive_index.values():
                if idx:
                    return True
        return False

    def get_alive_urls(self, channel_name: str,
                       settings: LinkReplacementSettings,
                       limit: Optional[int] = None) -> List[str]:
        """URL из _alive_index БЕЗ проверки сети. Мемоизировано (LRU)."""
        if not channel_name:
            return []
        if settings.ignore_special_chars_in_names:
            search_norm = ChannelNameNormalizer.normalize(
                channel_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names)
        else:
            search_norm = channel_name.lower()
        if not search_norm:
            return []
        if limit is None:
            limit = max(1, int(settings.max_urls_to_check_per_channel))

        with self._lock:
            cached = self._alive_index_cache.get(search_norm)
            if cached is not None:
                self._alive_index_cache.move_to_end(search_norm)
                return list(cached[:limit])

        results: List[str] = []
        seen: Set[str] = set()
        for source in self.get_enabled_sources():
            if len(results) >= limit:
                break
            with self._lock:
                alive_index = self._alive_index.get(source.name)
            if not alive_index:
                continue
            for ch in alive_index.get(search_norm, ()):
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

        with self._lock:
            self._alive_index_cache[search_norm] = results
            self._alive_index_cache.move_to_end(search_norm)
            while len(self._alive_index_cache) > ALIVE_INDEX_CACHE_MAX:
                self._alive_index_cache.popitem(last=False)
        return list(results)

    def search_channel(self, channel_name: str,
                       settings: LinkReplacementSettings,
                       stop_token: Optional['_StopToken'] = None,
                       config: Optional[Config] = None
                       ) -> List[ChannelData]:
        results: List[ChannelData] = []
        enabled = self.get_enabled_sources()

        if settings.ignore_special_chars_in_names:
            search_norm = ChannelNameNormalizer.normalize(
                channel_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names)
        else:
            search_norm = channel_name.lower()

        search_type = settings.search_type

        if search_type == 'exact':
            for source in enabled:
                if cancelled(stop_token):
                    break
                self.load_links_from_source(
                    source, True, config, True, stop_token)
                with self._lock:
                    index = self._name_index.get(source.name)
                if index is None:
                    continue
                for ch in index.get(search_norm, []):
                    if not ch.link.url or not ch.link.url.strip():
                        continue
                    if settings.is_blacklisted(ch.link.url):
                        continue
                    if settings.is_filtered_domain(ch.link.url):
                        continue
                    results.append(ch)
        else:
            ex = self._get_search_pool()
            futures = {ex.submit(self.load_links_from_source, s, True,
                                 config, True, stop_token): s
                       for s in enabled}
            for fut in concurrent.futures.as_completed(futures):
                if cancelled(stop_token):
                    for f in futures:
                        f.cancel()
                    break
                try:
                    channels = fut.result()
                except Exception as e:
                    logger.exception(f"Источник: {e}")
                    continue
                for ch in channels:
                    if not ch.link.url or not ch.link.url.strip():
                        continue
                    if settings.is_blacklisted(ch.link.url):
                        continue
                    if settings.is_filtered_domain(ch.link.url):
                        continue
                    cnorm = ch.normalized_name()
                    if self._is_match_normalized(search_norm, cnorm, settings):
                        results.append(ch)

        def key(ch: ChannelData):
            sp = self._source_priority(ch.link.link_source)
            ms = ChannelNameNormalizer.similarity(channel_name, ch.meta.name)
            return (-sp, -ms)

        results.sort(key=key)
        seen: Set[str] = set()
        unique: List[ChannelData] = []
        for r in results:
            if r.link.url not in seen:
                seen.add(r.link.url)
                unique.append(r)
        return unique[:settings.max_alternative_urls]

    def _is_match_normalized(self, search_norm: str, cnorm: str,
                             settings: LinkReplacementSettings) -> bool:
        search_type = settings.search_type
        if search_type == "exact":
            return search_norm == cnorm
        threshold = max(0.0, min(1.0, settings.match_threshold_percent / 100.0))
        if search_type == "similar":
            if not settings.use_fuzzy_matching:
                return search_norm == cnorm
            ratio = SequenceMatcher(None, search_norm, cnorm).ratio()
            return ratio >= max(settings.min_name_similarity, threshold)
        if search_type == "fuzzy":
            if not settings.use_fuzzy_matching:
                return search_norm == cnorm
            return ChannelNameNormalizer.jaccard(
                search_norm, cnorm) >= max(
                settings.min_name_similarity, threshold)
        return False

    def _is_match(self, search_norm: str, channel_name: str,
                  settings: LinkReplacementSettings) -> bool:
        if settings.ignore_special_chars_in_names:
            cnorm = ChannelNameNormalizer.normalize(
                channel_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names)
        else:
            cnorm = channel_name.lower()
        return self._is_match_normalized(search_norm, cnorm, settings)

    def _source_priority(self, source_name: str) -> int:
        s = self.get_source_by_name(source_name)
        return s.priority if s else 5