# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Алиасы iptv-org (channels.json → {alt_casefold: canon_raw}).

Кэш ~2-3 МБ в config-каталоге. TTL 24 ч. Загрузка ленивая.
Защита от рекурсии через thread-local флаг _building.

Алиасы хранятся в «сыром» виде (casefold, без нормализации).
Нормализация применяется потребителем (name_normalizer) с
учётом пользовательских настроек. Это гарантирует, что
отключение любой опции нормализации (скобки, эмодзи,
спецсимволы) не ломает поиск алиасов.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Dict, Optional

from logging_setup import logger

_URL = "https://iptv-org.github.io/api/channels.json"
_TTL_SEC = 24 * 60 * 60
_UA = "Ksenia-M3U-Editor/0.2"
_TIMEOUT_SEC = 30
_CACHE_FILENAME = "iptv_org_aliases.json"

_lock = threading.RLock()
_cache: Optional[Dict[str, str]] = None
_cache_loaded: bool = False
_cache_meta: Dict[str, object] = {}

_building = threading.local()
_download_in_progress = threading.Event()


def _cache_path() -> str:
    try:
        from paths import Paths
        return os.path.join(Paths.get_config_dir(), _CACHE_FILENAME)
    except Exception:
        return _CACHE_FILENAME


def _read_cache() -> Optional[Dict[str, str]]:
    p = _cache_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.exception("iptv_org_aliases: read cache")
        return None
    if not isinstance(data, dict):
        return None
    aliases = data.get("aliases")
    if not isinstance(aliases, dict):
        return None
    return {str(k): str(v) for k, v in aliases.items() if k and v}


def _cache_age_sec() -> Optional[float]:
    p = _cache_path()
    if not os.path.isfile(p):
        return None
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return None


def _write_cache(aliases: Dict[str, str], source_count: int) -> None:
    p = _cache_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        payload = {
            "version": 3,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_url": _URL,
            "source_count": int(source_count),
            "aliases": aliases,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
        logger.info("iptv_org_aliases: cached %d aliases",
                    len(aliases))
    except Exception:
        logger.exception("iptv_org_aliases: write cache")


def _download_channels() -> Optional[list]:
    try:
        req = urllib.request.Request(
            _URL, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req,
                                     timeout=_TIMEOUT_SEC) as r:
            raw = r.read()
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning("iptv_org_aliases: download failed: %s", e)
        return None


def _normalize_raw(name: str) -> str:
    import re
    if not name:
        return ""
    s = re.sub(r'\s+', ' ', name.strip())
    return s.casefold()


def _build_aliases(channels: list) -> Dict[str, str]:
    _building.value = True
    try:
        aliases: Dict[str, str] = {}
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            name = (ch.get("name") or "").strip()
            if not name:
                continue
            canon_key = _normalize_raw(name)
            if not canon_key:
                continue
            alts = ch.get("alt_names") or []
            if not isinstance(alts, list):
                continue
            for alt in alts:
                if not isinstance(alt, str):
                    continue
                alt_key = _normalize_raw(alt)
                if not alt_key or alt_key == canon_key:
                    continue
                aliases.setdefault(alt_key, name)
        return aliases
    finally:
        _building.value = False


def get_aliases(force_refresh: bool = False) -> Dict[str, str]:
    if getattr(_building, "value", False):
        return {}
    global _cache, _cache_loaded, _cache_meta

    with _lock:
        if _cache_loaded and not force_refresh:
            return dict(_cache or {})

    if _download_in_progress.is_set() and not force_refresh:
        with _lock:
            return dict(_cache or {})

    with _lock:
        if _cache_loaded and not force_refresh:
            return dict(_cache or {})
        if not force_refresh:
            age = _cache_age_sec()
            if age is not None and age < _TTL_SEC:
                cached = _read_cache()
                if cached is not None:
                    _cache = cached
                    _cache_loaded = True
                    _cache_meta = {"from_cache": True,
                                    "age_sec": age,
                                    "count": len(cached)}
                    return dict(cached)

    _download_in_progress.set()
    try:
        channels = _download_channels()
        if channels is None:
            stale = _read_cache()
            if stale is not None:
                with _lock:
                    _cache = stale
                    _cache_loaded = True
                    _cache_meta = {"from_cache": True,
                                    "age_sec": _cache_age_sec(),
                                    "count": len(stale),
                                    "stale": True}
                return dict(stale)
            with _lock:
                _cache = {}
                _cache_loaded = True
                _cache_meta = {"count": 0, "error": True}
            return {}

        aliases = _build_aliases(channels)
        if not aliases:
            with _lock:
                _cache = {}
                _cache_loaded = True
                _cache_meta = {"count": 0, "error": True}
            return {}

        _write_cache(aliases, len(channels))
        with _lock:
            _cache = aliases
            _cache_loaded = True
            _cache_meta = {"from_cache": False,
                            "count": len(aliases),
                            "source_count": len(channels)}
        return dict(aliases)
    finally:
        _download_in_progress.clear()


def get_aliases_info() -> Dict[str, object]:
    with _lock:
        info = dict(_cache_meta)
    info["cache_path"] = _cache_path()
    info["cache_exists"] = os.path.isfile(_cache_path())
    info["cache_age_sec"] = _cache_age_sec()
    info["cache_ttl_sec"] = _TTL_SEC
    info["download_in_progress"] = _download_in_progress.is_set()
    return info


def clear_cache() -> None:
    global _cache, _cache_loaded, _cache_meta
    with _lock:
        _cache = None
        _cache_loaded = False
        _cache_meta = {}
        p = _cache_path()
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            logger.exception("iptv_org_aliases: clear_cache")