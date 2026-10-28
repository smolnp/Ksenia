# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""BaseJsonStore, StableStateManager, CacheManager."""
from __future__ import annotations

import atexit
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import suppress
from typing import Any, Dict, List, Optional, Tuple

from constants import (STABLE_STATE_FILE, MIN_SQLITE_VERSION,
    CHECK_RESULT_CACHE_TTL_HOURS, EPG_CACHE_TTL_HOURS,
    SQLITE_BATCH_CHUNK)
from logging_setup import logger
from paths import Paths


def _check_sqlite_version() -> None:
    v = sqlite3.sqlite_version_info
    if v < MIN_SQLITE_VERSION:
        raise RuntimeError(
            f"SQLite {v} слишком старый. Требуется ≥ "
            f"{'.'.join(map(str, MIN_SQLITE_VERSION))}")


class BaseJsonStore:
    def __init__(self, path: str, default: Any):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(default)
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, type(self._data)):
                self._data = data
            else:
                logger.warning(
                    "BaseJsonStore %s: type mismatch "
                    "(expected %s, got %s) — kept default",
                    self.path, type(self._data).__name__,
                    type(data).__name__)
        except Exception:
            logger.exception("BaseJsonStore load: %s", self.path)

    def save(self) -> bool:
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False,
                              indent=2)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except Exception:
                logger.exception("BaseJsonStore save: %s", self.path)
                with suppress(OSError):
                    if os.path.exists(tmp):
                        os.remove(tmp)
                return False


class StableStateManager:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, STABLE_STATE_FILE)
        self._lock = threading.RLock()
        self._data: Dict[str, str] = {}
        self._dirty = False
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {str(k): str(v) for k, v in data.items()}
        except Exception:
            logger.exception("StableState load")

    def save(self) -> bool:
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False,
                              indent=2)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, self.path)
                self._dirty = False
                return True
            except Exception:
                logger.exception("StableState save")
                with suppress(OSError):
                    if os.path.exists(tmp):
                        os.remove(tmp)
                return False

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, url: str):
        with self._lock:
            self._data[key] = url
            self._dirty = True


