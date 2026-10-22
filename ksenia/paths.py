# -*- coding: utf-8 -*-
"""Пути и диалоговые хелперы (без логгера)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from constants import YES_NO
from logging_setup import logger


class Paths:
    @staticmethod
    def get_config_dir() -> str:
        if sys.platform.startswith("linux"):
            base = os.environ.get('XDG_CONFIG_HOME',
                                  os.path.expanduser('~/.config'))
            return os.path.join(base, "ksenia")
        appdata = os.environ.get('APPDATA')
        if appdata:
            return os.path.join(appdata, "Ksenia")
        return os.path.expanduser("~/.ksenia")

    @staticmethod
    def get_log_path() -> str:
        return os.path.join(Paths.get_config_dir(), "editor.log")

    @staticmethod
    def get_app_dir() -> str:
        if getattr(sys, 'frozen', False):
            return (getattr(sys, '_MEIPASS', None)
                    or os.path.dirname(sys.executable))
        return os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def get_help_dir() -> str:
        return os.path.join(Paths.get_app_dir(), "help")


def error_box(parent, msg, title: str = "") -> None:
    QMessageBox.critical(parent, title or "Ошибка", str(msg))


def warn_box(parent, msg, title: str = "") -> None:
    QMessageBox.warning(parent, title or "Предупреждение", str(msg))


def info_box(parent, msg, title: str = "") -> None:
    QMessageBox.information(parent, title or "Информация", str(msg))


def confirm(parent, msg, title: str = "") -> bool:
    return QMessageBox.question(
        parent, title or "Подтверждение", msg, YES_NO
    ) == QMessageBox.StandardButton.Yes


def confirm_three(parent, msg, title: str = "") -> str:
    r = QMessageBox.question(
        parent, title or "Подтверждение", msg,
        QMessageBox.StandardButton.Yes |
        QMessageBox.StandardButton.No |
        QMessageBox.StandardButton.Cancel)
    return {QMessageBox.StandardButton.Yes: 'yes',
            QMessageBox.StandardButton.No: 'no',
            QMessageBox.StandardButton.Cancel: 'cancel'}.get(r, 'cancel')


def confirm_then(parent, msg, action, *, title: str = "") -> bool:
    if not confirm(parent, msg, title):
        return False
    action()
    return True


def open_file_dialog(parent, title: str = "",
                     filters: str = "",
                     initial_dir: str = "") -> Optional[str]:
    if not filters:
        from constants import M3U_FILTER
        filters = M3U_FILTER
    fp, _ = QFileDialog.getOpenFileName(
        parent, title or "Открыть", initial_dir, filters)
    return fp or None


def save_file_dialog(parent, title: str = "",
                     default_name: str = "",
                     filters: str = "",
                     default_ext: str = "") -> Optional[str]:
    if not filters:
        from constants import M3U_FILTER
        filters = M3U_FILTER
    fp, _ = QFileDialog.getSaveFileName(
        parent, title or "Сохранить", default_name, filters)
    if not fp:
        return None
    if default_ext and not fp.lower().endswith(default_ext):
        fp += default_ext
    return fp


def open_external(url: str) -> bool:
    if not url:
        return False
    try:
        if url.startswith(('http://', 'https://', 'mailto:')):
            return QDesktopServices.openUrl(QUrl(url))
        return QDesktopServices.openUrl(QUrl.fromLocalFile(url))
    except Exception:
        logger.exception("open_external")
        return False


def parse_datetime(s, fmt: str = "%Y-%m-%d %H:%M:%S"
                   ) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.strptime(s, fmt)
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None