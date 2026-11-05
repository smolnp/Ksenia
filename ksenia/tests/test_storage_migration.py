"""Тесты миграции схемы url_status_cache."""
import os
import sqlite3
import tempfile

import pytest


def _make_old_db(path: str):
    """Создаёт БД со старой схемой (без ttfb_ms/resolution)."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE url_status_cache (
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
        """)
        conn.execute(
            "INSERT INTO url_status_cache "
            "(name, url, alive, last_check) "
            "VALUES ('test', 'http://x/y', 1, 12345.0)")
        conn.commit()
    finally:
        conn.close()


def test_migration_adds_columns(tmp_path, monkeypatch):
    """При открытии старой БД CacheManager добавляет колонки."""
    # Создаём старую БД по пути, который ожидает CacheManager.
    from paths import Paths
    monkeypatch.setattr(
        Paths, 'get_config_dir',
        staticmethod(lambda: str(tmp_path)))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    db_path = cache_dir / "editor_cache.db"
    _make_old_db(str(db_path))

    # Проверяем, что колонок нет ДО.
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in
            conn.execute("PRAGMA table_info(url_status_cache)")}
    conn.close()
    assert 'ttfb_ms' not in cols
    assert 'resolution' not in cols

    # Инициализируем CacheManager — он должен мигрировать.
    from storage import CacheManager
    cm = CacheManager()

    # Проверяем, что колонки появились.
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in
            conn.execute("PRAGMA table_info(url_status_cache)")}
    conn.close()
    assert 'ttfb_ms' in cols
    assert 'resolution' in cols

    # Старая запись не потерялась.
    hit = cm.get_check_result("test", "http://x/y",
                                max_age_hours=99999)
    assert hit is not None
    assert hit['alive'] is True


def test_migration_idempotent(tmp_path, monkeypatch):
    """Повторный CacheManager на мигрированной БД не падает."""
    from paths import Paths
    monkeypatch.setattr(
        Paths, 'get_config_dir',
        staticmethod(lambda: str(tmp_path)))
    from storage import CacheManager
    cm1 = CacheManager()
    cm1.close_thread_connection()
    cm1.reset_all_connections()
    # Второй экземпляр не должен повторно ALTER-ить.
    cm2 = CacheManager()
    assert cm2 is not None