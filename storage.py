# -*- coding: utf-8 -*-
"""BaseJsonStore, StableStateManager, CacheManager."""

from __future__ import annotations
import os
import json
import time
import sqlite3
import shutil
import atexit
import hashlib
import threading
from contextlib import suppress
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict
from constants import (STABLE_STATE_FILE, CACHE_SCHEMA_VERSION,
    CHECK_RESULT_CACHE_TTL_HOURS, EPG_CACHE_TTL_HOURS)
from paths import Paths, logger

class BaseJsonStore:
    def __init__(self, path: str, default: Any):
        self.path = path
        self._lock = threading.RLock()
        self._data = default
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, type(self._data)):
                self._data = data
            elif isinstance(self._data, list) and isinstance(data, dict):
                self._data = list(data.values()) if data else []
            elif isinstance(self._data, dict) and isinstance(data, list):
                self._data = {}
        except Exception as e:
            logger.exception(f"Load {self.path}: {e}")

    def save(self) -> bool:
        with self._lock:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except Exception as e:
                logger.exception(f"Save {self.path}: {e}")
                return False

class StableStateManager:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, STABLE_STATE_FILE)
        self._lock = threading.RLock()
        self._data: Dict[str, str] = {}
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
            try:
                tmp = self.path + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except Exception:
                logger.exception("StableState save")
                return False

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, url: str):
        with self._lock:
            self._data[key] = url

