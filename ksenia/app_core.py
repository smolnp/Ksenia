# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""ApplicationCore — глобальное состояние приложения."""
from __future__ import annotations

import os
import threading
import uuid
import weakref
from contextlib import suppress
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from blacklists import (BlacklistManager, DomainBlacklistManager,
                        DomainUserAgentManager)
from config import Config, LinkReplacementSettings
from constants import (APP_VERSION, CHECK_RESULT_CACHE_TTL_HOURS,
                        EPG_CACHE_TTL_HOURS, DEFAULT_EPG_URL)
from epg import EPGDatabase
from group_templates import GroupTemplateManager
from logging_setup import logger
from models import ChannelData, LinkQuality
from paths import Paths
from sources import LinkSource, LinkSourceManager
from storage import CacheManager, StableStateManager
from theme import invalidate_cache as theme_invalidate
from workers import SourcesRefreshWorker


class _WorkerRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._threads: List[Any] = []
        self._runnables: List[Any] = []

    def add_thread(self, w):
        with self._lock:
            self._threads.append(w)

    def remove_thread(self, w):
        with self._lock:
            if w in self._threads:
                self._threads.remove(w)

    def add_runnable(self, r):
        with self._lock:
            self._runnables.append(r)

    def remove_runnable(self, r):
        with self._lock:
            if r in self._runnables:
                self._runnables.remove(r)

    def stop_all(self):
        with self._lock:
            threads = list(self._threads)
            runnables = list(self._runnables)
            self._threads.clear()
            self._runnables.clear()
        for w in threads:
            with suppress(Exception):
                if w.isRunning():
                    w.stop()
                    if not w.wait(2000):
                        logger.warning(
                            "Worker thread did not stop: %s", w)
        for r in runnables:
            with suppress(Exception):
                stop = getattr(r, 'stop', None)
                if callable(stop):
                    stop()


