# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""logging_setup — логгер без зависимостей от проекта.

Единственный источник логгера. Никаких импортов из paths/i18n/
constants/models — чтобы не-GUI модули не тянули PyQt6.QtWidgets.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue as _queue
import sys
import threading
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from typing import Optional

_LOG_INIT_LOCK = threading.RLock()
_INITIALIZED = False
_LISTENER: Optional[QueueListener] = None

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'


def _log_dir() -> str:
    if sys.platform.startswith("linux"):
        base = os.environ.get('XDG_CONFIG_HOME',
                              os.path.expanduser('~/.config'))
        return os.path.join(base, "ksenia")
    appdata = os.environ.get('APPDATA')
    if appdata:
        return os.path.join(appdata, "Ksenia")
    return os.path.expanduser("~/.ksenia")


def get_logger(name: str = "ksenia") -> logging.Logger:
    global _INITIALIZED, _LISTENER
    log = logging.getLogger(name)
    with _LOG_INIT_LOCK:
        if _INITIALIZED or log.handlers:
            return log
        log.setLevel(logging.INFO)
        fmt = logging.Formatter(LOG_FORMAT)

        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        log.addHandler(stream)

        try:
            d = _log_dir()
            os.makedirs(d, exist_ok=True)
            fh = RotatingFileHandler(
                os.path.join(d, "editor.log"),
                maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                encoding='utf-8')
            fh.setFormatter(fmt)

            q: _queue.Queue = _queue.Queue(-1)
            qh = QueueHandler(q)
            qh.setFormatter(fmt)
            log.addHandler(qh)

            _LISTENER = QueueListener(q, fh,
                                       respect_handler_level=True)
            _LISTENER.start()
            atexit.register(_LISTENER.stop)
        except Exception as e:
            log.warning("logging_setup: file handler failed: %s", e)

        _INITIALIZED = True
    return log


logger = get_logger("ksenia")


def swallow(context: str, exc: Optional[BaseException] = None,
            level: int = logging.WARNING) -> None:
    """Проглотить исключение, но залогировать."""
    if exc is not None:
        logger.log(level, "%s: %s", context, exc, exc_info=True)
    else:
        logger.log(level, "%s (no exception attached)", context)