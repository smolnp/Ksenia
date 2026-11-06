# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Утилиты для ffprobe: определение фактического разрешения потока.

Мягкая деградация: если ffprobe отсутствует — функции
возвращают (None, "ffprobe not found") и логируют warning,
но не бросают исключений.

Безопасность:
  - protocol_whitelist: http,https,tcp,tls,crypto (без file)
  - env: только безопасные переменные, без *_PROXY
  - shell=False явно

Проблема 240p vs 1080p:
  HLS-мастер-плейлист может содержать несколько вариантов
  качества. Раньше мы брали v:0 (первый), который часто
  оказывается самым низким. Теперь читаем ВСЕ видеопотоки
  и выбираем с максимальным разрешением.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from typing import Dict, List, Optional, Tuple

from constants import (
    FFPROBE_PATH, FFPROBE_TIMEOUT_SEC)
from logging_setup import logger


# Кеш результата поиска ffprobe.
_find_cache_lock = threading.Lock()
_find_cache: Optional[str] = None
_find_cache_done: bool = False

# Протоколы, которые разрешены ffprobe.
# НЕ включаем file, concat, subfile, rtmp, rtsp, udp —
# это векторы атаки (чтение локальных файлов, SSRF).
_ALLOWED_PROTOCOLS = "http,https,tcp,tls,crypto"


def _configured_path() -> str:
    try:
        from app_core import ApplicationCore
        core = ApplicationCore._instance
        if core is not None:
            p = core.config.get('ffprobe_path', '')
            return (p or '').strip()
    except Exception:
        pass
    return ""


def reset_ffprobe_cache() -> None:
    global _find_cache, _find_cache_done
    with _find_cache_lock:
        _find_cache = None
        _find_cache_done = False


def _try_configured(path: str) -> Optional[str]:
    if not path:
        return None
    p = os.path.expanduser(path)
    if os.path.isdir(p):
        for name in ('ffprobe.exe', 'ffprobe'):
            cand = os.path.join(p, name)
            if os.path.isfile(cand):
                return cand
        return None
    if os.path.isfile(p):
        return p
    return None


def find_ffprobe(force: bool = False) -> Optional[str]:
    global _find_cache, _find_cache_done
    with _find_cache_lock:
        if force:
            _find_cache = None
            _find_cache_done = False
        if _find_cache_done:
            return _find_cache
        cfg_path = _configured_path()
        if cfg_path:
            explicit = _try_configured(cfg_path)
            if explicit:
                _find_cache = explicit
                _find_cache_done = True
                logger.info("ffprobe (из настроек): %s", explicit)
                return explicit
            logger.warning(
                "ffprobe: указанный путь не найден (%s), "
                "ищу в PATH", cfg_path)
        path = shutil.which(FFPROBE_PATH)
        _find_cache = path
        _find_cache_done = True
        if path is None:
            logger.warning(
                "ffprobe не найден в PATH — "
                "определение разрешения будет недоступно.")
        else:
            logger.info("ffprobe найден в PATH: %s", path)
        return path


def is_ffprobe_available() -> bool:
    return find_ffprobe() is not None


def normalize_resolution(width: int, height: int) -> str:
    if not height:
        return ""
    if height >= 2160:
        return "4K"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 576:
        return "576p"
    if height >= 480:
        return "480p"
    return f"{height}p"


def _safe_env() -> Dict[str, str]:
    """Безопасное окружение для ffprobe.

    Не включает HTTP_PROXY/HTTPS_PROXY (иначе ffprobe пойдёт
    через прокси, что может сломать проверку), а также
    LD_PRELOAD/DYLD_INSERT_LIBRARIES (защита от инъекций).
    """
    if sys.platform.startswith("win"):
        keys = ('PATH', 'PATHEXT', 'SystemRoot', 'TEMP', 'TMP',
                'USERPROFILE', 'COMSPEC')
    else:
        keys = ('PATH', 'HOME', 'TMPDIR')
    env: Dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v:
            env[k] = v
    env.setdefault('LANG', 'C.UTF-8')
    env.setdefault('LC_ALL', 'C.UTF-8')
    return env