class CacheManager:
    _all_connections: List[Any] = []
    _all_connections_lock = threading.RLock()
    _atexit_registered = False
    _atexit_lock = threading.Lock()

    def __init__(self, cache_dir: str = "cache"):
        config_dir = Paths.get_config_dir()
        self.cache_dir = os.path.join(config_dir, cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.db_path = os.path.join(self.cache_dir, "editor_cache.db")

        self.link_cache_dir = os.path.join(self.cache_dir, "link_cache")
        os.makedirs(self.link_cache_dir, exist_ok=True)
        self._link_cache_lock = threading.RLock()
        self._link_cache_put_counter = 0
        self.link_cache_max_bytes = 64 * 1024 * 1024
        self.link_cache_max_files = 10000

        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._register_atexit()
        try:
            self._init_db()
        except Exception:
            conn = getattr(self._local, 'conn', None)
            if conn is not None:
                with suppress(Exception):
                    conn.close()
                self._local.conn = None
            raise

    @classmethod
    def _register_atexit(cls):
        with cls._atexit_lock:
            if cls._atexit_registered:
                return

            def _close_all():
                with cls._all_connections_lock:
                    conns = list(cls._all_connections)
                    cls._all_connections.clear()
                for conn in conns:
                    with suppress(Exception):
                        conn.close()
            atexit.register(_close_all)
            cls._atexit_registered = True

    @classmethod
    def _gc_connections(cls):
        with cls._all_connections_lock:
            cls._all_connections = [
                c for c in cls._all_connections
                if not cls._is_connection_dead(c)
            ]

    @staticmethod
    def _is_connection_dead(c) -> bool:
        try:
            _ = c.in_transaction
            return False
        except Exception:
            return True

    def _connect(self) -> sqlite3.Connection:
        with self._connect_lock:
            conn = getattr(self._local, 'conn', None)
            if conn is None:
                conn = sqlite3.connect(
                    self.db_path, timeout=30, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.row_factory = sqlite3.Row
                self._local.conn = conn
                with self._all_connections_lock:
                    self._all_connections.append(conn)
                    if len(self._all_connections) % 16 == 0:
                        self._gc_connections()
            return conn

    def _init_db(self):
        with self._init_lock:
            conn = self._connect()
            conn.execute("""
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
                )
            """)
            with suppress(sqlite3.OperationalError):
                cols = {row[1] for row in conn.execute(
                    "PRAGMA table_info(epg_entries)").fetchall()}
                if 'cached_at' not in cols:
                    conn.execute(
                        "ALTER TABLE epg_entries ADD COLUMN cached_at REAL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_epg_channel ON epg_entries(channel_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_epg_time ON epg_entries(start, stop)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_epg_cached ON epg_entries(cached_at)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS epg_channels (
                    channel_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    icon TEXT,
                    lcn TEXT,
                    source TEXT,
                    cached_at REAL DEFAULT 0
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_epg_channels_cached "
                "ON epg_channels(cached_at)")

            conn.execute("""
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
                )
            """)
            with suppress(sqlite3.OperationalError):
                cols = {row[1] for row in conn.execute(
                    "PRAGMA table_info(url_status_cache)").fetchall()}
                if 'successes' not in cols:
                    conn.execute(
                        "ALTER TABLE url_status_cache "
                        "ADD COLUMN successes INTEGER DEFAULT 0")
                if 'failures' not in cols:
                    conn.execute(
                        "ALTER TABLE url_status_cache "
                        "ADD COLUMN failures INTEGER DEFAULT 0")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)
            row = conn.execute(
                "SELECT version FROM schema_version LIMIT 1").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (CACHE_SCHEMA_VERSION,))
            else:
                with suppress(Exception):
                    conn.execute(
                        "UPDATE schema_version SET version=?",
                        (CACHE_SCHEMA_VERSION,))
            conn.commit()

    def save_check_result(self, name: str, url: str, alive: bool,
                          response_ms: float, status_text: str = "",
                          status_code: Optional[int] = None):
        if not name or not url:
            return
        try:
            conn = self._connect()
            with conn:
                if alive:
                    conn.execute("""
                        INSERT INTO url_status_cache
                            (name, url, alive, response_ms, last_check,
                             status_text, status_code,
                             successes, failures)
                        VALUES (?, ?, 1, ?, ?, ?, ?, 1, 0)
                        ON CONFLICT(name, url) DO UPDATE SET
                            alive = excluded.alive,
                            response_ms = excluded.response_ms,
                            last_check = excluded.last_check,
                            status_text = excluded.status_text,
                            status_code = excluded.status_code,
                            successes = url_status_cache.successes + 1
                    """, (name.lower(), url, response_ms, time.time(),
                          status_text,
                          status_code if status_code is not None else -1))
                else:
                    conn.execute("""
                        INSERT INTO url_status_cache
                            (name, url, alive, response_ms, last_check,
                             status_text, status_code,
                             successes, failures)
                        VALUES (?, ?, 0, ?, ?, ?, ?, 0, 1)
                        ON CONFLICT(name, url) DO UPDATE SET
                            alive = excluded.alive,
                            response_ms = excluded.response_ms,
                            last_check = excluded.last_check,
                            status_text = excluded.status_text,
                            status_code = excluded.status_code,
                            failures = url_status_cache.failures + 1
                    """, (name.lower(), url, response_ms, time.time(),
                          status_text,
                          status_code if status_code is not None else -1))
        except Exception as e:
            logger.debug(f"save_check_result: {e}")

    def get_check_result(self, name: str, url: str, max_age_hours: int = 24
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
            if row:
                return {
                    'alive': bool(row['alive']),
                    'response_ms': float(row['response_ms'] or 0),
                    'last_check': float(row['last_check'] or 0),
                    'status_text': row['status_text'] or '',
                    'status_code': (row['status_code']
                                    if row['status_code'] is not None
                                    and row['status_code'] >= 0 else None),
                    'successes': int(row['successes'] or 0),
                    'failures': int(row['failures'] or 0),
                }
        except Exception as e:
            logger.debug(f"get_check_result: {e}")
        return None

    def get_check_results_batch(
        self,
        pairs: List[Tuple[str, str]],
        max_age_hours: int = CHECK_RESULT_CACHE_TTL_HOURS,
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if not pairs:
            return {}
        cutoff = time.time() - max_age_hours * 3600
        result: Dict[Tuple[str, str], Dict[str, Any]] = {}
        by_name: Dict[str, Set[str]] = defaultdict(set)
        for name, url in pairs:
            if name and url:
                by_name[name.lower()].add(url)
        if not by_name:
            return {}
        unique_pairs = list({
            (n.lower(), u) for n, u in pairs if n and u
        })
        if not unique_pairs:
            return {}
        CHUNK = 400  # SQLite лимит ~999 параметров
        try:
            conn = self._connect()
            for i in range(0, len(unique_pairs), CHUNK):
                chunk = unique_pairs[i:i + CHUNK]
                placeholders = ",".join(["(?,?)"] * len(chunk))
                flat = [x for pair in chunk for x in pair]
                flat.append(cutoff)
                rows = conn.execute(f"""
                    SELECT name, url, alive, response_ms, last_check,
                           status_text, status_code, successes, failures
                    FROM url_status_cache
                    WHERE (name, url) IN ({placeholders})
                      AND last_check >= ?
                """, flat).fetchall()
                for row in rows:
                    key = (row['name'], row['url'])
                    result[key] = {
                        'alive': bool(row['alive']),
                        'response_ms': float(row['response_ms'] or 0),
                        'last_check': float(row['last_check'] or 0),
                        'status_text': row['status_text'] or '',
                        'status_code': (row['status_code']
                                        if row['status_code'] is not None
                                        and row['status_code'] >= 0 else None),
                        'successes': int(row['successes'] or 0),
                        'failures': int(row['failures'] or 0),
                    }
        except Exception as e:
            logger.debug(f"get_check_results_batch: {e}")
        return result

    def save_check_results_batch(
        self,
        results: List[Tuple[str, str, bool, float, str, Optional[int]]],
    ) -> None:
        if not results:
            return
        now = time.time()
        rows = []
        for name, url, alive, response_ms, status_text, status_code in results:
            if not name or not url:
                continue
            rows.append((
                name.lower(), url, 1 if alive else 0, response_ms, now,
                status_text or "",
                status_code if status_code is not None else -1,
                1 if alive else 0,
                0 if alive else 1,
            ))
        if not rows:
            return
        try:
            conn = self._connect()
            with conn:
                conn.executemany("""
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
                        successes = url_status_cache.successes + excluded.successes,
                        failures = url_status_cache.failures + excluded.failures
                """, rows)
        except Exception as e:
            logger.debug(f"save_check_results_batch: {e}")

    def cleanup_old(self, days: int = 30) -> int:
        cutoff = time.time() - days * 86400
        try:
            conn = self._connect()
            with conn:
                cur = conn.execute(
                    "DELETE FROM url_status_cache WHERE last_check < ?", (cutoff,))
                n = cur.rowcount
                cur3 = conn.execute(
                    "DELETE FROM epg_entries WHERE cached_at > 0 AND cached_at < ?",
                    (cutoff,))
                n += cur3.rowcount
                cur4 = conn.execute(
                    "DELETE FROM epg_channels WHERE cached_at > 0 AND cached_at < ?",
                    (cutoff,))
                n += cur4.rowcount
                return n
        except Exception:
            logger.exception("cache cleanup")
            return 0

    def clear(self):
        try:
            conn = self._connect()
            with conn:
                conn.execute("DELETE FROM url_status_cache")
        except Exception:
            logger.exception("cache clear")

    def vacuum(self):
        try:
            conn = self._connect()
            conn.execute("VACUUM")
        except Exception as e:
            logger.debug(f"cache vacuum: {e}")

    def close_thread_connection(self):
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            with suppress(Exception):
                conn.close()
            self._local.conn = None
            with self._all_connections_lock:
                if conn in self._all_connections:
                    self._all_connections.remove(conn)

    def save_epg_entries(self, entries: List[Dict[str, Any]], source: str = ""):
        if not entries:
            return
        try:
            now = time.time()
            conn = self._connect()
            with conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO epg_entries
                        (channel_id, start, stop, title, description, category,
                         source, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (e.get('channel_id', ''), e.get('start', ''),
                     e.get('stop', ''), e.get('title', ''),
                     e.get('desc', ''), e.get('category', ''), source, now)
                    for e in entries
                ])
        except Exception as e:
            logger.debug(f"save_epg_entries: {e}")

    def load_epg_entries(self, max_age_hours: int = EPG_CACHE_TTL_HOURS
                         ) -> List[Dict[str, Any]]:
        try:
            cutoff = time.time() - max_age_hours * 3600
            conn = self._connect()
            rows = conn.execute("""
                SELECT channel_id, start, stop, title, description, category, source
                FROM epg_entries
                WHERE start IS NOT NULL AND start != ''
                  AND (cached_at = 0 OR cached_at >= ?)
            """, (cutoff,)).fetchall()
            return [{
                'channel_id': r['channel_id'],
                'start': r['start'],
                'stop': r['stop'],
                'title': r['title'],
                'desc': r['description'],
                'category': r['category'],
                'source': r['source'],
            } for r in rows]
        except Exception as e:
            logger.debug(f"load_epg_entries: {e}")
            return []

    def save_epg_channels(self, entries: List[Dict[str, Any]], source: str = ""):
        if not entries:
            return
        try:
            now = time.time()
            conn = self._connect()
            with conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO epg_channels
                        (channel_id, display_name, icon, lcn, source, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, [
                    (e.get('channel_id', ''), e.get('display_name', ''),
                     e.get('icon', ''), e.get('lcn', ''), source, now)
                    for e in entries
                ])
        except Exception as e:
            logger.debug(f"save_epg_channels: {e}")

    def load_epg_channels(self, max_age_hours: int = EPG_CACHE_TTL_HOURS
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
                'icon': r['icon'],
                'lcn': r['lcn'],
                'source': r['source'],
            } for r in rows]
        except Exception as e:
            logger.debug(f"load_epg_channels: {e}")
            return []

    def clear_epg(self):
        try:
            conn = self._connect()
            with conn:
                conn.execute("DELETE FROM epg_entries")
                conn.execute("DELETE FROM epg_channels")
        except Exception as e:
            logger.debug(f"clear_epg: {e}")

    def configure_link_cache(self, max_files: Optional[int] = None,
                             max_mb: Optional[int] = None):
        with self._link_cache_lock:
            if max_files is not None:
                self.link_cache_max_files = max(1, int(max_files))
            if max_mb is not None:
                self.link_cache_max_bytes = max(1, int(max_mb)) * 1024 * 1024

    def _link_cache_key(self, url: str) -> str:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]

    def _link_cache_path(self, url: str) -> str:
        return os.path.join(self.link_cache_dir, f"{self._link_cache_key(url)}.json")

    def get_link_cache(self, url: str, max_age_hours: int) -> Optional[List[Dict[str, Any]]]:
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
        except Exception as e:
            logger.debug(f"get_link_cache: {e}")
        return None

    def put_link_cache(self, url: str, channels: List[Dict[str, Any]]):
        if not channels:
            return
        with self._link_cache_lock:
            self._link_cache_put_counter += 1
            if self._link_cache_put_counter % 50 == 0:
                self._evict_link_cache_if_needed()
            path = self._link_cache_path(url)
            try:
                tmp = path + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(channels, f, ensure_ascii=False)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception as e:
                logger.debug(f"put_link_cache: {e}")

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
            if total <= self.link_cache_max_bytes and len(files) <= self.link_cache_max_files:
                return
            files.sort(key=lambda x: (x[1], x[2]))
            target_bytes = self.link_cache_max_bytes * 0.8
            target_files = int(self.link_cache_max_files * 0.8)
            while (total > target_bytes or len(files) > target_files) and files:
                sz, _, p = files.pop(0)
                with suppress(OSError):
                    os.remove(p)
                    total -= sz
        except Exception as e:
            logger.debug(f"link cache evict: {e}")

    def clear_link_cache(self):
        with self._link_cache_lock:
            try:
                shutil.rmtree(self.link_cache_dir, ignore_errors=True)
                os.makedirs(self.link_cache_dir, exist_ok=True)
                self._link_cache_put_counter = 0
            except Exception:
                logger.exception("clear_link_cache")

    def get_stats(self, check_ttl_hours: int = 24,
                  epg_ttl_hours: int = EPG_CACHE_TTL_HOURS) -> Dict[str, Any]:
        stats = {
            'check_results': {'count': 0, 'old_count': 0},
            'epg_entries': {'count': 0, 'old_count': 0},
            'epg_channels': {'count': 0, 'old_count': 0},
            'link_cache': {'files': 0, 'bytes': 0},
            'db_size_bytes': 0,
        }
        try:
            conn = self._connect()
            now = time.time()

            row = conn.execute(
                "SELECT COUNT(*) FROM url_status_cache").fetchone()
            stats['check_results']['count'] = row[0] if row else 0
            cutoff = now - check_ttl_hours * 3600
            row = conn.execute(
                "SELECT COUNT(*) FROM url_status_cache WHERE last_check < ?",
                (cutoff,)).fetchone()
            stats['check_results']['old_count'] = row[0] if row else 0

            row = conn.execute("SELECT COUNT(*) FROM epg_entries").fetchone()
            stats['epg_entries']['count'] = row[0] if row else 0
            cutoff_epg = now - epg_ttl_hours * 3600
            row = conn.execute(
                "SELECT COUNT(*) FROM epg_entries "
                "WHERE cached_at > 0 AND cached_at < ?",
                (cutoff_epg,)).fetchone()
            stats['epg_entries']['old_count'] = row[0] if row else 0

            row = conn.execute("SELECT COUNT(*) FROM epg_channels").fetchone()
            stats['epg_channels']['count'] = row[0] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) FROM epg_channels "
                "WHERE cached_at > 0 AND cached_at < ?",
                (cutoff_epg,)).fetchone()
            stats['epg_channels']['old_count'] = row[0] if row else 0

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
        except Exception:
            logger.exception("get_stats")
        return stats
