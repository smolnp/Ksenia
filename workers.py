# -*- coding: utf-8 -*-
"""BaseWorker и все воркеры."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from contextlib import suppress
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from config import Config, LinkReplacementSettings
from constants import (URL_CHECK_MAX_WORKERS,
    CHECK_RESULT_CACHE_TTL_HOURS, SOURCE_CHECK_WORKERS_DEFAULT,
    SOURCE_CHECK_TIMEOUT_DEFAULT, SOURCE_CHECK_TRUST_SEC_DEFAULT,
    SOURCE_CHECK_BATCH_SIZE_DEFAULT, REPLACEMENT_MAX_WORKERS_DEFAULT,
    SEARCH_WORKER_MAX, EPG_FUZZY_ENABLED_DEFAULT,
    EPG_FUZZY_THRESHOLD_DEFAULT, EPG_FUZZY_MIN_LENGTH_DEFAULT,
    EPG_FUZZY_MIN_GAP_DEFAULT, EPG_SOURCE_TIMEOUT_SEC)
from epg import EPGDatabase
from models import ChannelData, LinkQuality
from paths import logger
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


class LinkReplacementWorker(BaseWorker):
    """Автозамена — ТОЛЬКО из кэша url_status_cache, без сети."""

    channel_updated = pyqtSignal(int, str, str, str)
    replacement_done = pyqtSignal(int, int)

    def __init__(self, channels: List[ChannelData],
                 source_manager: LinkSourceManager,
                 settings: LinkReplacementSettings,
                 config: Optional[Config] = None,
                 max_workers: int = REPLACEMENT_MAX_WORKERS_DEFAULT):
        super().__init__()
        self.channels = list(channels)
        self.source_manager = source_manager
        self.settings = settings
        self.config = config
        self.max_workers = max(1, min(int(max_workers), 8))
        try:
            from ksenia_window import ApplicationCore
            self._core = ApplicationCore.instance()
        except Exception:
            self._core = None
        self._sources_loaded = False
        self._replace_lock = threading.Lock()
        self._replaced_count = 0

    def _preload_sources(self):
        if self._sources_loaded:
            return
        self._sources_loaded = True
        try:
            sources = self.source_manager.get_enabled_sources()
            if not sources:
                return
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(SEARCH_WORKER_MAX, len(sources)),
                thread_name_prefix="preload")
            try:
                futures = [
                    ex.submit(
                        self.source_manager.load_links_from_source,
                        s, True, self.config, True, self._stop_token)
                    for s in sources
                ]
                for fut in concurrent.futures.as_completed(futures):
                    if self.is_stopped():
                        break
                    try:
                        fut.result()
                    except Exception:
                        logger.exception("preload source")
            finally:
                with suppress(Exception):
                    ex.shutdown(wait=False, cancel_futures=True)
            logger.info(
                f"LinkReplacementWorker: предзагружено источников "
                f"{len(sources)}")
        except Exception:
            logger.exception("_preload_sources")

    def run(self):
        try:
            total = len(self.channels)
            if total == 0:
                self.replacement_done.emit(0, 0)
                return

            self._preload_sources()

            workers = min(self.max_workers, total)
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="replace")
            processed = 0
            progress_lock = threading.Lock()

            def process_one(ch: ChannelData):
                nonlocal processed
                if self.is_stopped():
                    return None
                try:
                    res = self._find_replacement_for(ch)
                except Exception:
                    logger.exception(f"Ошибка обработки {ch.meta.name}")
                    res = None
                with progress_lock:
                    processed += 1
                    cur = processed
                if res:
                    old_url, new_url = res
                    self.channel_updated.emit(
                        ch.uid, old_url, new_url, ch.meta.name)
                    with self._replace_lock:
                        self._replaced_count += 1
                self.progress.emit(
                    int(cur / total * 100), 100,
                    f"Обработано: {cur}/{total}")
                return res

            try:
                futures = [ex.submit(process_one, ch) for ch in self.channels]
                for fut in concurrent.futures.as_completed(futures):
                    if self.is_stopped():
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        fut.result()
                    except Exception:
                        logger.exception("replace worker")
            finally:
                with suppress(Exception):
                    ex.shutdown(wait=False, cancel_futures=True)

            self.replacement_done.emit(self._replaced_count, total)
        except Exception as e:
            self.error.emit(f"Ошибка: {e}")
            logger.exception("LinkReplacementWorker")
        finally:
            self.worker_done.emit()

    def _should_replace(self, channel: ChannelData) -> bool:
        return channel.needs_replacement(self.settings)

    def _find_replacement_for(self, channel: ChannelData
                              ) -> Optional[Tuple[str, str]]:
        if self.is_stopped():
            return None
        if not self._should_replace(channel):
            return None
        new_url = self._find_replacement(channel)
        if not new_url or new_url == channel.link.url:
            return None
        return channel.link.url, new_url

    def _filter_urls_by_cache(self, urls: List[str],
                              name_lower: str
                              ) -> Tuple[List[str], List[str]]:
        trusted: List[str] = []
        unknown: List[str] = []
        trust_sec = 3600
        if self.config is not None:
            trust_sec = int(self.config.get('check_cache_trust_seconds', 3600))
        now = time.time()
        core = self._core
        for u in urls:
            if core is None:
                unknown.append(u)
                continue
            cached = core.get_cached_check_result(name_lower, u)
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

    def _find_replacement(self, channel: ChannelData) -> Optional[str]:
        """Только кэш: get_alive_urls → search_channel → filter."""
        t0 = time.perf_counter()
        try:
            s = self.settings
            name_lower = channel.meta.name.lower()
            max_urls = s.get_max_urls_per_channel()

            try:
                alive_urls = self.source_manager.get_alive_urls(
                    channel.meta.name, s, limit=max_urls)
            except Exception:
                logger.exception("get_alive_urls")
                alive_urls = []

            for url in alive_urls:
                if self.is_stopped():
                    return None
                if s.is_blacklisted(url) or s.is_filtered_domain(url):
                    continue
                if URLUtils.validate_url(url) is None:
                    logger.info(
                        f"[REPL] {channel.meta.name}: alive_hit "
                        f"urls={len(alive_urls)} "
                        f"elapsed={time.perf_counter() - t0:.3f}s")
                    return url

            alt_urls = [a for a in channel.link.alternative_urls
                        if a and a.strip()
                        and not s.is_blacklisted(a)
                        and not s.is_filtered_domain(a)]

            alts = self.source_manager.search_channel(
                channel.meta.name, s, stop_token=self._stop_token,
                config=self.config)
            candidates: List[str] = [
                a.link.url for a in alts
                if a.link.url and a.link.url.strip()
                and not s.is_blacklisted(a.link.url)
                and not s.is_filtered_domain(a.link.url)
            ]

            seen: Set[str] = set()
            all_urls: List[str] = []
            for u in alt_urls + candidates:
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)

            if not all_urls:
                logger.info(
                    f"[REPL] {channel.meta.name}: urls=0, "
                    f"elapsed={time.perf_counter() - t0:.3f}s")
                return None

            all_urls = all_urls[:max_urls]

            trusted, unknown = self._filter_urls_by_cache(
                all_urls, name_lower)

            for u in trusted:
                if self.is_stopped():
                    return None
                if URLUtils.validate_url(u) is None:
                    logger.info(
                        f"[REPL] {channel.meta.name}: "
                        f"trusted={len(trusted)} found_trusted, "
                        f"elapsed={time.perf_counter() - t0:.3f}s")
                    return u

            logger.info(
                f"[REPL] {channel.meta.name}: urls={len(all_urls)}, "
                f"trusted={len(trusted)}, unknown={len(unknown)}, "
                f"elapsed={time.perf_counter() - t0:.3f}s, "
                f"found=False (cache-only)")
            return None
        except Exception:
            logger.exception(f"Ошибка поиска замены {channel.meta.name}")
            return None


class SourceUrlCheckWorker(BaseWorker):
    """Единственная массовая проверка URL."""

    source_check_progress = pyqtSignal(str, int, int)
    source_check_done = pyqtSignal(str, int, int)
    channel_checked = pyqtSignal(str, bool, str)

    def __init__(
        self,
        source_name: str,
        channels: List[ChannelData],
        cache_manager: Optional['CacheManager'],
        max_workers: int = SOURCE_CHECK_WORKERS_DEFAULT,
        timeout: int = SOURCE_CHECK_TIMEOUT_DEFAULT,
        trust_sec: int = SOURCE_CHECK_TRUST_SEC_DEFAULT,
        batch_size: int = SOURCE_CHECK_BATCH_SIZE_DEFAULT,
        stop_token: Optional['_StopToken'] = None,
    ):
        super().__init__()
        if stop_token is not None:
            self._stop_token = stop_token
        self.source_name = source_name
        self.channels = list(channels)
        self.cache_manager = cache_manager
        self.max_workers = max(1, min(int(max_workers), 32))
        self.timeout = max(int(timeout), 3)
        self.trust_sec = int(trust_sec)
        self.batch_size = max(1, int(batch_size))
        self.verify_ssl = False
        try:
            from ksenia_window import ApplicationCore
            self.verify_ssl = bool(
                ApplicationCore.instance().config.get('verify_ssl', False))
        except Exception:
            pass

        self._settings: Optional[LinkReplacementSettings] = None
        self._bl_names: Set[str] = set()
        self._bl_tvgs: Set[str] = set()
        self._skip_count_channel = 0
        self._skip_count_domain = 0
        self._skip_count_filtered = 0

    def _prepare_filters(self):
        try:
            from ksenia_window import ApplicationCore
            core = ApplicationCore.instance()
        except Exception:
            self._settings = None
            return
        self._settings = core.get_replacement_settings()
        if self._settings is None:
            self._settings = core.link_replacement_settings
        try:
            for bi in core.blacklist_manager.get_all():
                n = (bi.get('name') or '').strip().lower()
                t = (bi.get('tvg_id') or '').strip().lower()
                if n:
                    self._bl_names.add(n)
                if t:
                    self._bl_tvgs.add(t)
        except Exception:
            logger.exception("SourceUrlCheckWorker prepare bl")

    def _should_skip(self, ch: ChannelData) -> bool:
        n_low = (ch.meta.name or '').strip().lower()
        t_low = (ch.meta.tvg_id or '').strip().lower()
        if (n_low and n_low in self._bl_names) or \
           (t_low and t_low in self._bl_tvgs):
            self._skip_count_channel += 1
            return True
        url = ch.link.url
        if not url:
            return True
        if self._settings is not None:
            if self._settings.is_blacklisted(url):
                self._skip_count_domain += 1
                return True
            if self._settings.is_filtered_domain(url):
                self._skip_count_filtered += 1
                return True
        return False

    def run(self):
        try:
            total = len(self.channels)
            if total == 0:
                self.source_check_done.emit(self.source_name, 0, 0)
                return

            self._prepare_filters()

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
                        pairs, max_age_hours=CHECK_RESULT_CACHE_TTL_HOURS)
                except Exception:
                    logger.exception("SourceUrlCheckWorker batch read")
                    fresh = {}

            logger.info(
                f"SourceUrlCheckWorker[{self.source_name}]: "
                f"total={total}, after_filters={len(passed)} "
                f"(ch_bl={self._skip_count_channel}, "
                f"dom_bl={self._skip_count_domain}, "
                f"filtered={self._skip_count_filtered})")

            now = time.time()
            to_check: List[ChannelData] = []
            working = 0
            checked = 0
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

            self.source_check_progress.emit(self.source_name, checked, total)

            if not to_check:
                self.source_check_done.emit(
                    self.source_name, working, total)
                return

            lock = threading.Lock()
            pending: List[Tuple[str, str, bool, float, str, Optional[int]]] = []
            pending_lock = threading.Lock()

            def flush_batch(items):
                if not items or self.cache_manager is None:
                    return
                try:
                    self.cache_manager.save_check_results_batch(items)
                except Exception:
                    logger.exception("batch save")
                    for row in items:
                        try:
                            self.cache_manager.save_check_result(
                                row[0], row[1], row[2], row[3],
                                row[4], row[5])
                        except Exception:
                            pass

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
                        url, self.timeout, verify_ssl=self.verify_ssl,
                        stop_token=self._stop_token)
                except Exception as e:
                    ok, rt, msg, code = False, 0.0, \
                                          f"exception: {str(e)[:40]}", None
                if self.is_stopped():
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
                        ch.status.link_quality = LinkQuality.NOT_WORKING
                    else:
                        ch.status.link_quality = LinkQuality.UNKNOWN
                    st_txt, _sc = URLUtils.classify_status(
                        url, code, '' if ok else (msg or ''))
                    ch.status.status_text = st_txt
                    object.__setattr__(ch, 'modified_date', datetime.now())
                with pending_lock:
                    if ok is not None:
                        pending.append((
                            ch.meta.name, url, bool(ok),
                            (rt or 0.0) * 1000.0,
                            (msg or "")[:200], code,
                        ))
                    if len(pending) >= self.batch_size:
                        batch = pending[:]
                        pending.clear()
                    else:
                        batch = None
                if batch:
                    flush_batch(batch)

            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix=f"srcchk-{self.source_name[:16]}",
            )
            try:
                futures = [executor.submit(check_one, ch)
                           for ch in to_check]
                last_emit = 0
                for fut in concurrent.futures.as_completed(futures):
                    if self.is_stopped():
                        for f in futures:
                            f.cancel()
                        break
                    cur = checked
                    if cur - last_emit >= 5 or cur == len(to_check):
                        last_emit = cur
                        self.source_check_progress.emit(
                            self.source_name, cur, total)
            finally:
                with suppress(Exception):
                    executor.shutdown(wait=False, cancel_futures=True)

            with pending_lock:
                leftover = pending[:]
                pending.clear()
            if leftover and not self.is_stopped():
                flush_batch(leftover)

            self.source_check_done.emit(
                self.source_name, working, total)

        except Exception as e:
            logger.exception("SourceUrlCheckWorker")
            self.error.emit(str(e))
        finally:
            self.worker_done.emit()


class SourcesRefreshWorker(BaseWorker):
    """Объединённое «Обновить всё»: загрузка + проверка URL + rebuild.

    Трёхфазная шкала прогресса (0..100):
      • Фаза 1 (0..40%)  — загрузка источников
      • Фаза 2 (40..90%) — проверка URL
      • Фаза 3 (90..100%) — rebuild_alive_index
    """

    PHASE1_END = 40
    PHASE2_END = 90
    PHASE3_END = 100

    all_done = pyqtSignal(int, int)
    source_checked = pyqtSignal(str, int, int)
    channel_checked = pyqtSignal(str, str, bool, str)

    def __init__(self, sources: List[LinkSource],
                 manager: LinkSourceManager, config: Config,
                 check_urls: bool = True):
        super().__init__()
        self.sources = list(sources)
        self.manager = manager
        self.config = config
        self.check_urls = bool(check_urls)
        self._child_workers: List[SourceUrlCheckWorker] = []

        self._phase2_lock = threading.Lock()
        self._phase2_weights: Dict[str, int] = {}
        self._phase2_checked: Dict[str, int] = {}
        self._phase2_total: int = 0
        self._last_phase2_pct: int = self.PHASE1_END

    def stop(self):
        super().stop()
        for w in getattr(self, '_child_workers', []):
            with suppress(Exception):
                if w.isRunning():
                    w.stop()

    def _run_in_pool(self, items, worker_fn, max_workers=None):
        if not items:
            return
        if max_workers is None or max_workers <= 1:
            for item in items:
                if self.is_stopped():
                    return
                yield worker_fn(item)
            return
        ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="sources-load")
        try:
            futures = {ex.submit(worker_fn, item): item for item in items}
            for fut in concurrent.futures.as_completed(futures):
                if self.is_stopped():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    yield fut.result()
                except Exception:
                    logger.exception("_run_in_pool")
        finally:
            with suppress(Exception):
                ex.shutdown(wait=False, cancel_futures=True)

    def _load_one(self, source: LinkSource):
        self.manager.invalidate_cache(source.name)
        try:
            chs = self.manager.load_links_from_source(
                source, use_cache=False, config=self.config,
                stop_token=self._stop_token)
        except Exception:
            logger.exception(f"load_links_from_source {source.name}")
            chs = []
        return (len(chs) if chs else 0), source, chs

    def _on_source_check_done(self, name: str, working: int, total: int):
        try:
            self.manager.update_source_health(name, working)
        except Exception:
            logger.exception("update_source_health")
        with suppress(Exception):
            self.source_checked.emit(name, working, total)

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

    def run(self):
        processed = 0
        total = len(self.sources)
        success = 0
        try:
            if total == 0:
                self.all_done.emit(0, 0)
                return

            # === ФАЗА 1: загрузка ===
            loaded: List[Tuple[LinkSource, List[ChannelData]]] = []
            span1 = self.PHASE1_END
            for cnt, src, chs in self._run_in_pool(
                    self.sources, self._load_one,
                    max_workers=min(URL_CHECK_MAX_WORKERS, total)):
                processed += 1
                if cnt:
                    success += 1
                pct = int(processed / total * span1)
                self.progress.emit(
                    pct, self.PHASE3_END,
                    f"[1/3] Загрузка источников: {processed}/{total}")
                if src is not None:
                    loaded.append((src, chs or []))

            if self.is_stopped():
                self.all_done.emit(success, total)
                return

            if not self.check_urls:
                self._phase3_rebuild(loaded)
                self.all_done.emit(success, total)
                return

            max_workers = int(self.config.get(
                'source_check_workers', SOURCE_CHECK_WORKERS_DEFAULT))
            timeout = int(self.config.get(
                'source_check_timeout', SOURCE_CHECK_TIMEOUT_DEFAULT))
            trust_sec = int(self.config.get(
                'source_check_trust_sec', SOURCE_CHECK_TRUST_SEC_DEFAULT))
            batch_size = int(self.config.get(
                'source_check_batch_size', SOURCE_CHECK_BATCH_SIZE_DEFAULT))

            # === ФАЗА 2: проверка URL ===
            workers: List[SourceUrlCheckWorker] = []
            for src, chs in loaded:
                if self.is_stopped():
                    break
                if not chs:
                    continue
                w = SourceUrlCheckWorker(
                    source_name=src.name,
                    channels=chs,
                    cache_manager=self.manager.cache_manager,
                    max_workers=max_workers,
                    timeout=timeout,
                    trust_sec=trust_sec,
                    batch_size=batch_size,
                    stop_token=self._stop_token,
                )
                w.source_check_done.connect(self._on_source_check_done)
                w.source_check_progress.connect(self._on_child_progress)
                w.channel_checked.connect(
                    lambda n, ok_, m, s=src.name:
                        self.channel_checked.emit(s, n, ok_, m))
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

            while any(w.isRunning() for w in workers):
                if self.is_stopped():
                    for w in workers:
                        if w.isRunning():
                            w.stop()
                    break
                time.sleep(0.1)

            for w in workers:
                with suppress(Exception):
                    w.wait(5000)

            self._last_phase2_pct = self.PHASE2_END
            self.progress.emit(
                self.PHASE2_END, self.PHASE3_END,
                f"[2/3] Проверка URL: {self._phase2_total}/{self._phase2_total}")

            if self.is_stopped():
                self.all_done.emit(success, total)
                return

            # === ФАЗА 3: rebuild_alive_index ===
            self._phase3_rebuild(loaded)

            self.all_done.emit(success, total)
        except Exception as e:
            self.error.emit(f"Ошибка: {e}")
            logger.exception("SourcesRefreshWorker")
            with suppress(Exception):
                self.all_done.emit(success, total)
        finally:
            self._child_workers = []
            self.worker_done.emit()

    def _phase3_rebuild(self,
                        loaded: List[Tuple[LinkSource, List[ChannelData]]]):
        if not loaded:
            self.progress.emit(
                self.PHASE3_END, self.PHASE3_END, "[3/3] Готово")
            return
        span = self.PHASE3_END - self.PHASE2_END
        total3 = len(loaded)
        for i, (src, _chs) in enumerate(loaded):
            if self.is_stopped():
                break
            try:
                self.manager.rebuild_alive_index(src.name)
            except Exception:
                logger.exception(f"rebuild_alive_index({src.name})")
            pct = self.PHASE2_END + int((i + 1) / total3 * span)
            if pct > self.PHASE3_END:
                pct = self.PHASE3_END
            self.progress.emit(
                pct, self.PHASE3_END,
                f"[3/3] Перестройка индекса: {i + 1}/{total3}")
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

    def run(self):
        try:
            if not self.urls:
                self.epg_loaded.emit(0, [])
                return
            self.progress.emit(0, len(self.urls), "Загрузка EPG...")
            count, errors = self.epg_db.load_from_urls(
                self.urls, self.timeout, self._stop_token)
            self.epg_loaded.emit(count, errors)
        except Exception as e:
            self.error.emit(f"Ошибка EPG: {e}")
        finally:
            self.worker_done.emit()


class EPGMetadataApplyWorker(BaseWorker):
    applied = pyqtSignal(int, list)

    def __init__(self, channels: List[ChannelData], epg_db: EPGDatabase,
                 config: Optional[Config] = None):
        super().__init__()
        # Сохраняем только uid + имя + tvg_id + tvg_name (не весь объект)
        self._channels = [(ch.uid, ch.meta.name or "",
                           ch.meta.tvg_id or "",
                           ch.meta.tvg_name or "")
                          for ch in channels]
        self.epg_db = epg_db
        self.config = config
        if config is not None:
            self._fuzzy_enabled = bool(
                config.get('epg_fuzzy_match_enabled',
                           EPG_FUZZY_ENABLED_DEFAULT))
            self._fuzzy_threshold = float(
                config.get('epg_fuzzy_threshold',
                           EPG_FUZZY_THRESHOLD_DEFAULT))
            self._fuzzy_min_length = int(
                config.get('epg_fuzzy_min_length',
                           EPG_FUZZY_MIN_LENGTH_DEFAULT))
            self._fuzzy_min_gap = float(
                config.get('epg_fuzzy_min_gap',
                           EPG_FUZZY_MIN_GAP_DEFAULT))
        else:
            self._fuzzy_enabled = EPG_FUZZY_ENABLED_DEFAULT
            self._fuzzy_threshold = EPG_FUZZY_THRESHOLD_DEFAULT
            self._fuzzy_min_length = EPG_FUZZY_MIN_LENGTH_DEFAULT
            self._fuzzy_min_gap = EPG_FUZZY_MIN_GAP_DEFAULT

    def run(self):
        try:
            if not self.epg_db.has_channel_info:
                self.applied.emit(0, [])
                return

            total = len(self._channels)
            if total == 0:
                self.applied.emit(0, [])
                return

            threshold = (self._fuzzy_threshold
                         if self._fuzzy_enabled else 1.0)
            results: List[Tuple[int, str, str]] = []
            sources: Dict[int, str] = {}
            processed = 0

            # Переиспользуем один объект ChannelData
            tmp = ChannelData()
            for uid, name, tvg_id, tvg_name in self._channels:
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

                updates, src = self.epg_db.build_epg_metadata_updates(
                    tmp, threshold, self._fuzzy_min_length,
                    self._fuzzy_min_gap)
                for field, value in updates:
                    results.append((uid, field, value))
                if src:
                    sources[uid] = src

            for uid, src in sources.items():
                results.append((uid, 'epg_source', src))

            touched = len({r[0] for r in results})
            self.applied.emit(touched, results)
        except Exception as e:
            logger.exception("EPGMetadataApplyWorker")
            self.error.emit(f"Ошибка применения EPG-метаданных: {e}")
        finally:
            self.worker_done.emit()