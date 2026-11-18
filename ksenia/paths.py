# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Пути, диалоги подтверждения, безопасная запись файлов."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QMessageBox, QWidget,
)

from logging_setup import logger
from qt_constants import YES_NO


def _is_qobject_valid(obj) -> bool:
    if obj is None:
        return False
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except Exception:
        # если shiboken6 нет или объект ещё валиден —
        # считаем валидным
        return True


class Paths:
    """Каталоги приложения (config, cache, help, log)."""

    @staticmethod
    def get_config_dir() -> str:
        if sys.platform.startswith("linux"):
            base = os.environ.get(
                'XDG_CONFIG_HOME',
                os.path.expanduser('~/.config'))
            return os.path.join(base, "ksenia")
        appdata = os.environ.get('APPDATA')
        if appdata:
            return os.path.join(appdata, "Ksenia")
        return os.path.expanduser("~/.ksenia")

    @staticmethod
    def get_cache_dir() -> str:
        return os.path.join(Paths.get_config_dir(), "cache")

    @staticmethod
    def get_log_path() -> str:
        return os.path.join(Paths.get_config_dir(), "editor.log")

    @staticmethod
    def get_help_dir() -> str:
        return os.path.join(Paths.get_app_dir(), "help")

    @staticmethod
    def get_app_dir() -> str:
        return os.path.dirname(os.path.abspath(sys.argv[0]))


def parse_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── Диалоги подтверждения ──

def confirm(parent: Optional[QWidget], msg: str,
            title: str = "") -> bool:
    """Возвращает True, если пользователь нажал «Да»."""
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    return QMessageBox.question(
        parent,
        title or "Подтверждение",
        msg,
        YES_NO,
    ) == QMessageBox.StandardButton.Yes


def confirm_three(parent: Optional[QWidget], msg: str,
                  title: str = "") -> str:
    """Возвращает 'yes' / 'no' / 'cancel'."""
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    box = QMessageBox(parent)
    box.setWindowTitle(title or "Подтверждение")
    box.setText(msg)
    box.setIcon(QMessageBox.Icon.Question)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes
        | QMessageBox.StandardButton.No
        | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Yes)
    ret = box.exec()
    if ret == QMessageBox.StandardButton.Yes:
        return 'yes'
    if ret == QMessageBox.StandardButton.No:
        return 'no'
    return 'cancel'


def info_box(parent, msg: str, title: str = "") -> None:
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    QMessageBox.information(
        parent, title or "Информация", msg)


def warn_box(parent, msg: str, title: str = "") -> None:
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    QMessageBox.warning(
        parent, title or "Предупреждение", msg)


def error_box(parent, msg: str, title: str = "") -> None:
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    QMessageBox.critical(
        parent, title or "Ошибка", msg)


def open_file_dialog(parent, title: str, filter_str: str,
                     start_dir: str = "") -> str:
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    fp, _ = QFileDialog.getOpenFileName(
        parent, title, start_dir, filter_str)
    return fp or ""


def save_file_dialog(parent, title: str, default: str,
                     filter_str: str, suffix: str = "") -> str:
    if parent is not None and not _is_qobject_valid(parent):
        parent = None
    fp, _ = QFileDialog.getSaveFileName(
        parent, title, default, filter_str)
    if not fp:
        return ""
    if suffix and not fp.lower().endswith(suffix.lower()):
        fp += suffix
    return fp


def open_external(path: str) -> None:
    """Открывает файл/папку системным приложением."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        logger.exception("open_external(%r)", path)
        raise