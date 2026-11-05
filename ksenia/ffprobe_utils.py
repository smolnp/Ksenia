# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Утилиты для ffprobe: определение фактического разрешения потока.

Мягкая деградация: если ffprobe отсутствует — функции
возвращают (None, "ffprobe not found") и логируют warning,
но не бросают исключений. Вызывающий код решает, показывать
ли статус «неизвестно».

Путь к ffprobe: сначала явный `ffprobe_path` из конфига
(если задан), затем `shutil.which("ffprobe")`. Это решает
проблему Windows, где ffmpeg не всегда в PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Dict, Optional, Tuple

from constants import (
    FFPROBE_PATH, FFPROBE_TIMEOUT_SEC)
from logging_setup import logger


# Кеш результата поиска ffprobe: ищем один раз на процесс
# (или до явного сброса через reset_ffprobe_cache()).
_find_cache_lock = threading.Lock()
_find_cache: Optional[str] = None
_find_cache_done: bool = False


def _configured_path() -> str:
    """Возвращает явный путь к ffprobe из конфига или ''.

    Читаем конфиг НАПРЯМУЮ (не через try_get_if_gui_thread),
    потому что ffprobe ищется в т.ч. из фоновых потоков
    (ResolutionWorker, SourceUrlCheckWorker).
    """
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
    """Сбрасывает кеш поиска ffprobe (например, после смены
    пути в настройках)."""
    global _find_cache, _find_cache_done
    with _find_cache_lock:
        _find_cache = None
        _find_cache_done = False


def _try_configured(path: str) -> Optional[str]:
    """Проверяет явно указанный путь. Может быть:
      - полный путь к файлу ffprobe(.exe);
      - директория, в которой лежит ffprobe(.exe).
    """
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
    """Ищет ffprobe: явный путь из конфига → PATH.

    Кеширует результат до reset_ffprobe_cache() / force=True.
    """
    global _find_cache, _find_cache_done
    with _find_cache_lock:
        if force:
            _find_cache = None
            _find_cache_done = False
        if _find_cache_done:
            return _find_cache

        # 1. Явный путь из конфига
        cfg_path = _configured_path()
        if cfg_path:
            explicit = _try_configured(cfg_path)
            if explicit:
                _find_cache = explicit
                _find_cache_done = True
                logger.info(
                    "ffprobe (из настроек): %s", explicit)
                return explicit
            logger.warning(
                "ffprobe: указанный путь не найден (%s), "
                "ищу в PATH", cfg_path)

        # 2. PATH
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
    """Нормализует (w,h) в строку SD/HD/FHD/4K.

    Пустая строка, если высота не определена.
    """
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


def _build_ffprobe_cmd(
        ffprobe: str, url: str,
        user_agent: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
) -> list:
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
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


def get_stream_resolution(
        url: str,
        timeout: int = FFPROBE_TIMEOUT_SEC,
        user_agent: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], str]:
    """Возвращает (разрешение, ошибка). Не бросает исключений."""
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
            cmd, capture_output=True, text=True,
            timeout=max(1, int(timeout)), check=False)
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
        s = streams[0]
        w = int(s.get("width") or 0)
        h = int(s.get("height") or 0)
        if not h:
            return None, "no height"
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
    """Числовой ранг для сортировки (больше = лучше)."""
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