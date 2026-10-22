# -*- coding: utf-8 -*-
"""Поиск источников IPTV-плейлистов через GitHub API.

Модуль НЕ содержит встроенных ссылок на плейлисты.
Все результаты получаются live-запросом к GitHub API в момент
выполнения поиска.

Используется в SourcesFinderDialog по кнопке
«🔍 Найти источники автоматически».

Сигнал завершения называется `search_finished`, чтобы не
конфликтовать с QThread.finished.
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


# GitHub API-эндпоинт для проверки файлов в организации
# m3uguide — это НЕ прямая ссылка на плейлист, а API-запрос
# содержимого репозитория. Сами URL плейлистов приходят
# в ответе API в момент выполнения.
_M3UGUIDE_REPO_API = (
    "https://api.github.com/repos/m3uguide/playlists/contents/")


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
    res = _HealthResult(url)

    headers: Dict[str, str] = {}
    status: Optional[int] = None
    head_error = ""

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
    """Ищет URL-ы плейлистов через GitHub API (live),
    проверяет доступность и свежесть.

    Никаких встроенных ссылок: всё, что попадает в результат,
    получено из ответа API в момент выполнения поиска.
    """

    progress = pyqtSignal(str)
    found = pyqtSignal(list)
    health_progress = pyqtSignal(int, int)
    rejected_found = pyqtSignal(list)
    search_finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, *, include_github: bool = True,
                 include_m3uguide: bool = True,
                 max_github: int = SOURCES_FINDER_MAX_GITHUB_DEFAULT,
                 max_m3uguide: int = SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT,
                 max_age_days: int = SOURCES_FINDER_MAX_AGE_DAYS,
                 health_workers: int = SOURCES_FINDER_HEALTH_WORKERS,
                 health_timeout: int = SOURCES_FINDER_HEALTH_TIMEOUT):
        super().__init__()
        self._stop = threading.Event()
        self.include_github = bool(include_github)
        self.include_m3uguide = bool(include_m3uguide)
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
            'Accept': ('application/vnd.github.v3+json, '
                       'application/json, */*'),
        })
        return s

    def _search_github(self, session: requests.Session,
                       out: List[Dict[str, Any]],
                       seen_urls: Set[str]):
        """Поиск через GitHub Search API — только live-запрос,
        без известных репозиториев."""
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
                            "https://api.github.com/repos/"
                            f"{full}/contents/",
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
                logger.debug(
                    f"GitHub search error ({query}): {e}")
            time.sleep(0.3)

    def _search_m3uguide_repo(self, session: requests.Session,
                              out: List[Dict[str, Any]],
                              seen_urls: Set[str]):
        """Поиск в репозитории m3uguide/playlists через API.

        Никаких хардкод-ссылок: список файлов приходит в ответе
        API, download_url формируется GitHub'ом в момент запроса.
        """
        if self._is_stopped():
            return
        self.progress.emit("m3u.guide: поиск файлов...")
        try:
            r = session.get(_M3UGUIDE_REPO_API, timeout=15)
            if r.status_code != 200:
                return
            items = r.json()
        except Exception as e:
            logger.debug("m3u.guide repo search: %s", e)
            return
        if not isinstance(items, list):
            return
        count = 0
        for item in items:
            if self._is_stopped() or count >= self.max_m3uguide:
                return
            name = item.get('name', '') or ''
            if not (name.endswith('.m3u')
                    or name.endswith('.m3u8')):
                continue
            url = item.get('download_url') or ''
            if not url or url in seen_urls:
                continue
            label = f"m3u.guide - {name}"
            if _is_excluded(label, url):
                continue
            seen_urls.add(url)
            out.append({
                'name': label, 'url': url,
                'source': 'm3uguide',
                'priority': 3, 'stars': 0,
            })
            count += 1

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

            if self.include_github and not self._is_stopped():
                self._search_github(session, out, seen_urls)
            if self.include_m3uguide and not self._is_stopped():
                self._search_m3uguide_repo(
                    session, out, seen_urls)

            github_items = [x for x in out
                            if x.get('source') == 'github']
            other_items = [x for x in out
                           if x.get('source') != 'github']
            github_items = github_items[:self.max_github]
            candidates = github_items + other_items

            if self._is_stopped() or not candidates:
                self.search_finished.emit([])
                return

            self.found.emit([dict(c) for c in candidates])

            self.progress.emit(
                f"Проверка {len(candidates)} источников...")
            valid, rejected = self._health_check_all(candidates)

            if self._is_stopped():
                self.search_finished.emit([])
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
            self.search_finished.emit(valid)
        except Exception as e:
            logger.exception("SourcesFinderWorker")
            self.error.emit(str(e))
        finally:
            try:
                session.close()
            except Exception:
                pass