# -*- coding: utf-8 -*-
"""Все воркеры в одном модуле.

FIX (0.3.2): LinkReplacementWorker принимает epg_db и передаёт
его в find_matches_for_channel / get_alive_urls_for_keys, чтобы
автозамена искала замены не только по meta.name, но и по
tvg_id / tvg_name / epg_alternative_names / EPG display-name.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from blacklists import BlacklistIndex
from config import Config, LinkReplacementSettings
from constants import (
    URL_CHECK_MAX_WORKERS, CHECK_RESULT_CACHE_TTL_HOURS,
    SOURCE_CHECK_WORKERS_DEFAULT, SOURCE_CHECK_TIMEOUT_DEFAULT,
    SOURCE_CHECK_TRUST_SEC_DEFAULT, SOURCE_CHECK_BATCH_SIZE_DEFAULT,
    REPLACEMENT_MAX_WORKERS_DEFAULT, SEARCH_WORKER_MAX,
    EPG_FUZZY_ENABLED_DEFAULT, EPG_FUZZY_THRESHOLD_DEFAULT,
    EPG_FUZZY_MIN_LENGTH_DEFAULT, EPG_FUZZY_MIN_GAP_DEFAULT,
    EPG_SOURCE_TIMEOUT_SEC, FUTURES_SHUTDOWN_WAIT_SEC, StatusText)
from epg import EPGDatabase
from logging_setup import logger
from models import ChannelData, LinkQuality
from sources import LinkSource, LinkSourceManager
from utils import URLUtils, _StopToken


class BaseWorker(QThread):
    progress = pyqtSignal(int, int, str)
    error = pyqtSignal(str)
    worker_done = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._stop_token = _StopToken()

    def stop(self):
        self._stop_token.set()

    def is_stopped(self) -> bool:
        return self._stop_token.is_set()

    def run(self):
        try:
            self._execute()
        except Exception as e:
            logger.exception(type(self).__name__)
            self.error.emit(str(e))
        finally:
            self.worker_done.emit()

    def _execute(self):
        raise NotImplementedError


def run_parallel(items, worker_fn, max_workers, stop_token):
    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers)
    try:
        futures = [ex.submit(worker_fn, x) for x in items]
        for fut in concurrent.futures.as_completed(futures):
            if stop_token.is_set():
                for f in futures:
                    with suppress(Exception):
                        f.cancel()
                break
            yield fut.result()
    finally:
        with suppress(Exception):
            ex.shutdown(wait=True, cancel_futures=True)


@dataclass
class SourceCheckConfig:
    max_workers: int = SOURCE_CHECK_WORKERS_DEFAULT
    timeout: int = SOURCE_CHECK_TIMEOUT_DEFAULT
    trust_sec: int = SOURCE_CHECK_TRUST_SEC_DEFAULT
    batch_size: int = SOURCE_CHECK_BATCH_SIZE_DEFAULT
    verify_ssl: bool = False

    @classmethod
    def from_app_config(cls, config: Optional[Config]
                        ) -> 'SourceCheckConfig':
        if config is None:
            return cls()
        return cls(
            max_workers=int(config.get('source_check_workers',
                                       SOURCE_CHECK_WORKERS_DEFAULT)),
            timeout=int(config.get('source_check_timeout',
                                   SOURCE_CHECK_TIMEOUT_DEFAULT)),
            trust_sec=int(config.get('source_check_trust_sec',
                                     SOURCE_CHECK_TRUST_SEC_DEFAULT)),
            batch_size=int(config.get('source_check_batch_size',
                                      SOURCE_CHECK_BATCH_SIZE_DEFAULT)),
            verify_ssl=bool(config.get('verify_ssl', False)),
        )


class LinkReplacementWorker(BaseWorker):
    channel_updated = pyqtSignal(int, str, str, str, bool)
    replacement_done = pyqtSignal(int, int)

    def __init__(self, channels: List[ChannelData],
                 source_manager: LinkSourceManager,
                 settings: LinkReplacementSettings,
                 config: Optional[Config] = None,
                 max_workers: int = REPLACEMENT_MAX_WORKERS_DEFAULT,
                 epg_db: Optional[EPGDatabase] = None):
        super().__init__()
        self.channels = list(channels)
        self.source_manager = source_manager
        self.settings = settings
        self.config = config
        self.epg_db = epg_db
        self.max_workers = max(1, min(int(max_workers), 8))
        self._sources_loaded = False
        self._replace_lock = threading.Lock()
        self._replaced_count = 0

        self._used_urls: Set[str] = set()
        self._used_urls_lock = threading.Lock()
        try:
            for ch in self.channels:
                if (ch.link.url
                        and not ch.needs_replacement(self.settings)):
                    self._used_urls.add(ch.link.url)
        except Exception:
            logger.exception("prefill _used_urls")

    def _try_claim_url(self, url: str) -> bool:
        if not url:
            return False
        with self._used_urls_lock:
            if url in self._used_urls:
                return False
            self._used_urls.add(url)
            return True

    def _release_url(self, url: str):
        if not url:
            return
        with self._used_urls_lock:
            self._used_urls.discard(url)

    def _verify_candidates(self, urls: List[str]) -> Optional[str]:
        if not urls:
            return None
        if len(urls) == 1:
            return urls[0]
        timeout = self.settings.get_verify_candidates_timeout()
        max_workers = min(
            self.settings.get_verify_candidates_max_workers(),
            len(urls))
        results: List[Tuple[str, float]] = []
        lock = threading.Lock()

        def _check_one(u: str):
            if self.is_stopped():
                return
            try:
                ok, rt, msg, code = URLUtils.check_url(
                    u, timeout, verify_ssl=False,
                    stop_token=self._stop_token)
            except Exception:
                logger.exception("verify candidate %s", u[:80])
                ok, rt = False, None
            if ok is True:
                with lock:
                    results.append((u, float(rt or 0.0)))

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="verify") as ex:
            futures = [ex.submit(_check_one, u) for u in urls]
            for fut in concurrent.futures.as_completed(futures):
                if self.is_stopped():
                    for f in futures:
                        with suppress(Exception):
                            f.cancel()
                    break
                with suppress(Exception):
                    fut.result()
        if not results:
            return None
        results.sort(key=lambda x: x[1])
        return results[0][0]

    def _get_cached_check(self, name_lower: str,
                          url: str) -> Optional[Dict[str, Any]]:
        cm = self.source_manager.cache_manager
        if cm is None:
            return None
        try:
            ttl = CHECK_RESULT_CACHE_TTL_HOURS
            if self.config is not None:
                ttl = int(self.config.get(
                    'check_result_cache_ttl_hours',
                    CHECK_RESULT_CACHE_TTL_HOURS))
            return cm.get_check_result(name_lower, url, ttl)
        except Exception:
            logger.exception("get_cached_check")
            return None

    def _preload_sources(self):
        if self._sources_loaded:
            return
        self._sources_loaded = True
        try:
            sources = self.source_manager.get_enabled_sources()
            if not sources:
                return

            def _load(s):
                return self.source_manager.load_links_from_source(
                    s, True, self.config, True, self._stop_token,
                    settings=self.settings)

            list(run_parallel(sources, _load,
                               min(SEARCH_WORKER_MAX,
                                   len(sources)),
                               self._stop_token))
        except Exception:
            logger.exception("_preload_sources")

    def _execute(self):
        total = len(self.channels)
        if total == 0:
            self.replacement_done.emit(0, 0)
            return
        self._preload_sources()
        workers = min(self.max_workers, total)
        processed = 0
        progress_lock = threading.Lock()
        last_emit = [0.0]

        def emit_progress(cur: int):
            now = time.time()
            if now - last_emit[0] < 0.1 and cur < total:
                return
            last_emit[0] = now
            self.progress.emit(
                int(cur / total * 100), 100,
                f"Обработано: {cur}/{total}")

        def process_one(ch: ChannelData):
            nonlocal processed
            if self.is_stopped():
                return None
            try:
                res = self._find_replacement_for(ch)
            except Exception:
                logger.exception("Error processing %s", ch.meta.name)
                res = None
            with progress_lock:
                processed += 1
                cur = processed
            if res:
                old_url, new_url, from_alive = res
                self.channel_updated.emit(
                    ch.uid, old_url, new_url, ch.meta.name,
                    from_alive)
                with self._replace_lock:
                    self._replaced_count += 1
            emit_progress(cur)
            return res

        list(run_parallel(self.channels, process_one, workers,
                          self._stop_token))
        self.replacement_done.emit(self._replaced_count, total)

    def _should_replace(self, channel: ChannelData) -> bool:
        return channel.needs_replacement(self.settings)

    def _find_replacement_for(self, channel: ChannelData
                              ) -> Optional[Tuple[str, str, bool]]:
        if self.is_stopped() or not self._should_replace(channel):
            return None
        res = self._find_replacement(channel)
        if not res:
            return None
        new_url, from_alive = res
        if not new_url or new_url == channel.link.url:
            return None
        return channel.link.url, new_url, from_alive

    def _filter_urls_by_cache(self, urls: List[str],
                              name_lower: str
                              ) -> Tuple[List[str], List[str]]:
        trusted, unknown = [], []
        trust_sec = 3600
        if self.config is not None:
            trust_sec = int(self.config.get(
                'check_cache_trust_seconds', 3600))
        now = time.time()
        for u in urls:
            cached = self._get_cached_check(name_lower, u)
            if not cached:
                unknown.append(u)
                continue
            age = now - float(cached.get('last_check') or 0)
            if age >= trust_sec:
                unknown.append(u)
                continue
            if cached.get('alive'):
                trusted.append(u)
        return trusted, unknown

    def _find_replacement(self, channel: ChannelData
                          ) -> Optional[Tuple[str, bool]]:
        t0 = time.perf_counter()
        try:
            s = self.settings
            if s.is_channel_blacklisted(channel.meta.name,
                                        channel.meta.tvg_id):
                return None
            max_urls = s.get_max_urls_per_channel()
            verify_enabled = s.get_verify_candidates_enabled()

            # FIX: search_keys с EPG — единый набор для всех
            # этапов (alive / matches / verify).
            search_keys = channel.search_keys(
                s, epg_db=self.epg_db)

            try:
                alive_urls = (
                    self.source_manager.get_alive_urls_for_keys(
                        search_keys, s, limit=max_urls))
            except Exception:
                logger.exception("get_alive_urls_for_keys")
                alive_urls = []

            alive_urls = [
                u for u in alive_urls
                if u and not s.is_blacklisted(u)
                and not s.is_filtered_domain(u)
                and URLUtils.validate_url(u) is None]

            if alive_urls:
                if (len(alive_urls) > 1 and verify_enabled
                        and not self.is_stopped()):
                    best = self._verify_candidates(alive_urls)
                    if best and self._try_claim_url(best):
                        return best, True
                else:
                    for u in alive_urls:
                        if self._try_claim_url(u):
                            return u, True

            alt_urls = [a for a in channel.link.alternative_urls
                        if a and a.strip()
                        and not s.is_blacklisted(a)
                        and not s.is_filtered_domain(a)]

            matches = self.source_manager.find_matches_for_channel(
                channel, s,
                config=self.config,
                stop_token=self._stop_token,
                only_working=False,
                epg_db=self.epg_db)
            candidates = [a.link.url for a in matches
                          if a.link.url and a.link.url.strip()
                          and not s.is_blacklisted(a.link.url)
                          and not s.is_filtered_domain(a.link.url)]
            seen: Set[str] = set()
            all_urls = []
            for u in alt_urls + candidates:
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)
            if not all_urls:
                return None
            all_urls = all_urls[:max_urls]

            trusted, unknown = self._filter_urls_by_cache(
                all_urls, channel.meta.name.lower())

            if trusted:
                if (len(trusted) > 1 and verify_enabled
                        and not self.is_stopped()):
                    best = self._verify_candidates(trusted)
                    if best and self._try_claim_url(best):
                        return best, True
                else:
                    for u in trusted:
                        if self._try_claim_url(u):
                            return u, True
            return None
        except Exception:
            logger.exception("Replacement search failed for %s",
                             channel.meta.name)
            return None


class SourceUrlCheckWorker(BaseWorker):
    source_check_progress = pyqtSignal(str, int, int)
    source_check_done = pyqtSignal(str, int, int)
    channel_checked = pyqtSignal(str, bool, str)

    _CHANNEL_EMIT_EVERY = 25

    def __init__(self, source_name: str,
                 channels: List[ChannelData],
                 cache_manager: Optional['CacheManager'],
                 check_config: Optional[SourceCheckConfig] = None,
                 stop_token: Optional['_StopToken'] = None,
                 *,
                 settings_snapshot: Optional[Dict[str, Any]] = None,
                 blacklist_index: Optional[BlacklistIndex] = None):
        super().__init__()
        if stop_token is not None:
            self._stop_token = stop_token
        self.source_name = source_name
        self.channels = list(channels)
        self.cache_manager = cache_manager
        cfg = check_config or SourceCheckConfig()
        self.max_workers = max(1, min(int(cfg.max_workers), 32))
        self.timeout = max(int(cfg.timeout), 3)
        self.trust_sec = int(cfg.trust_sec)
        self.batch_size = max(1, int(cfg.batch_size))
        self.verify_ssl = bool(cfg.verify_ssl)

        self._settings_snapshot = settings_snapshot or {}
        self._bl_index = blacklist_index or BlacklistIndex(set(), set())
        self._skip_count_channel = 0
        self._skip_count_domain = 0
        self._skip_count_filtered = 0

    def _should_skip(self, ch: ChannelData) -> bool:
        if self._bl_index.matches(ch.meta.name, ch.meta.tvg_id):
            self._skip_count_channel += 1
            return True
        url = ch.link.url
        if not url:
            return True
        snap = self._settings_snapshot
        if snap.get('use_ip_filtering', True):
            domain_bl = snap.get('_domain_bl_manager')
            if domain_bl is not None:
                try:
                    if domain_bl.matches_url(url):
                        self._skip_count_domain += 1
                        return True
                except Exception:
                    logger.exception("domain_bl.matches_url")
            temp = snap.get('temporary_domains', [])
            unsafe = snap.get('unsafe_domains', [])
            if URLUtils.host_in_domains(
                    URLUtils.extract_host(url) or '',
                    list(temp) + list(unsafe)):
                self._skip_count_filtered += 1
                return True
        return False

    def _execute(self):
        total = len(self.channels)
        if total == 0:
            self.source_check_done.emit(self.source_name, 0, 0)
            return

        fresh: Dict[Tuple[str, str], Dict[str, Any]] = {}
        pairs: List[Tuple[str, str]] = []
        passed: List[ChannelData] = []
        for ch in self.channels:
            if self.is_stopped():
                break
            if self._should_skip(ch):
                continue
            passed.append(ch)
            pairs.append((ch.meta.name, ch.link.url))

        if self.cache_manager is not None and pairs:
            try:
                fresh = self.cache_manager.get_check_results_batch(
                    pairs,
                    max_age_hours=CHECK_RESULT_CACHE_TTL_HOURS)
            except Exception:
                logger.exception(
                    "SourceUrlCheckWorker batch read")
                fresh = {}

        now = time.time()
        to_check: List[ChannelData] = []
        working = checked = 0
        for ch in passed:
            if self.is_stopped():
                break
            url = ch.link.url
            hit = fresh.get((ch.meta.name.lower(), url))
            if hit and (now - hit['last_check']) < self.trust_sec:
                checked += 1
                if hit['alive']:
                    working += 1
                continue
            to_check.append(ch)

        self.source_check_progress.emit(
            self.source_name, checked, total)
        if not to_check:
            self.source_check_done.emit(
                self.source_name, working, total)
            return

        lock = threading.Lock()
        pending: List[Tuple[str, str, bool, float, str,
                             Optional[int]]] = []
        pending_lock = threading.Lock()
        emit_counter = [0]

        def flush_batch(items):
            if not items or self.cache_manager is None:
                return
            try:
                self.cache_manager.save_check_results_batch(items)
            except Exception:
                logger.warning("batch save failed",
                               exc_info=True)

        def check_one(ch: ChannelData):
            nonlocal working, checked
            if self.is_stopped():
                return
            url = ch.link.url
            if not url:
                with lock:
                    checked += 1
                return
            try:
                ok, rt, msg, code = URLUtils.check_url(
                    url, self.timeout,
                    verify_ssl=self.verify_ssl,
                    stop_token=self._stop_token)
            except Exception as e:
                ok, rt, msg, code = (
                    False, 0.0,
                    f"exception: {str(e)[:40]}", None)
            if self.is_stopped():
                return
            if ok is None:
                with suppress(Exception):
                    ch.status.status_text = StatusText.CANCELLED
                return
            with lock:
                checked += 1
                if ok is True:
                    working += 1
            with suppress(Exception):
                ch.status.url_status = ok
                ch.status.url_check_time = datetime.now()
                ch.status.status_code = code
                ch.status.link_response_time = (
                    float(rt) if rt is not None else None)
                if ok is True:
                    ch.status.link_quality = LinkQuality.WORKING
                elif ok is False:
                    err = URLUtils.validate_url(url)
                    if err and ('protocol' in err.lower()
                                or 'UDP' in err
                                or 'TCP' in err):
                        ch.status.link_quality = \
                            LinkQuality.UNSUPPORTED
                    else:
                        ch.status.link_quality = \
                            LinkQuality.NOT_WORKING
                else:
                    ch.status.link_quality = LinkQuality.UNKNOWN
                st_txt, _ = URLUtils.classify_status(
                    url, code, '' if ok else (msg or ''))
                ch.status.status_text = st_txt
                ch.touch()

            emit_counter[0] += 1
            if emit_counter[0] % self._CHANNEL_EMIT_EVERY == 0:
                with suppress(Exception):
                    self.channel_checked.emit(
                        self.source_name, bool(ok),
                        ch.meta.name or "")

            with pending_lock:
                if ok is not None:
                    pending.append((
                        ch.meta.name, url, bool(ok),
                        (rt or 0.0) * 1000.0,
                        (msg or "")[:200], code))
                batch = (pending[:]
                         if len(pending) >= self.batch_size
                         else None)
                if batch:
                    pending.clear()
            if batch:
                flush_batch(batch)

        last_emit = 0
        try:
            for _ in run_parallel(to_check, check_one,
                                   self.max_workers,
                                   self._stop_token):
                if self.is_stopped():
                    continue
                with lock:
                    cur = checked
                if cur - last_emit >= 5 or cur == len(to_check):
                    last_emit = cur
                    self.source_check_progress.emit(
                        self.source_name, cur, total)
        except Exception:
            logger.exception("check_one")

        with pending_lock:
            leftover = pending[:]
            pending.clear()
        if leftover and not self.is_stopped():
            flush_batch(leftover)

        self.source_check_done.emit(
            self.source_name, working, total)


class SourcesRefreshWorker(BaseWorker):
    PHASE1_END = 40
    PHASE2_END = 90
    PHASE3_END = 100

    all_done = pyqtSignal(int, int)
    source_checked = pyqtSignal(str, int, int)
    channel_checked = pyqtSignal(str, str, bool, str)

    def __init__(self, sources: List[LinkSource],
                 manager: LinkSourceManager, config: Config,
                 check_urls: bool = True, *,
                 settings_snapshot: Optional[Dict[str, Any]] = None,
                 blacklist_index: Optional[BlacklistIndex] = None,
                 settings: Optional[LinkReplacementSettings] = None):
        super().__init__()
        self.sources = list(sources)
        self.manager = manager
        self.config = config
        self.check_urls = bool(check_urls)
        self._child_workers: List[SourceUrlCheckWorker] = []
        self._settings_snapshot = settings_snapshot or {}
        self._bl_index = blacklist_index or BlacklistIndex(set(), set())
        self._settings = settings

        self._phase2_lock = threading.Lock()
        self._phase2_weights: Dict[str, int] = {}
        self._phase2_checked: Dict[str, int] = {}
        self._phase2_total = 0
        self._last_phase2_pct = self.PHASE1_END

    def stop(self):
        super().stop()
        for w in getattr(self, '_child_workers', []):
            with suppress(Exception):
                if w.isRunning():
                    w.stop()

    def _load_one(self, source: LinkSource):
        self.manager.invalidate_cache(source.name)
        try:
            chs = self.manager.load_links_from_source(
                source, use_cache=False, config=self.config,
                stop_token=self._stop_token,
                settings=self._settings)
        except Exception:
            logger.exception("load_links_from_source %s", source.name)
            chs = []
        return (len(chs) if chs else 0), source, chs

    def _on_source_check_done(self, name: str, working: int,
                              total: int):
        try:
            self.manager.update_source_health(name, working)
        except Exception:
            logger.exception("update_source_health")
        with suppress(Exception):
            self.source_checked.emit(name, working, total)

    def _on_source_error(self, name: str, err: str):
        logger.warning("Source check error [%s]: %s", name, err)
        with suppress(Exception):
            self.source_checked.emit(name, 0, 0)

    def _on_child_progress(self, name: str, cur: int, tot: int):
        with self._phase2_lock:
            weight = self._phase2_weights.get(name, tot)
            self._phase2_checked[name] = min(int(cur), int(weight))
            checked_sum = sum(self._phase2_checked.values())
            total_sum = self._phase2_total or 1
            span = self.PHASE2_END - self.PHASE1_END
            pct = self.PHASE1_END + int(checked_sum / total_sum * span)
            if pct > self.PHASE2_END:
                pct = self.PHASE2_END
            if pct <= self._last_phase2_pct:
                return
            self._last_phase2_pct = pct
        self.progress.emit(
            pct, self.PHASE3_END,
            f"[2/3] Проверка URL: {checked_sum}/{total_sum}")

    def _execute(self):
        processed = 0
        total = len(self.sources)
        success = 0
        if total == 0:
            self.all_done.emit(0, 0)
            return

        loaded: List[Tuple[LinkSource, List[ChannelData]]] = []
        span1 = self.PHASE1_END
        try:
            for cnt, src, chs in run_parallel(
                    self.sources, self._load_one,
                    min(URL_CHECK_MAX_WORKERS, total),
                    self._stop_token):
                if self.is_stopped():
                    continue
                processed += 1
                if cnt:
                    success += 1
                pct = int(processed / total * span1)
                self.progress.emit(
                    pct, self.PHASE3_END,
                    f"[1/3] Загрузка источников: "
                    f"{processed}/{total}")
                loaded.append((src, chs or []))
        except Exception:
            logger.exception("_load_one failed")

        if self.is_stopped():
            self.all_done.emit(success, total)
            return

        if not self.check_urls:
            self._phase3_rebuild(loaded)
            self.all_done.emit(success, total)
            return

        check_cfg = SourceCheckConfig.from_app_config(self.config)
        workers: List[SourceUrlCheckWorker] = []
        for src, chs in loaded:
            if self.is_stopped():
                break
            if not chs:
                continue
            w = SourceUrlCheckWorker(
                source_name=src.name, channels=chs,
                cache_manager=self.manager.cache_manager,
                check_config=check_cfg,
                stop_token=self._stop_token,
                settings_snapshot=self._settings_snapshot,
                blacklist_index=self._bl_index)
            w.source_check_done.connect(self._on_source_check_done)
            w.source_check_progress.connect(self._on_child_progress)
            w.error.connect(
                lambda err, s=src.name:
                    self._on_source_error(s, err))
            w.channel_checked.connect(
                lambda n, alive, m, s=src.name:
                    self.channel_checked.emit(s, n, alive, m))
            workers.append(w)
        self._child_workers = workers

        with self._phase2_lock:
            self._phase2_weights = {}
            self._phase2_checked = {}
            for src, chs in loaded:
                if chs:
                    self._phase2_weights[src.name] = len(chs)
            self._phase2_total = sum(self._phase2_weights.values())
            self._last_phase2_pct = self.PHASE1_END

        if not workers or self._phase2_total == 0:
            self._phase3_rebuild(loaded)
            self.all_done.emit(success, total)
            return

        self.progress.emit(
            self.PHASE1_END, self.PHASE3_END,
            f"[2/3] Проверка URL: 0/{self._phase2_total}")

        for w in workers:
            if self.is_stopped():
                break
            w.start()

        deadline = time.time() + 3600
        for w in workers:
            if self.is_stopped():
                break
            remaining_ms = max(
                1000, int((deadline - time.time()) * 1000))
            with suppress(Exception):
                w.wait(remaining_ms)
        for w in workers:
            with suppress(Exception):
                if w.isRunning():
                    w.stop()
        for w in workers:
            with suppress(Exception):
                if w.isRunning():
                    w.wait(
                        int(FUTURES_SHUTDOWN_WAIT_SEC * 1000))

        with self._phase2_lock:
            self._last_phase2_pct = self.PHASE2_END
        self.progress.emit(
            self.PHASE2_END, self.PHASE3_END,
            f"[2/3] Проверка URL: "
            f"{self._phase2_total}/{self._phase2_total}")

        if self.is_stopped():
            self.all_done.emit(success, total)
            return

        self._phase3_rebuild(loaded)
        self.all_done.emit(success, total)

    def _phase3_rebuild(self, loaded):
        if not loaded:
            self.progress.emit(
                self.PHASE3_END, self.PHASE3_END, "[3/3] Готово")
            return
        span = self.PHASE3_END - self.PHASE2_END
        total3 = len(loaded)
        completed = 0
        for i, (src, _chs) in enumerate(loaded):
            if self.is_stopped():
                break
            try:
                self.manager.rebuild_alive_index(
                    src.name,
                    settings=self._settings)
            except Exception:
                logger.exception("rebuild_alive_index(%s)", src.name)
            completed = i + 1
            pct = self.PHASE2_END + int(completed / total3 * span)
            if pct > self.PHASE3_END:
                pct = self.PHASE3_END
            self.progress.emit(
                pct, self.PHASE3_END,
                f"[3/3] Перестройка индекса: "
                f"{completed}/{total3}")
        self.progress.emit(
            self.PHASE3_END, self.PHASE3_END, "[3/3] Готово")


class EPGLoaderWorker(BaseWorker):
    epg_loaded = pyqtSignal(int, list)

    def __init__(self, urls: List[str], epg_db: EPGDatabase,
                 timeout: int = EPG_SOURCE_TIMEOUT_SEC):
        super().__init__()
        self.urls = list(urls)
        self.epg_db = epg_db
        self.timeout = timeout

    def _execute(self):
        if not self.urls:
            self.epg_loaded.emit(0, [])
            return
        self.progress.emit(0, len(self.urls), "Загрузка EPG...")
        count, errors = self.epg_db.load_from_urls(
            self.urls, self.timeout, self._stop_token)
        self.epg_loaded.emit(count, errors)


class EPGMetadataApplyWorker(BaseWorker):
    applied = pyqtSignal(int, list)

    def __init__(self, channels: List[ChannelData],
                 epg_db: EPGDatabase,
                 config: Optional[Config] = None,
                 settings: Optional[LinkReplacementSettings] = None):
        super().__init__()
        # FIX: сохраняем полный снимок канала (uid, name,
        # tvg_id, tvg_name, alternative_names), чтобы матчинг
        # учитывал alternative_names после первого применения.
        self._channels = [
            (ch.uid, ch.meta.name or "",
             ch.meta.tvg_id or "", ch.meta.tvg_name or "",
             list(ch.meta.epg_alternative_names or []))
            for ch in channels]
        self.epg_db = epg_db
        self.config = config
        self.settings = settings

        if config is not None:
            self._fuzzy_enabled = bool(config.get(
                'epg_fuzzy_match_enabled',
                EPG_FUZZY_ENABLED_DEFAULT))
            self._fuzzy_threshold = float(config.get(
                'epg_fuzzy_threshold',
                EPG_FUZZY_THRESHOLD_DEFAULT))
            self._fuzzy_min_length = int(config.get(
                'epg_fuzzy_min_length',
                EPG_FUZZY_MIN_LENGTH_DEFAULT))
            self._fuzzy_min_gap = float(config.get(
                'epg_fuzzy_min_gap',
                EPG_FUZZY_MIN_GAP_DEFAULT))
            self._prefer_tvg_id = bool(config.get(
                'epg_prefer_tvg_id_over_name', True))
        else:
            self._fuzzy_enabled = EPG_FUZZY_ENABLED_DEFAULT
            self._fuzzy_threshold = EPG_FUZZY_THRESHOLD_DEFAULT
            self._fuzzy_min_length = EPG_FUZZY_MIN_LENGTH_DEFAULT
            self._fuzzy_min_gap = EPG_FUZZY_MIN_GAP_DEFAULT
            self._prefer_tvg_id = True

    def _execute(self):
        if not self.epg_db.has_channel_info:
            self.applied.emit(0, [])
            return
        total = len(self._channels)
        if total == 0:
            self.applied.emit(0, [])
            return

        try:
            self.epg_db.set_prefer_tvg_id(self._prefer_tvg_id)
        except Exception:
            logger.exception("set_prefer_tvg_id")

        threshold = (self._fuzzy_threshold
                     if self._fuzzy_enabled else 1.0)
        results: List[Tuple[int, str, str]] = []
        alternatives_map: Dict[int, List[str]] = {}
        sources: Dict[int, str] = {}
        processed = 0
        tmp = ChannelData()
        for (uid, name, tvg_id, tvg_name,
                alt_names) in self._channels:
            if self.is_stopped():
                break
            processed += 1
            if processed % 200 == 0:
                self.progress.emit(
                    int(processed / total * 100), 100,
                    f"EPG-метаданные: {processed}/{total}")
            tmp.meta.name = name
            tmp.meta.tvg_id = tvg_id
            tmp.meta.tvg_name = tvg_name
            tmp.meta.epg_alternative_names = list(alt_names or [])
            tmp.uid = uid
            updates, src, alts = (
                self.epg_db.build_epg_metadata_updates(
                    tmp, threshold, self._fuzzy_min_length,
                    self._fuzzy_min_gap, settings=self.settings))
            for field, value in updates:
                results.append((uid, field, value))
            if alts:
                alternatives_map[uid] = alts
            if src:
                sources[uid] = src
        for uid, src in sources.items():
            results.append((uid, 'epg_source', src))
        for uid, alts in alternatives_map.items():
            results.append((
                uid, 'epg_alternative_names', "\x1f".join(alts)))
        touched = len({r[0] for r in results})
        self.applied.emit(touched, results)