class CacheManager:
    @classmethod
    def _register_atexit_for(cls, instance: 'CacheManager'):
        with cls._atexit_lock:
            if cls._atexit_registered:
                return
            atexit.register(instance._close_all_connections)
            cls._atexit_registered = True

    _atexit_registered = False
    _atexit_lock = threading.Lock()

    def __init__(self, cache_dir: str = "cache"):
        _check_sqlite_version()
        config_dir = Paths.get_config_dir()
        self.cache_dir = os.path.join(config_dir, cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.db_path = os.path.join(self.cache_dir,
                                     "editor_cache.db")
        self.link_cache_dir = os.path.join(self.cache_dir,
                                            "link_cache")
        os.makedirs(self.link_cache_dir, exist_ok=True)
        self._link_cache_lock = threading.RLock()
        self._link_cache_put_counter = 0
        self.link_cache_max_bytes = 64 * 1024 * 1024
        self.link_cache_max_files = 10000

        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._connect_lock = threading.RLock()
        self._all_connections: List[sqlite3.Connection] = []
        self._all_connections_lock = threading.RLock()
        self._shutting_down = False

        self._register_atexit_for(self)
        self._init_db()

    def _close_all_connections(self):
        with self._all_connections_lock:
            conns = list(self._all_connections)
            self._all_connections.clear()
        for conn in conns:
            with suppress(Exception):
                conn.close()

    def reset_all_connections(self):
        self._shutting_down = True
        with self._connect_lock:
            self._close_all_connections()
        self._local.conn = None

    def _connect(self) -> sqlite3.Connection:
        if self._shutting_down:
            raise sqlite3.ProgrammingError(
                "CacheManager is shutting down")
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            return conn
        with self._connect_lock:
            conn = getattr(self._local, 'conn', None)
            if conn is not None:
                return conn
            conn = sqlite3.connect(self.db_path, timeout=30,
                                    check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    logger.warning("SQLite WAL not available")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.row_factory = sqlite3.Row
            except Exception:
                with suppress(Exception):
                    conn.close()
                raise
            self._local.conn = conn
            with self._all_connections_lock:
                self._all_connections.append(conn)
            return conn

    def _init_db(self):
        with self._init_lock:
            conn = self._connect()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS epg_entries (
                    channel_id TEXT NOT NULL,
                    start TEXT,
                    stop TEXT,
                    title TEXT,
                    description TEXT,
                    category TEXT,
                    source TEXT,
                    cached_at REAL DEFAULT 0,
                    PRIMARY KEY (channel_id, start, stop)
                );
                CREATE INDEX IF NOT EXISTS idx_epg_channel
                    ON epg_entries(channel_id);
                CREATE INDEX IF NOT EXISTS idx_epg_time
                    ON epg_entries(start, stop);
                CREATE INDEX IF NOT EXISTS idx_epg_cached
                    ON epg_entries(cached_at);

                CREATE TABLE IF NOT EXISTS epg_channels (
                    channel_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    icon TEXT,
                    lcn TEXT,
                    source TEXT,
                    cached_at REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_epg_channels_cached
                    ON epg_channels(cached_at);

                CREATE TABLE IF NOT EXISTS url_status_cache (
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    alive INTEGER DEFAULT 0,
                    response_ms REAL DEFAULT 0,
                    last_check REAL DEFAULT 0,
                    status_text TEXT DEFAULT '',
                    status_code INTEGER,
                    successes INTEGER DEFAULT 0,
                    failures INTEGER DEFAULT 0,
                    PRIMARY KEY (name, url)
                );

                CREATE TABLE IF NOT EXISTS normalized_index (
                    source TEXT NOT NULL,
                    search_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    tvg_id TEXT DEFAULT '',
                    url TEXT NOT NULL,
                    priority INTEGER DEFAULT 5,
                    settings_fp TEXT NOT NULL,
                    cached_at REAL DEFAULT 0,
                    PRIMARY KEY (source, search_key, url)
                );
                CREATE INDEX IF NOT EXISTS idx_norm_key
                    ON normalized_index(search_key);
                CREATE INDEX IF NOT EXISTS idx_norm_tvg
                    ON normalized_index(tvg_id);
                CREATE INDEX IF NOT EXISTS idx_norm_source
                    ON normalized_index(source);
                CREATE INDEX IF NOT EXISTS idx_norm_cached
                    ON normalized_index(cached_at);
            """)
            conn.commit()

    @staticmethod
    def _row_to_check_dict(row: sqlite3.Row) -> Dict[str, Any]:
        sc = row['status_code']
        return {
            'alive': bool(row['alive']),
            'response_ms': float(row['response_ms'] or 0),
            'last_check': float(row['last_check'] or 0),
            'status_text': row['status_text'] or '',
            'status_code': sc if sc is not None and sc >= 0 else None,
            'successes': int(row['successes'] or 0),
            'failures': int(row['failures'] or 0),
        }

    @staticmethod
    def _upsert_sql() -> str:
        return """
            INSERT INTO url_status_cache
                (name, url, alive, response_ms, last_check,
                 status_text, status_code, successes, failures)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, url) DO UPDATE SET
                alive = excluded.alive,
                response_ms = excluded.response_ms,
                last_check = excluded.last_check,
                status_text = excluded.status_text,
                status_code = excluded.status_code,
                successes = url_status_cache.successes
                    + excluded.successes,
                failures = url_status_cache.failures
                    + excluded.failures
        """

    def save_check_result(self, name: str, url: str, alive: bool,
                          response_ms: float, status_text: str = "",
                          status_code: Optional[int] = None) -> None:
        if not name or not url:
            return
        try:
            conn = self._connect()
            with conn:
                conn.execute(self._upsert_sql(), (
                    name.lower(), url, 1 if alive else 0,
                    response_ms, time.time(), status_text,
                    status_code if status_code is not None else -1,
                    1 if alive else 0, 0 if alive else 1))
        except sqlite3.ProgrammingError:
            pass
        except Exception:
            logger.warning("save_check_result failed: %s", url)

    def get_check_result(self, name: str, url: str,
                         max_age_hours: int = 24
                         ) -> Optional[Dict[str, Any]]:
        if not name or not url:
            return None
        try:
            cutoff = time.time() - max_age_hours * 3600
            conn = self._connect()
            row = conn.execute("""
                SELECT alive, response_ms, last_check, status_text,
                       status_code, successes, failures
                FROM url_status_cache
                WHERE name=? AND url=? AND last_check >= ?
            """, (name.lower(), url, cutoff)).fetchone()
            return self._row_to_check_dict(row) if row else None
        except sqlite3.ProgrammingError:
            return None
        except Exception:
            return None

    def get_check_results_batch(
            self, pairs: List[Tuple[str, str]],
            max_age_hours: int = CHECK_RESULT_CACHE_TTL_HOURS
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if not pairs:
            return {}
        cutoff = time.time() - max_age_hours * 3600
        unique = list({(n.lower(), u) for n, u in pairs if n and u})
        if not unique:
            return {}
        result: Dict[Tuple[str, str], Dict[str, Any]] = {}
        try:
            conn = self._connect()
            for i in range(0, len(unique), SQLITE_BATCH_CHUNK):
                chunk = unique[i:i + SQLITE_BATCH_CHUNK]
                placeholders = ",".join(["(?,?)"] * len(chunk))
                flat = [x for p in chunk for x in p] + [cutoff]
                rows = conn.execute(f"""
                    SELECT name, url, alive, response_ms,
                           last_check, status_text, status_code,
                           successes, failures
                    FROM url_status_cache
                    WHERE (name, url) IN ({placeholders})
                      AND last_check >= ?
                """, flat).fetchall()
                for row in rows:
                    result[(row['name'], row['url'])] = \
                        self._row_to_check_dict(row)
        except sqlite3.ProgrammingError:
            return {}
        except Exception:
            logger.debug("get_check_results_batch failed")
        return result

    def save_check_results_batch(
            self,
            results: List[Tuple[str, str, bool, float, str,
                                Optional[int]]]) -> None:
        if not results:
            return
        now = time.time()
        rows = []
        for name, url, alive, rt, st, sc in results:
            if not name or not url:
                continue
            rows.append((
                name.lower(), url, 1 if alive else 0, rt, now,
                st or "", sc if sc is not None else -1,
                1 if alive else 0, 0 if alive else 1))
        if not rows:
            return
        try:
            conn = self._connect()
            with conn:
                conn.executemany(self._upsert_sql(), rows)
        except sqlite3.ProgrammingError:
            return
        except Exception:
            logger.warning("save_check_results_batch failed: %d",
                           len(rows))
            for r in rows:
                try:
                    self.save_check_result(r[0], r[1], bool(r[2]),
                                            r[3], r[4], r[5])
                except Exception:
                    pass

    def cleanup_old(self, days: int = 30) -> int:
        cutoff = time.time() - days * 86400
        try:
            conn = self._connect()
            with conn:
                n = conn.execute(
                    "DELETE FROM url_status_cache "
                    "WHERE last_check < ?", (cutoff,)).rowcount
                n += conn.execute(
                    "DELETE FROM epg_entries WHERE cached_at < ?",
                    (cutoff,)).rowcount
                n += conn.execute(
                    "DELETE FROM epg_channels WHERE cached_at < ?",
                    (cutoff,)).rowcount
                n += conn.execute(
                    "DELETE FROM normalized_index "
                    "WHERE cached_at < ?", (cutoff,)).rowcount
                return n
        except sqlite3.ProgrammingError:
            return 0
        except Exception:
            logger.warning("cache cleanup failed")
            return 0

    def clear(self):
        try:
            conn = self._connect()
            with conn:
                conn.execute("DELETE FROM url_status_cache")
        except Exception:
            pass

    def vacuum(self):
        try:
            self._connect().execute("VACUUM")
        except sqlite3.ProgrammingError:
            pass
        except Exception as e:
            logger.warning("VACUUM failed: %s", e)

    def close_thread_connection(self):
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            with suppress(Exception):
                conn.close()
            self._local.conn = None
            with self._all_connections_lock:
                if conn in self._all_connections:
                    self._all_connections.remove(conn)

    def save_epg_entries(self, entries: List[Dict[str, Any]],
                         source: str = ""):
        if not entries:
            return
        try:
            now = time.time()
            conn = self._connect()
            with conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO epg_entries
                        (channel_id, start, stop, title, description,
                         category, source, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (e.get('channel_id', ''), e.get('start', ''),
                     e.get('stop', ''), e.get('title', ''),
                     e.get('desc', ''), e.get('category', ''),
                     source, now) for e in entries])
        except sqlite3.ProgrammingError:
            pass
        except Exception:
            logger.warning("save_epg_entries failed")

    def load_epg_entries(
            self, max_age_hours: int = EPG_CACHE_TTL_HOURS
    ) -> List[Dict[str, Any]]:
        try:
            cutoff = time.time() - max_age_hours * 3600
            conn = self._connect()
            rows = conn.execute("""
                SELECT channel_id, start, stop, title, description,
                       category, source
                FROM epg_entries
                WHERE start IS NOT NULL AND start != ''
                  AND (cached_at = 0 OR cached_at >= ?)
            """, (cutoff,)).fetchall()
            return [{
                'channel_id': r['channel_id'], 'start': r['start'],
                'stop': r['stop'], 'title': r['title'],
                'desc': r['description'],
                'category': r['category'], 'source': r['source'],
            } for r in rows]
        except sqlite3.ProgrammingError:
            return []
        except Exception:
            return []

    def save_epg_channels(self, entries: List[Dict[str, Any]],
                          source: str = ""):
        if not entries:
            return
        try:
            now = time.time()
            conn = self._connect()
            with conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO epg_channels
                        (channel_id, display_name, icon, lcn,
                         source, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [
                    (e.get('channel_id', ''),
                     e.get('display_name', ''),
                     e.get('icon', ''), e.get('lcn', ''),
                     source, now) for e in entries])
        except sqlite3.ProgrammingError:
            pass
        except Exception:
            logger.warning("save_epg_channels failed")

    def load_epg_channels(
            self, max_age_hours: int = EPG_CACHE_TTL_HOURS
    ) -> List[Dict[str, Any]]:
        try:
            cutoff = time.time() - max_age_hours * 3600
            conn = self._connect()
            rows = conn.execute("""
                SELECT channel_id, display_name, icon, lcn, source
                FROM epg_channels
                WHERE (cached_at = 0 OR cached_at >= ?)
            """, (cutoff,)).fetchall()
            return [{
                'channel_id': r['channel_id'],
                'display_name': r['display_name'],
                'icon': r['icon'], 'lcn': r['lcn'],
                'source': r['source'],
            } for r in rows]
        except sqlite3.ProgrammingError:
            return []
        except Exception:
            return []

    def clear_epg(self):
        try:
            conn = self._connect()
            with conn:
                conn.execute("DELETE FROM epg_entries")
                conn.execute("DELETE FROM epg_channels")
        except sqlite3.ProgrammingError:
            pass
        except Exception:
            pass

    def save_normalized_index(
            self,
            rows: List[Dict[str, Any]],
            source: str,
            settings_fp: str,
            replace_source: bool = True,
    ) -> int:
        if not source or not settings_fp:
            return 0
        if not rows:
            if replace_source:
                try:
                    conn = self._connect()
                    with conn:
                        conn.execute(
                            "DELETE FROM normalized_index "
                            "WHERE source = ?", (source,))
                except Exception:
                    pass
            return 0
        now = time.time()
        prepared: List[Tuple[str, str, str, str, str, int, str,
                              float]] = []
        seen: set = set()
        for r in rows:
            sk = (r.get('search_key') or '').strip()
            name = (r.get('name') or '').strip()
            url = (r.get('url') or '').strip()
            if not sk or not name or not url:
                continue
            key = (sk, url)
            if key in seen:
                continue
            seen.add(key)
            prepared.append((
                source, sk, name,
                (r.get('tvg_id') or '').strip(),
                url, int(r.get('priority') or 5),
                settings_fp, now))
        if not prepared:
            if replace_source:
                try:
                    conn = self._connect()
                    with conn:
                        conn.execute(
                            "DELETE FROM normalized_index "
                            "WHERE source = ?", (source,))
                except Exception:
                    pass
            return 0
        try:
            conn = self._connect()
            with conn:
                if replace_source:
                    conn.execute(
                        "DELETE FROM normalized_index "
                        "WHERE source = ?", (source,))
                conn.executemany("""
                    INSERT OR REPLACE INTO normalized_index
                        (source, search_key, name, tvg_id, url,
                         priority, settings_fp, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, prepared)
            return len(prepared)
        except sqlite3.ProgrammingError:
            return 0
        except Exception:
            logger.warning("save_normalized_index failed: %s",
                            source)
            return 0

    def load_normalized_index(
            self,
            settings_fp: str,
            max_age_hours: int = 24,
            source: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        if not settings_fp:
            return {}
        cutoff = time.time() - max_age_hours * 3600
        result: Dict[str, List[Dict[str, Any]]] = {}
        try:
            conn = self._connect()
            if source is not None:
                rows = conn.execute("""
                    SELECT source, search_key, name, tvg_id, url,
                           priority
                    FROM normalized_index
                    WHERE source = ?
                      AND settings_fp = ?
                      AND (cached_at = 0 OR cached_at >= ?)
                """, (source, settings_fp, cutoff)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT source, search_key, name, tvg_id, url,
                           priority
                    FROM normalized_index
                    WHERE settings_fp = ?
                      AND (cached_at = 0 OR cached_at >= ?)
                """, (settings_fp, cutoff)).fetchall()
            for r in rows:
                src = r['source']
                result.setdefault(src, []).append({
                    'search_key': r['search_key'],
                    'name': r['name'],
                    'tvg_id': r['tvg_id'] or '',
                    'url': r['url'],
                    'priority': int(r['priority'] or 5),
                })
        except sqlite3.ProgrammingError:
            return {}
        except Exception:
            logger.debug("load_normalized_index failed")
        return result

    def clear_normalized_index(
            self, source: Optional[str] = None) -> int:
        try:
            conn = self._connect()
            with conn:
                if source is None:
                    n = conn.execute(
                        "DELETE FROM normalized_index").rowcount
                else:
                    n = conn.execute(
                        "DELETE FROM normalized_index "
                        "WHERE source = ?", (source,)).rowcount
                return n
        except sqlite3.ProgrammingError:
            return 0
        except Exception:
            logger.warning("clear_normalized_index failed")
            return 0

    def get_normalized_index_stats(
            self,
            max_age_hours: int = 24,
    ) -> Dict[str, Any]:
        try:
            conn = self._connect()
            cutoff = time.time() - max_age_hours * 3600
            count = conn.execute(
                "SELECT COUNT(*) FROM normalized_index"
            ).fetchone()[0]
            if count == 0:
                return {
                    'count': 0, 'sources': 0,
                    'newest_ts': 0.0, 'oldest_ts': 0.0,
                    'fresh_count': 0, 'settings_fps': [],
                }
            sources = conn.execute(
                "SELECT COUNT(DISTINCT source) FROM normalized_index"
            ).fetchone()[0]
            newest = conn.execute(
                "SELECT MAX(cached_at) FROM normalized_index"
            ).fetchone()[0] or 0.0
            oldest = conn.execute(
                "SELECT MIN(cached_at) FROM normalized_index"
            ).fetchone()[0] or 0.0
            fresh = conn.execute(
                "SELECT COUNT(*) FROM normalized_index "
                "WHERE cached_at >= ?", (cutoff,)).fetchone()[0]
            fp_rows = conn.execute(
                "SELECT DISTINCT settings_fp FROM normalized_index"
            ).fetchall()
            return {
                'count': int(count),
                'sources': int(sources),
                'newest_ts': float(newest),
                'oldest_ts': float(oldest),
                'fresh_count': int(fresh),
                'settings_fps': [r[0] for r in fp_rows],
            }
        except sqlite3.ProgrammingError:
            return {'count': 0, 'sources': 0, 'newest_ts': 0.0,
                    'oldest_ts': 0.0, 'fresh_count': 0,
                    'settings_fps': []}
        except Exception:
            logger.exception("get_normalized_index_stats")
            return {'count': 0, 'sources': 0, 'newest_ts': 0.0,
                    'oldest_ts': 0.0, 'fresh_count': 0,
                    'settings_fps': []}

    def configure_link_cache(self, max_files: Optional[int] = None,
                             max_mb: Optional[int] = None):
        with self._link_cache_lock:
            if max_files is not None:
                self.link_cache_max_files = max(1, int(max_files))
            if max_mb is not None:
                self.link_cache_max_bytes = (
                    max(1, int(max_mb)) * 1024 * 1024)

    def _link_cache_key(self, url: str) -> str:
        return hashlib.sha256(
            url.encode('utf-8')).hexdigest()[:16]

    def _link_cache_path(self, url: str) -> str:
        return os.path.join(self.link_cache_dir,
                            f"{self._link_cache_key(url)}.json")

    def get_link_cache(self, url: str, max_age_hours: int
                       ) -> Optional[List[Dict[str, Any]]]:
        path = self._link_cache_path(url)
        if not os.path.exists(path):
            return None
        try:
            age = (time.time() - os.path.getmtime(path)) / 3600.0
            if age > max_age_hours:
                return None
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) and data else None
        except Exception:
            return None

    def put_link_cache(self, url: str,
                       channels: List[Dict[str, Any]]):
        if not channels:
            return
        with self._link_cache_lock:
            self._link_cache_put_counter += 1
            # FIX 9: эвикция раз в 10 записей (а не 50), чтобы
            # не превышать лимит при интенсивной записи.
            if self._link_cache_put_counter % 10 == 0:
                self._evict_link_cache_if_needed()
            path = self._link_cache_path(url)
            tmp = path + ".tmp"
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(channels, f, ensure_ascii=False)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception:
                with suppress(OSError):
                    if os.path.exists(tmp):
                        os.remove(tmp)

    def _evict_link_cache_if_needed(self):
        try:
            files = []
            total = 0
            for name in os.listdir(self.link_cache_dir):
                if not name.endswith('.json'):
                    continue
                p = os.path.join(self.link_cache_dir, name)
                try:
                    sz = os.path.getsize(p)
                    mtime = os.path.getmtime(p)
                    files.append((sz, mtime, p))
                    total += sz
                except OSError:
                    continue
            if (total <= self.link_cache_max_bytes
                    and len(files) <= self.link_cache_max_files):
                return
            files.sort(key=lambda x: (x[1], x[2]))
            target_bytes = int(self.link_cache_max_bytes * 0.8)
            target_files = max(1,
                                int(self.link_cache_max_files * 0.8))
            while (total > target_bytes
                   or len(files) > target_files) and files:
                sz, _, p = files.pop(0)
                with suppress(OSError):
                    os.remove(p)
                    total -= sz
        except Exception:
            pass

    def clear_link_cache(self):
        with self._link_cache_lock:
            try:
                shutil.rmtree(self.link_cache_dir,
                              ignore_errors=True)
                os.makedirs(self.link_cache_dir, exist_ok=True)
                self._link_cache_put_counter = 0
            except Exception:
                logger.warning("clear_link_cache failed")

    def get_stats(self, check_ttl_hours: int = 24,
                  epg_ttl_hours: int = EPG_CACHE_TTL_HOURS
                  ) -> Dict[str, Any]:
        stats = {
            'check_results': {'count': 0, 'old_count': 0},
            'epg_entries': {'count': 0, 'old_count': 0},
            'epg_channels': {'count': 0, 'old_count': 0},
            'normalized_index': {'count': 0, 'old_count': 0,
                                 'sources': 0},
            'link_cache': {'files': 0, 'bytes': 0},
            'db_size_bytes': 0,
        }
        try:
            conn = self._connect()
            now = time.time()
            stats['check_results']['count'] = conn.execute(
                "SELECT COUNT(*) FROM url_status_cache").fetchone()[0]
            cutoff = now - check_ttl_hours * 3600
            stats['check_results']['old_count'] = conn.execute(
                "SELECT COUNT(*) FROM url_status_cache "
                "WHERE last_check < ?", (cutoff,)).fetchone()[0]
            stats['epg_entries']['count'] = conn.execute(
                "SELECT COUNT(*) FROM epg_entries").fetchone()[0]
            cutoff_epg = now - epg_ttl_hours * 3600
            stats['epg_entries']['old_count'] = conn.execute(
                "SELECT COUNT(*) FROM epg_entries "
                "WHERE cached_at < ?", (cutoff_epg,)).fetchone()[0]
            stats['epg_channels']['count'] = conn.execute(
                "SELECT COUNT(*) FROM epg_channels").fetchone()[0]
            stats['epg_channels']['old_count'] = conn.execute(
                "SELECT COUNT(*) FROM epg_channels "
                "WHERE cached_at < ?", (cutoff_epg,)).fetchone()[0]
            stats['normalized_index']['count'] = conn.execute(
                "SELECT COUNT(*) FROM normalized_index").fetchone()[0]
            cutoff_norm = now - 24 * 3600
            stats['normalized_index']['old_count'] = conn.execute(
                "SELECT COUNT(*) FROM normalized_index "
                "WHERE cached_at < ?", (cutoff_norm,)).fetchone()[0]
            stats['normalized_index']['sources'] = conn.execute(
                "SELECT COUNT(DISTINCT source) "
                "FROM normalized_index").fetchone()[0]

            with suppress(OSError):
                files = 0
                total = 0
                for name in os.listdir(self.link_cache_dir):
                    if name.endswith('.json'):
                        p = os.path.join(self.link_cache_dir, name)
                        with suppress(OSError):
                            total += os.path.getsize(p)
                            files += 1
                stats['link_cache']['files'] = files
                stats['link_cache']['bytes'] = total

            with suppress(OSError):
                size = os.path.getsize(self.db_path)
                for suffix in ('-wal', '-shm'):
                    p = self.db_path + suffix
                    if os.path.exists(p):
                        size += os.path.getsize(p)
                stats['db_size_bytes'] = size
        except sqlite3.ProgrammingError:
            pass
        except Exception:
            logger.exception("get_stats failed")
        return stats