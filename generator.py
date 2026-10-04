# -*- coding: utf-8 -*-
"""Поиск источников IPTV-плейлистов в интернете.

Модуль НЕ парсит плейлисты и НЕ проверяет потоки каналов — только
находит URL-ы .m3u/.m3u8, проверяет их доступность и свежесть.

Используется в SourcesFinderDialog по кнопке
«🔍 Найти источники автоматически».
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from constants import (
    SOURCES_FINDER_EXCLUDE_KEYWORDS,
    SOURCES_FINDER_MAX_GITHUB_DEFAULT,
    SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT,
    SOURCES_FINDER_MAX_AGE_DAYS,
    SOURCES_FINDER_HEALTH_WORKERS,
    SOURCES_FINDER_HEALTH_TIMEOUT,
    SOURCES_FINDER_HEALTH_MAX_BYTES,
)
from paths import logger


def _is_excluded(name: str, url: str) -> bool:
    text = f"{name} {url}".lower()
    return any(k in text for k in SOURCES_FINDER_EXCLUDE_KEYWORDS)


KNOWN_REPOS = [
    "iptv-org/iptv",
    "Free-TV/IPTV",
    "smolnp/IPTVru",
    "Spirt007/Tvru",
    "Azlux/iptv",
    "iptv-ru/iptv",
]

FAST_SOURCES: List[Tuple[str, str]] = [
    ("Pluto TV (US)",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_pluto.m3u8"),
    ("Samsung TV Plus (US)",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_samsung.m3u8"),
    ("Plex (US)",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_plex.m3u8"),
    ("Tubi (US)",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_tubi.m3u8"),
    ("Roku Channel (US)",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_roku.m3u8"),
    ("Xumo Play (US)",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_xumo.m3u8"),
    ("Русские каналы",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/playlists/ru.m3u"),
    ("СНГ каналы",
     "https://raw.githubusercontent.com/iptv-org/iptv/master/playlists/cis.m3u"),
]

M3UGUIDE_SOURCES: List[Tuple[str, str]] = [
    ("Россия",
     "https://raw.githubusercontent.com/m3uguide/playlists/main/Russia.m3u"),
    ("Мир",
     "https://raw.githubusercontent.com/m3uguide/playlists/main/World.m3u"),
]


def _parse_http_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _days_since(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    return (now - dt.astimezone(timezone.utc)).total_seconds() / 86400.0


def _human_age(days: float) -> str:
    if days < 1:
        hours = int(days * 24)
        return f"{hours} ч" if hours > 0 else "только что"
    if days < 30:
        return f"{int(days)} дн"
    if days < 365:
        return f"{int(days / 30)} мес"
    return f"{days / 365:.1f} лет"


class _HealthResult:
    __slots__ = ('url', 'ok', 'status_code', 'error',
                 'last_modified', 'age_days', 'bytes_checked')

    def __init__(self, url: str):
        self.url = url
        self.ok: bool = False
        self.status_code: Optional[int] = None
        self.error: str = ""
        self.last_modified: Optional[datetime] = None
        self.age_days: Optional[float] = None
        self.bytes_checked: int = 0


def _check_source_health(url: str, *,
                         timeout: int,
                         max_bytes: int,
                         max_age_days: int,
                         stop_event: Optional[threading.Event] = None
                         ) -> _HealthResult:
    """Проверить URL: доступность + свежесть.

    1. HEAD-запрос для заголовков.
    2. Если HEAD не дал Last-Modified или вернул 405/501 — GET с stream.
    3. Если Last-Modified есть и старше max_age_days — отбраковка.
       Если Last-Modified нет — считаем «без даты» и допускаем.
    """
    res = _HealthResult(url)

    headers: Dict[str, str] = {}
    status: Optional[int] = None
    head_error = ""

    # --- HEAD ---
    try:
        with requests.Session() as session:
            session.headers.update({
                'User-Agent': 'Ksenia-M3U-Editor/SourceHealth',
                'Accept': '*/*',
            })
            try:
                r = session.head(url, timeout=timeout,
                                 allow_redirects=True, verify=False)
                status = r.status_code
                headers = {k.lower(): v for k, v in r.headers.items()}
            except requests.Timeout:
                head_error = "timeout (HEAD)"
            except requests.ConnectionError:
                head_error = "connection error (HEAD)"
            except Exception as e:
                head_error = f"HEAD: {str(e)[:80]}"

            # --- Fallback на GET ---
            need_get = (
                status is None
                or status in (405, 501)
                or 'last-modified' not in headers
                or 'content-length' not in headers
            )
            if need_get:
                try:
                    with session.get(url, timeout=timeout, stream=True,
                                     allow_redirects=True,
                                     verify=False) as r:
                        status = r.status_code
                        headers = {k.lower(): v
                                   for k, v in r.headers.items()}
                        if status == 200:
                            buf = bytearray()
                            for chunk in r.iter_content(chunk_size=8192):
                                if (stop_event is not None
                                        and stop_event.is_set()):
                                    break
                                buf.extend(chunk)
                                if len(buf) >= max_bytes:
                                    break
                            res.bytes_checked = len(buf)
                except requests.Timeout:
                    if not head_error:
                        head_error = "timeout (GET)"
                except requests.ConnectionError:
                    if not head_error:
                        head_error = "connection error (GET)"
                except Exception as e:
                    if not head_error:
                        head_error = f"GET: {str(e)[:80]}"
    except Exception as e:
        res.error = f"session: {str(e)[:80]}"
        return res

    if status is None:
        res.error = head_error or "не удалось получить ответ"
        return res

    res.status_code = status

    if not (200 <= status < 400):
        if status == 403:
            res.error = "403 (доступ запрещён)"
        elif status == 404:
            res.error = "404 (не найдено)"
        else:
            res.error = f"HTTP {status}"
        return res

    # --- Свежесть ---
    last_mod = _parse_http_date(headers.get('last-modified'))
    if last_mod is None:
        last_mod = _parse_http_date(headers.get('date'))

    if last_mod is not None:
        age = _days_since(last_mod)
        res.last_modified = last_mod
        res.age_days = age
        if age > max_age_days:
            res.error = (f"устарел ({_human_age(age)}, "
                         f"лимит {max_age_days} дн)")
            return res

    res.ok = True
    return res


class SourcesFinderWorker(QThread):
    """Ищет URL-ы плейлистов, проверяет доступность и свежесть."""

    progress = pyqtSignal(str)
    found = pyqtSignal(list)
    health_progress = pyqtSignal(int, int)
    rejected_found = pyqtSignal(list)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, *, include_github: bool = True,
                 include_m3uguide: bool = True,
                 include_fast: bool = True,
                 max_github: int = SOURCES_FINDER_MAX_GITHUB_DEFAULT,
                 max_m3uguide: int = SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT,
                 max_age_days: int = SOURCES_FINDER_MAX_AGE_DAYS,
                 health_workers: int = SOURCES_FINDER_HEALTH_WORKERS,
                 health_timeout: int = SOURCES_FINDER_HEALTH_TIMEOUT):
        super().__init__()
        self._stop = threading.Event()
        self.include_github = bool(include_github)
        self.include_m3uguide = bool(include_m3uguide)
        self.include_fast = bool(include_fast)
        self.max_github = max(1, int(max_github))
        self.max_m3uguide = max(1, int(max_m3uguide))
        self.max_age_days = max(1, int(max_age_days))
        self.health_workers = max(1, min(int(health_workers), 16))
        self.health_timeout = max(2, int(health_timeout))

    def stop(self):
        self._stop.set()

    def _is_stopped(self) -> bool:
        return self._stop.is_set()

    @staticmethod
    def _session() -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Ksenia-M3U-Editor/SourcesFinder',
            'Accept': 'application/vnd.github.v3+json, application/json, */*',
        })
        return s

    def _scan_known_repos(self, session: requests.Session,
                          out: List[Dict[str, Any]],
                          seen_urls: Set[str]):
        for repo in KNOWN_REPOS:
            if self._is_stopped():
                return
            self.progress.emit(f"GitHub: {repo}")
            try:
                r = session.get(
                    f"https://api.github.com/repos/{repo}/contents/",
                    timeout=10)
                if r.status_code != 200:
                    continue
                contents = r.json()
                if not isinstance(contents, list):
                    continue
                for item in contents:
                    if self._is_stopped():
                        return
                    name = item.get('name', '') or ''
                    if not (name.endswith('.m3u')
                            or name.endswith('.m3u8')):
                        continue
                    url = item.get('download_url') or ''
                    if not url or url in seen_urls:
                        continue
                    label = f"{repo} - {name}"
                    if _is_excluded(label, url):
                        continue
                    seen_urls.add(url)
                    out.append({
                        'name': label,
                        'url': url,
                        'source': 'github',
                        'priority': 4,
                        'stars': 0,
                    })
            except Exception as e:
                logger.debug(f"GitHub known-repo error {repo}: {e}")
            time.sleep(0.3)

    def _search_github(self, session: requests.Session,
                       out: List[Dict[str, Any]],
                       seen_urls: Set[str]):
        queries = [
            "iptv m3u russia",
            "russian iptv playlist",
            "iptv ru m3u",
        ]
        for query in queries:
            if self._is_stopped():
                return
            self.progress.emit(f"GitHub поиск: {query}")
            try:
                r = session.get(
                    "https://api.github.com/search/repositories"
                    f"?q={quote(query)}&sort=stars&per_page=10",
                    timeout=15)
                if r.status_code != 200:
                    continue
                items = r.json().get('items', []) or []
                for repo in items:
                    if self._is_stopped():
                        return
                    full = repo.get('full_name') or ''
                    if not full:
                        continue
                    stars = int(repo.get('stargazers_count') or 0)
                    try:
                        contents = session.get(
                            f"https://api.github.com/repos/{full}/contents/",
                            timeout=10).json()
                    except Exception:
                        continue
                    if not isinstance(contents, list):
                        continue
                    for item in contents:
                        if self._is_stopped():
                            return
                        name = item.get('name', '') or ''
                        if not (name.endswith('.m3u')
                                or name.endswith('.m3u8')):
                            continue
                        url = item.get('download_url') or ''
                        if not url or url in seen_urls:
                            continue
                        label = f"{full} - {name}"
                        if _is_excluded(label, url):
                            continue
                        seen_urls.add(url)
                        out.append({
                            'name': label,
                            'url': url,
                            'source': 'github',
                            'priority': 5,
                            'stars': stars,
                        })
            except Exception as e:
                logger.debug(f"GitHub search error ({query}): {e}")
            time.sleep(0.3)

    def _scan_m3uguide(self, out: List[Dict[str, Any]],
                       seen_urls: Set[str]):
        count = 0
        for name, url in M3UGUIDE_SOURCES:
            if self._is_stopped() or count >= self.max_m3uguide:
                return
            self.progress.emit(f"m3u.guide: {name}")
            label = f"m3u.guide - {name}"
            if _is_excluded(label, url) or url in seen_urls:
                continue
            seen_urls.add(url)
            out.append({
                'name': label, 'url': url, 'source': 'm3uguide',
                'priority': 3, 'stars': 0,
            })
            count += 1
            time.sleep(0.2)

    def _scan_fast(self, out: List[Dict[str, Any]],
                   seen_urls: Set[str]):
        for name, url in FAST_SOURCES:
            if self._is_stopped():
                return
            self.progress.emit(f"FAST: {name}")
            label = f"FAST - {name}"
            if _is_excluded(label, url) or url in seen_urls:
                continue
            seen_urls.add(url)
            out.append({
                'name': label, 'url': url, 'source': 'fast',
                'priority': 3, 'stars': 0,
            })
            time.sleep(0.15)

    def _health_check_all(self, candidates: List[Dict[str, Any]]
                          ) -> Tuple[List[Dict[str, Any]],
                                     List[Dict[str, Any]]]:
        valid: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        total = len(candidates)
        if total == 0:
            return valid, rejected

        done = 0
        lock = threading.Lock()
        max_bytes = SOURCES_FINDER_HEALTH_MAX_BYTES

        def _one(item: Dict[str, Any]):
            health = _check_source_health(
                item['url'],
                timeout=self.health_timeout,
                max_bytes=max_bytes,
                max_age_days=self.max_age_days,
                stop_event=self._stop,
            )
            enriched = dict(item)
            enriched['age_days'] = health.age_days
            enriched['last_modified'] = (
                health.last_modified.isoformat()
                if health.last_modified else None
            )
            enriched['status_code'] = health.status_code

            if health.ok:
                return enriched, None
            enriched['error'] = health.error
            return None, enriched

        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.health_workers,
                    thread_name_prefix="health") as ex:
                futures = [ex.submit(_one, c) for c in candidates]
                for fut in concurrent.futures.as_completed(futures):
                    if self._is_stopped():
                        # Не break, а продолжаем ждать завершения,
                        # но не обрабатываем результаты.
                        continue
                    try:
                        ok_item, rej_item = fut.result()
                    except Exception as e:
                        logger.debug(f"health worker: {e}")
                        ok_item, rej_item = None, None
                    with lock:
                        done += 1
                        if ok_item is not None:
                            valid.append(ok_item)
                        if rej_item is not None:
                            rejected.append(rej_item)
                    self.health_progress.emit(done, total)
        except Exception as e:
            logger.exception("_health_check_all")
            self.error.emit(f"Ошибка проверки: {e}")

        return valid, rejected

    def run(self):
        session = self._session()
        try:
            out: List[Dict[str, Any]] = []
            seen_urls: Set[str] = set()

            if self.include_github:
                self._scan_known_repos(session, out, seen_urls)
            if self.include_github and not self._is_stopped():
                self._search_github(session, out, seen_urls)
            if self.include_m3uguide and not self._is_stopped():
                self._scan_m3uguide(out, seen_urls)
            if self.include_fast and not self._is_stopped():
                self._scan_fast(out, seen_urls)

            github_items = [x for x in out if x.get('source') == 'github']
            other_items = [x for x in out if x.get('source') != 'github']
            github_items = github_items[:self.max_github]
            candidates = github_items + other_items

            if self._is_stopped() or not candidates:
                self.finished.emit([])
                return

            self.found.emit([dict(c) for c in candidates])

            self.progress.emit(
                f"Проверка {len(candidates)} источников...")
            valid, rejected = self._health_check_all(candidates)

            if self._is_stopped():
                self.finished.emit([])
                return

            if rejected:
                self.rejected_found.emit(rejected)

            def _key(x: Dict[str, Any]):
                age = x.get('age_days')
                age_key = age if age is not None else 15.0
                return (-(x.get('stars') or 0),
                        int(x.get('priority') or 5),
                        age_key,
                        (x.get('name') or '').lower())

            valid.sort(key=_key)

            self.progress.emit(
                f"✓ Готово: {len(valid)} доступных, "
                f"{len(rejected)} отфильтровано")
            self.finished.emit(valid)
        except Exception as e:
            logger.exception("SourcesFinderWorker")
            self.error.emit(str(e))
        finally:
            try:
                session.close()
            except Exception:
                pass