class ApplicationCore(QObject):
    channels_updated = pyqtSignal(str)
    sources_updated = pyqtSignal()
    settings_changed = pyqtSignal()
    blacklist_updated = pyqtSignal()
    domain_blacklist_updated = pyqtSignal()
    epg_updated = pyqtSignal()
    playlist_from_sources_ready = pyqtSignal(list)
    playlist_from_sources_failed = pyqtSignal(str)
    group_templates_updated = pyqtSignal()
    # FIX 3: сигнал для фоновой перестройки alive-индекса.
    alive_index_rebuild_started = pyqtSignal(str)
    alive_index_rebuild_finished = pyqtSignal(str, int)

    _instance: Optional['ApplicationCore'] = None
    _instance_lock = threading.RLock()

    def __init__(self):
        super().__init__()
        self._alive_rebuild_lock = threading.RLock()
        self._alive_rebuild_pending = False
        self._alive_rebuild_thread: Optional[threading.Thread] = None
        self._initialize()

    @classmethod
    def instance(cls) -> 'ApplicationCore':
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def try_get_if_gui_thread(cls) -> Optional['ApplicationCore']:
        app = QApplication.instance()
        if app is None:
            return None
        from PyQt6.QtCore import QThread
        if QThread.currentThread() != app.thread():
            return None
        return cls._instance

    def _initialize(self):
        config_dir = Paths.get_config_dir()
        os.makedirs(config_dir, exist_ok=True)
        self.config = Config()
        self.blacklist_manager = BlacklistManager(config_dir)
        self.domain_blacklist_manager = (
            DomainBlacklistManager(config_dir))
        self.domain_user_agent_manager = (
            DomainUserAgentManager(config_dir))

        self.cache_manager = (
            CacheManager()
            if self.config.get('use_cache_manager', True)
            else None)
        self.link_source_manager = LinkSourceManager(
            config_dir, self.cache_manager)
        self.link_replacement_settings = (
            LinkReplacementSettings(self.config))
        self._bind_managers()
        self.stable_state_manager = StableStateManager(
            config_dir)

        self.group_template_manager = GroupTemplateManager(
            config_dir)

        if self.cache_manager:
            try:
                self.cache_manager.configure_link_cache(
                    max_files=int(self.config.get(
                        'link_cache_max_files', 10000)),
                    max_mb=int(self.config.get(
                        'link_cache_max_mb', 64)))
            except Exception:
                logger.exception("configure_link_cache failed")

        self.epg_db = EPGDatabase(self.cache_manager)

        self._tab_lock = threading.RLock()
        self._tabs: Dict[str, weakref.ref] = {}
        self._tab_metadata: Dict[str, Dict[str, Any]] = {}
        self._tab_revision: Dict[str, int] = {}
        self._stats_cache: Dict[str, Tuple[int,
                                            Dict[str, Any]]] = {}

        self._workers = _WorkerRegistry()

        if self.cache_manager:
            try:
                removed = self.cache_manager.cleanup_old(
                    self.config.get('cache_cleanup_days', 30))
                if removed:
                    logger.info(
                        "Cleaned %d old cache entries", removed)
            except Exception:
                logger.exception("cleanup_old failed")

        self.sources_updated.connect(self._on_sources_updated)

        self._auto_update_timer = QTimer(self)
        self._auto_update_timer.timeout.connect(
            self._check_auto_update)
        self.apply_auto_update_setting()

        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._on_app_quit)
            theme_invalidate()

        logger.info("ApplicationCore %s initialized",
                    APP_VERSION)

    def _bind_managers(self):
        bind = getattr(self.link_replacement_settings,
                       'bind_managers', None)
        if bind is None:
            return
        try:
            bind(
                domain_blacklist_manager=(
                    self.domain_blacklist_manager),
                blacklist_manager=self.blacklist_manager,
                domain_user_agent_manager=(
                    self.domain_user_agent_manager))
        except Exception:
            logger.exception("_bind_managers")

    # ── FIX 3: фоновая перестройка alive-индекса ──

    def schedule_alive_index_rebuild(self, reason: str = "") -> None:
        """Запускает перестройку alive-индекса в фоне.

        Если перестройка уже запланирована или выполняется —
        просто помечает «ещё раз», чтобы не запускать параллельно.
        """
        with self._alive_rebuild_lock:
            if self._alive_rebuild_pending:
                logger.debug(
                    "alive_index_rebuild already pending (%s)",
                    reason)
                return
            self._alive_rebuild_pending = True

        def _worker():
            try:
                self.alive_index_rebuild_started.emit(reason)
                settings = self.get_replacement_settings()
                try:
                    rebuilt = self.link_source_manager \
                        .rebuild_alive_index(settings=settings)
                except Exception:
                    logger.exception(
                        "rebuild_alive_index (bg, %s)", reason)
                    rebuilt = 0
                self.alive_index_rebuild_finished.emit(
                    reason, rebuilt)
            finally:
                with self._alive_rebuild_lock:
                    self._alive_rebuild_pending = False
                    self._alive_rebuild_thread = None

        t = threading.Thread(
            target=_worker, name="alive-rebuild", daemon=True)
        with self._alive_rebuild_lock:
            self._alive_rebuild_thread = t
        t.start()

    def _on_sources_updated(self):
        try:
            self.link_source_manager.invalidate_cache()
        except Exception:
            logger.exception("invalidate_cache")
        # После изменения списка источников индекс устарел —
        # запланируем перестройку в фоне.
        self.schedule_alive_index_rebuild("sources_updated")

    def _on_app_quit(self):
        with suppress(Exception):
            self._auto_update_timer.stop()
        self._workers.stop_all()
        try:
            self.link_source_manager.shutdown()
        except Exception:
            logger.exception("link_source_manager.shutdown")
        if self.cache_manager:
            try:
                self.cache_manager.cleanup_old(
                    self.config.get('cache_cleanup_days', 30))
                self.cache_manager.vacuum()
            except Exception:
                logger.exception("cache shutdown")

    def _check_auto_update(self):
        sources = self.link_source_manager.get_all_sources()
        to_update = [s for s in sources
                     if s.enabled and s.auto_update
                     and s.should_refresh()]
        if not to_update:
            return
        snap = self._build_settings_snapshot()
        try:
            bl_index = self.blacklist_manager.build_index()
        except Exception:
            bl_index = None
        settings = self.get_replacement_settings()
        worker = SourcesRefreshWorker(
            to_update, self.link_source_manager, self.config,
            check_urls=True, settings_snapshot=snap,
            blacklist_index=bl_index,
            settings=settings)
        core_ref = weakref.ref(self)

        def _on_all_done(s, t):
            core = core_ref()
            if core is None:
                return
            with suppress(Exception):
                if s:
                    core.sources_updated.emit()

        worker.all_done.connect(_on_all_done)
        self._workers.add_thread(worker)
        worker.finished.connect(
            lambda w=worker: self._workers.remove_thread(w))
        worker.start()

    def get_default_epg_url(self) -> str:
        try:
            v = self.config.get('default_epg_url', DEFAULT_EPG_URL)
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            logger.exception("get_default_epg_url")
        return DEFAULT_EPG_URL

    def resolve_epg_urls(self,
                         header_epg_sources: Optional[List[str]] = None
                         ) -> List[str]:
        urls = [u for u in (header_epg_sources or [])
                if u and u.strip()]
        if urls:
            return urls
        d = self.get_default_epg_url()
        return [d] if d else []

    def ensure_epg_loaded_async(
            self, urls: List[str],
            on_done: Optional[Callable[[int, List[str]], None]] = None,
            on_error: Optional[Callable[[str], None]] = None,
            timeout: int = 20) -> None:
        urls = [u for u in urls if u and u.strip()]
        if not urls:
            d = self.get_default_epg_url()
            urls = [d] if d else []

        if self.epg_db.has_channel_info:
            if on_done is not None:
                on_done(0, [])
            return
        if not urls:
            if on_error is not None:
                on_error("EPG не задан (default_epg_url пуст)")
            return

        from workers import EPGLoaderWorker
        worker = EPGLoaderWorker(urls, self.epg_db,
                                  timeout=timeout)
        self._workers.add_thread(worker)
        core_ref = weakref.ref(self)

        def _on_loaded(count: int, errors: list):
            core = core_ref()
            if core is None:
                return
            try:
                core.epg_updated.emit()
            except Exception:
                logger.exception("epg_updated.emit")
            if on_done is not None:
                try:
                    on_done(count, list(errors or []))
                except Exception:
                    logger.exception("ensure_epg on_done")

        def _on_error(msg: str):
            if on_error is not None:
                try:
                    on_error(msg)
                except Exception:
                    logger.exception("ensure_epg on_error")

        worker.epg_loaded.connect(_on_loaded)
        worker.error.connect(_on_error)
        worker.finished.connect(
            lambda w=worker: self._workers.remove_thread(w))
        worker.start()

    def get_normalized_index_stats(
            self, max_age_hours: int = 24,
    ) -> Dict[str, Any]:
        settings = self.get_replacement_settings()
        try:
            return self.link_source_manager.get_normalized_index_stats(
                settings=settings,
                max_age_hours=max_age_hours)
        except Exception:
            logger.exception("get_normalized_index_stats")
            return {'count': 0, 'sources': 0, 'fresh_count': 0,
                    'is_fresh': False, 'settings_match': False}

    def has_normalized_index(
            self, max_age_hours: int = 24,
    ) -> bool:
        settings = self.get_replacement_settings()
        try:
            return self.link_source_manager.has_normalized_index(
                settings=settings, max_age_hours=max_age_hours)
        except Exception:
            logger.exception("has_normalized_index")
            return False

    def _build_settings_snapshot(self) -> Dict[str, Any]:
        return {
            'use_ip_filtering': bool(self.config.get(
                'use_ip_filtering', True)),
            'temporary_domains': list(self.config.get(
                'temporary_domains', [])),
            'unsafe_domains': list(self.config.get(
                'unsafe_domains', [])),
            '_domain_bl_manager': self.domain_blacklist_manager,
        }

    def apply_auto_update_setting(self):
        try:
            enabled = bool(self.config.get(
                'auto_update_sources', True))
            if enabled and not self._auto_update_timer.isActive():
                self._auto_update_timer.start(60 * 60 * 1000)
            elif not enabled and self._auto_update_timer.isActive():
                self._auto_update_timer.stop()
        except Exception:
            logger.exception("apply_auto_update_setting")

    def set_cache_manager_enabled(self, enabled: bool):
        if enabled and self.cache_manager is None:
            try:
                self.cache_manager = CacheManager()
                self.link_source_manager.cache_manager = (
                    self.cache_manager)
                self.epg_db.cache_manager = self.cache_manager
            except Exception:
                logger.exception("Failed to create CacheManager")
                self.cache_manager = None
        elif not enabled and self.cache_manager is not None:
            try:
                self._workers.stop_all()
            except Exception:
                logger.exception("stop_all before cache disable")
            try:
                self.cache_manager.close_thread_connection()
            except Exception:
                logger.exception("close_thread_connection")
            try:
                self.cache_manager.reset_all_connections()
            except Exception:
                logger.exception("reset_all_connections")
            self.cache_manager = None
            self.link_source_manager.cache_manager = None
            self.epg_db.cache_manager = None

    def load_epg_from_cache_async(self):
        if self.cache_manager is None:
            return
        try:
            ttl = int(self.config.get(
                'epg_cache_ttl_hours', EPG_CACHE_TTL_HOURS))
        except (ValueError, TypeError):
            ttl = EPG_CACHE_TTL_HOURS
        core_ref = weakref.ref(self)

        def _worker():
            try:
                core = core_ref()
                if core is None:
                    return
                cnt = core.epg_db.load_from_cache(ttl)
                if cnt:
                    logger.info(
                        "EPG loaded from cache: %d programs", cnt)
            except Exception as e:
                logger.debug("EPG cache load: %s", e)

        threading.Thread(target=_worker, daemon=True).start()

    def register_tab(self, tab) -> str:
        tid = uuid.uuid4().hex
        with self._tab_lock:
            self._tabs[tid] = weakref.ref(tab)
            self._tab_metadata[tid] = {
                'filepath': getattr(tab, 'filepath', None),
                'created': datetime.now(),
                'modified': False,
                'channels_count': 0,
            }
            self._tab_revision[tid] = 0
        return tid

    def unregister_tab(self, tid: str) -> bool:
        with self._tab_lock:
            if tid in self._tabs:
                del self._tabs[tid]
                self._tab_metadata.pop(tid, None)
                self._tab_revision.pop(tid, None)
                self._stats_cache.pop(tid, None)
                return True
        return False

    def get_tab(self, tid: str):
        with self._tab_lock:
            ref = self._tabs.get(tid)
        return ref() if ref else None

    def get_all_tabs(self) -> List[Any]:
        with self._tab_lock:
            refs = list(self._tabs.values())
        return [r() for r in refs if r() is not None]

    def update_tab_metadata(self, tid: str, **kw):
        with self._tab_lock:
            if tid in self._tab_metadata:
                self._tab_metadata[tid].update(kw)

    def bump_tab_revision(self, tid: str):
        with self._tab_lock:
            self._tab_revision[tid] = \
                self._tab_revision.get(tid, 0) + 1
            self._stats_cache.pop(tid, None)

    def update_channels(self, tid: str,
                        channels: List[ChannelData]) -> None:
        with self._tab_lock:
            ref = self._tabs.get(tid)
            tab = ref() if ref else None
            if tab is None:
                return
            tab.all_channels = channels
            self._tab_metadata[tid]['modified'] = True
            self._tab_metadata[tid]['channels_count'] = len(
                channels)
            self._tab_revision[tid] = \
                self._tab_revision.get(tid, 0) + 1
            self._stats_cache.pop(tid, None)
        self.channels_updated.emit(tid)

    def get_stats(self, tid: str) -> Dict[str, Any]:
        with self._tab_lock:
            rev = self._tab_revision.get(tid, 0)
            cached = self._stats_cache.get(tid)
            if cached and cached[0] == rev:
                return cached[1]
            ref = self._tabs.get(tid)
            tab = ref() if ref else None
        if tab is None:
            return {'total': 0, 'with_url': 0, 'without_url': 0,
                    'working': 0, 'not_working': 0, 'unknown': 0,
                    'groups': 0, 'unsupported': 0}
        channels = tab.all_channels
        total = len(channels)
        with_url = working = not_working = unknown = \
            unsupported = 0
        groups_set: Set[str] = set()
        # FIX 4: unknown считается отдельно от unsupported,
        # чтобы working + not_working + unknown + unsupported
        # сходилось с with_url.
        for ch in channels:
            has = ch.has_valid_url
            if has:
                with_url += 1
            if ch.status.url_status is True:
                working += 1
            elif ch.status.url_status is False:
                not_working += 1
            else:
                # url_status is None
                if has:
                    if ch.status.link_quality == \
                            LinkQuality.UNSUPPORTED:
                        unsupported += 1
                    else:
                        unknown += 1
            if ch.meta.group:
                groups_set.add(ch.meta.group)
        stats = {
            'total': total, 'with_url': with_url,
            'without_url': total - with_url,
            'working': working, 'not_working': not_working,
            'unknown': unknown, 'unsupported': unsupported,
            'groups': len(groups_set),
        }
        with self._tab_lock:
            self._stats_cache[tid] = (rev, stats)
        return stats

    def get_sources(self) -> List[LinkSource]:
        return self.link_source_manager.get_all_sources()

    def search_in_sources(
            self, channel_name: str,
            settings: Optional[LinkReplacementSettings] = None,
            epg_db: Optional[EPGDatabase] = None
    ) -> List[ChannelData]:
        s = settings or self.link_replacement_settings
        self._bind_managers()
        tmp = ChannelData()
        tmp.meta.name = channel_name
        return self.link_source_manager.find_matches_for_channel(
            tmp, s,
            config=self.config,
            only_working=False,
            epg_db=epg_db if epg_db is not None else self.epg_db)

    # ── ЧС каналов ──

    def add_to_blacklist(self, name: str,
                         tvg_id: str = "") -> bool:
        r = self.blacklist_manager.add_channel(name, tvg_id)
        if r:
            self.blacklist_updated.emit()
            # FIX 3: перестройка alive-индекса — в фоне.
            self.schedule_alive_index_rebuild("add_to_blacklist")
        return r

    def remove_from_blacklist(self, name: str,
                              tvg_id: str = "") -> bool:
        r = self.blacklist_manager.remove_channel(name, tvg_id)
        if r:
            self.blacklist_updated.emit()
            self.schedule_alive_index_rebuild(
                "remove_from_blacklist")
        return r

    def get_blacklist(self) -> List[Dict[str, str]]:
        return self.blacklist_manager.get_all()

    def clear_blacklist(self) -> bool:
        self.blacklist_manager.clear()
        self.blacklist_updated.emit()
        self.schedule_alive_index_rebuild("clear_blacklist")
        return True

    def apply_blacklist_to_channels(
            self, channels: List[ChannelData]
    ) -> Tuple[List[ChannelData], int]:
        return self.blacklist_manager.filter_channels(channels)

    # ── ЧС домен/IP ──

    def add_domain_to_blacklist(
            self, value: str,
            include_subdomains: bool = True,
            note: str = "") -> bool:
        r = self.domain_blacklist_manager.add_domain(
            value, include_subdomains=include_subdomains,
            note=note)
        if r:
            self.domain_blacklist_updated.emit()
            self.schedule_alive_index_rebuild(
                "add_domain_to_blacklist")
        return r

    def remove_domain_from_blacklist(self, value: str) -> bool:
        r = self.domain_blacklist_manager.remove_domain(value)
        if r:
            self.domain_blacklist_updated.emit()
            self.schedule_alive_index_rebuild(
                "remove_domain_from_blacklist")
        return r

    def clear_domain_blacklist(self) -> bool:
        self.domain_blacklist_manager.clear()
        self.domain_blacklist_updated.emit()
        self.schedule_alive_index_rebuild(
            "clear_domain_blacklist")
        return True

    def get_domain_blacklist(self) -> List[Dict[str, Any]]:
        return self.domain_blacklist_manager.get_all_dicts()

    def apply_domain_blacklist(
            self, channels: List[ChannelData]
    ) -> Tuple[List[ChannelData], int]:
        return self.domain_blacklist_manager.filter_channels(
            channels)

    def apply_domain_blacklist_clean(
            self, channels: List[ChannelData]
    ) -> Tuple[List[ChannelData], int]:
        return self.domain_blacklist_manager.clean_channels(
            channels)

    # ── UA ──

    def apply_domain_user_agent(
            self, channels: List[ChannelData]) -> int:
        return (self.domain_user_agent_manager
                .apply_rules_to_channels(channels))

    def get_user_agent_for_url(self, url: str) -> Optional[str]:
        return (self.domain_user_agent_manager
                .get_user_agent_for_url(url))

    def should_remove_user_agent(self, url: str) -> bool:
        return (self.domain_user_agent_manager
                .should_remove_user_agent(url))

    # ── Настройки / кэш ──

    def get_replacement_settings(self) -> LinkReplacementSettings:
        self._bind_managers()
        return self.link_replacement_settings

    def get_cached_check_result(
            self, name: str, url: str
    ) -> Optional[Dict[str, Any]]:
        if self.cache_manager is None:
            return None
        if not self.config.get('use_check_result_cache', True):
            return None
        ttl = int(self.config.get(
            'check_result_cache_ttl_hours',
            CHECK_RESULT_CACHE_TTL_HOURS))
        return self.cache_manager.get_check_result(name, url, ttl)

    # ── Шаблоны группировки (group-title) ──

    def get_group_templates(self) -> List[Any]:
        return self.group_template_manager.get_all()

    def get_group_template_names(self) -> List[str]:
        return self.group_template_manager.get_names()

    def get_group_template(self, name: str):
        return self.group_template_manager.get(name)

    def save_group_template(self, template,
                             overwrite: bool = False
                             ) -> Tuple[bool, str]:
        ok, err = self.group_template_manager.save_template(
            template, overwrite=overwrite)
        if ok:
            self.group_templates_updated.emit()
        return ok, err

    def rename_group_template(self, old_name: str,
                               new_name: str
                               ) -> Tuple[bool, str]:
        ok, err = self.group_template_manager.rename_template(
            old_name, new_name)
        if ok:
            self.group_templates_updated.emit()
        return ok, err

    def delete_group_template(self, name: str) -> bool:
        ok = self.group_template_manager.delete_template(name)
        if ok:
            self.group_templates_updated.emit()
        return ok

    def build_group_template_from_channels(
            self, channels: List[ChannelData],
            name: str,
            include_only_existing_groups: bool = True,
    ) -> Tuple[Any, str]:
        return self.group_template_manager.build_from_channels(
            channels, name,
            settings=self.get_replacement_settings(),
            include_only_existing_groups=(
                include_only_existing_groups))

    def apply_group_template(self, name: str, channels
                              ) -> Dict[str, Any]:
        t = self.group_template_manager.get(name)
        if t is None:
            return {'changed': 0, 'matched': 0,
                    'unmatched': 0, 'missing': 0}
        return self.group_template_manager.apply_to_channels(
            t, channels,
            settings=self.get_replacement_settings(),
            keep_unmatched=True)