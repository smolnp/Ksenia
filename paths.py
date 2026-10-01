# -*- coding: utf-8 -*-
"""Paths, _setup_logging, logger, диалоговые хелперы."""

from __future__ import annotations
import os
import sys
import logging
import threading
import webbrowser
from contextlib import suppress
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional, Callable
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFileDialog, QMessageBox
from constants import M3U_FILTER, JSON_FILTER, YES_NO

class Paths:
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

_logging_initialized = False
_logging_lock = threading.Lock()

def _setup_logging():
    global _logging_initialized
    logger_ = logging.getLogger(__name__)
    with _logging_lock:
        if _logging_initialized:
            return logger_
        logger_.setLevel(logging.INFO)
        fmt = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger_.addHandler(sh)
        try:
            config_dir = Paths.get_config_dir()
            os.makedirs(config_dir, exist_ok=True)
            log_path = Paths.get_log_path()
            fh = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding='utf-8')
            fh.setFormatter(fmt)
            logger_.addHandler(fh)
        except Exception:
            pass
        _logging_initialized = True
    return logger_

logger = _setup_logging()

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
    if url:
        webbrowser.open(url)

def open_in_os(path_or_url: str):
    if not path_or_url:
        return
    if path_or_url.startswith(('http://', 'https://')):
        webbrowser.open(path_or_url)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path_or_url))

def parse_datetime(s, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, str):
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    else:
        return None
    for f in (fmt, None):
        try:
            return datetime.strptime(s, f) if f else datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
    return None
