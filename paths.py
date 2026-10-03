# -*- coding: utf-8 -*-
"""Пути, логирование, диалоговые хелперы."""

from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from typing import Optional
import queue as _queue
import atexit as _atexit

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from constants import M3U_FILTER, YES_NO


class Paths:
    """Все пути приложения."""

    @staticmethod
    def get_config_dir() -> str:
        if sys.platform.startswith("linux"):
            config_home = os.environ.get(
                'XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
            return os.path.join(config_home, "ksenia")
        if sys.platform == "darwin":
            return os.path.expanduser("~/Library/Application Support/Ksenia")
        appdata = os.environ.get('APPDATA')
        if appdata:
            return os.path.join(appdata, "Ksenia")
        return os.path.expanduser("~/.ksenia")

    @staticmethod
    def get_log_path() -> str:
        return os.path.join(Paths.get_config_dir(), "editor.log")

    @staticmethod
    def get_app_dir() -> str:
        """Корень проекта — папка, где лежит paths.py.

        Для PyInstaller — временная распаковка (_MEIPASS) или папка .exe.
        """
        if getattr(sys, 'frozen', False):
            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                return meipass
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def get_help_dir() -> str:
        """Папка help/ со встроенной справкой."""
        return os.path.join(Paths.get_app_dir(), "help")

    @staticmethod
    def get_help_index() -> str:
        """Полный путь к help/index.html."""
        return os.path.join(Paths.get_help_dir(), "index.html")


# =====================================================================
# Логирование
# =====================================================================
_logging_initialized = False
_logging_lock = threading.Lock()


def _setup_logging() -> logging.Logger:
    global _logging_initialized
    log = logging.getLogger(__name__)
    with _logging_lock:
        if _logging_initialized:
            return log
        log.setLevel(logging.INFO)
        fmt = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)

        try:
            config_dir = Paths.get_config_dir()
            os.makedirs(config_dir, exist_ok=True)
            log_path = Paths.get_log_path()
            fh = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding='utf-8')
            fh.setFormatter(fmt)

            log_queue: _queue.Queue = _queue.Queue(-1)
            qh = QueueHandler(log_queue)
            qh.setFormatter(fmt)
            log.addHandler(qh)

            listener = QueueListener(log_queue, fh,
                                     respect_handler_level=True)
            listener.start()
            _atexit.register(listener.stop)
        except Exception:
            pass

        _logging_initialized = True
    return log


logger = _setup_logging()


# =====================================================================
# Диалоговые хелперы
# =====================================================================
def error_box(parent, msg, title: str = "Ошибка"):
    QMessageBox.critical(parent, title, str(msg))


def warn_box(parent, msg, title: str = "Предупреждение"):
    QMessageBox.warning(parent, title, str(msg))


def info_box(parent, msg, title: str = "Информация"):
    QMessageBox.information(parent, title, str(msg))


def confirm(parent, msg, title: str = "Подтверждение") -> bool:
    return QMessageBox.question(parent, title, msg, YES_NO) \
        == QMessageBox.StandardButton.Yes


def confirm_three(parent, msg, title: str = "Подтверждение") -> str:
    r = QMessageBox.question(
        parent, title, msg,
        QMessageBox.StandardButton.Yes |
        QMessageBox.StandardButton.No |
        QMessageBox.StandardButton.Cancel)
    return {QMessageBox.StandardButton.Yes: 'yes',
            QMessageBox.StandardButton.No: 'no',
            QMessageBox.StandardButton.Cancel: 'cancel'}.get(r, 'cancel')


def open_file_dialog(parent, title: str = "Открыть",
                     filters: str = M3U_FILTER,
                     initial_dir: str = "") -> Optional[str]:
    fp, _ = QFileDialog.getOpenFileName(parent, title, initial_dir, filters)
    return fp or None


def save_file_dialog(parent, title: str = "Сохранить",
                     default_name: str = "",
                     filters: str = M3U_FILTER,
                     default_ext: str = "") -> Optional[str]:
    fp, _ = QFileDialog.getSaveFileName(parent, title, default_name, filters)
    if not fp:
        return None
    if default_ext and not fp.lower().endswith(default_ext):
        fp += default_ext
    return fp


def open_dir_dialog(parent, title: str = "Выбрать папку",
                    initial_dir: str = "") -> Optional[str]:
    d = QFileDialog.getExistingDirectory(parent, title, initial_dir)
    return d or None


def open_external(url: str):
    """Открыть URL/путь во внешнем приложении."""
    if not url:
        return
    if url.startswith(('http://', 'https://', 'mailto:')):
        webbrowser.open(url)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(url))


# Алиас для совместимости
open_in_os = open_external


def parse_datetime(s, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    for f in (fmt, None):
        try:
            return datetime.strptime(s, f) if f else datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
    return None