def _build_ffprobe_cmd(
        ffprobe: str, url: str,
        user_agent: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
) -> list:
    """Строит команду ffprobe.

    ВАЖНО: НЕ используем -select_streams v:0, потому что
    для HLS-мастер-плейлиста это даёт первый вариант,
    который часто оказывается самым низким (240p).
    Читаем ВСЕ потоки и выбираем максимальное разрешение
    в get_stream_resolution().
    """
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries",
        "stream=index,codec_type,width,height",
        "-of", "json",
        # P0.6: запрет file://, concat://, subfile://
        "-protocol_whitelist", _ALLOWED_PROTOCOLS,
        # Для HLS и медленных потоков
        "-analyzeduration", "5000000",
        "-probesize", "10000000",
        "-rw_timeout", "5000000",
    ]
    if user_agent:
        cmd.extend(["-user_agent", user_agent])
    if extra_headers:
        hdrs = "\r\n".join(
            f"{k}: {v}" for k, v in extra_headers.items()
            if k.lower() != "user-agent" and v)
        if hdrs:
            cmd.extend(["-headers", hdrs + "\r\n"])
    cmd.append(url)
    return cmd


def _pick_best_stream(streams: List[dict]) -> Tuple[int, int]:
    """Выбирает видеопоток с максимальным разрешением.

    Возвращает (width, height) или (0, 0), если видеопотоков
    нет.

    P0.6-fix: для HLS-мастер-плейлиста с несколькими
    вариантами качества это даёт 1080p, а не 240p (первый).
    """
    best_w = 0
    best_h = 0
    for s in streams:
        if not isinstance(s, dict):
            continue
        codec_type = (s.get("codec_type") or "").lower()
        if codec_type != "video":
            continue
        try:
            w = int(s.get("width") or 0)
            h = int(s.get("height") or 0)
        except (ValueError, TypeError):
            continue
        if h > best_h:
            best_h = h
            best_w = w
    return best_w, best_h


def get_stream_resolution(
        url: str,
        timeout: int = FFPROBE_TIMEOUT_SEC,
        user_agent: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], str]:
    """Возвращает (разрешение, ошибка). Не бросает исключений.

    Читает ВСЕ видеопотоки и выбирает максимальное
    разрешение — это решает проблему 240p vs 1080p для
    HLS-мастер-плейлистов.
    """
    if not url or not url.strip():
        return None, "empty url"
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None, "ffprobe not found"

    cmd = _build_ffprobe_cmd(
        ffprobe, url,
        user_agent=user_agent,
        extra_headers=extra_headers,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
            shell=False,  # P0.6: явно
            env=_safe_env(),
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip()[:200]
            logger.debug(
                "ffprobe failed (rc=%d) для %s: %s",
                result.returncode, url[:80], err)
            return None, err or f"rc={result.returncode}"
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.debug("ffprobe invalid JSON для %s", url[:80])
            return None, "invalid json"
        streams = data.get("streams") or []
        if not streams:
            return None, "no video stream"
        w, h = _pick_best_stream(streams)
        if not h:
            return None, "no height"
        logger.debug(
            "ffprobe: %s → %dx%d (из %d потоков)",
            url[:80], w, h, len(streams))
        return normalize_resolution(w, h), ""
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe timeout для %s", url[:80])
        return None, "timeout"
    except FileNotFoundError:
        logger.warning("ffprobe исчез между поиском и запуском")
        return None, "ffprobe not found"
    except Exception as e:
        logger.exception("ffprobe error для %s", url[:80])
        return None, str(e)[:200]


def resolution_rank(res: str) -> int:
    low = (res or "").lower()
    if "4k" in low or "2160" in low:
        return 4
    if "1080" in low:
        return 3
    if "720" in low:
        return 2
    if "576" in low or "480" in low:
        return 1
    return 0