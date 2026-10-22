# -*- coding: utf-8 -*-
"""Базовые диалоги, IconProvider, фабрики, helpers.

Версия 0.2.1:
  • make_action оборачивает слот в try/except — любая ошибка
    в слоте логируется, а не роняет приложение.
  • BaseDialog.add_reset_button() — кнопка «Сбросить».
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QAction, QIcon, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox,
    QLabel, QPushButton, QStyle, QTableWidget,
    QTableWidgetItem, QVBoxLayout)

from constants import (
    JSON_FILTER, OK_CANCEL_BB, CLOSE_BB,
    THEME_ICON_NEGATIVE_TTL_SEC, THEME_ICON_CACHE_MAX,
    MAX_JSON_BYTES)
from logging_setup import logger
from paths import (error_box, warn_box, open_file_dialog,
                    save_file_dialog)

try:
    import shiboken6
    _HAS_SHIBOKEN = True
except ImportError:
    shiboken6 = None
    _HAS_SHIBOKEN = False


def _is_qobject_valid(obj) -> bool:
    if obj is None:
        return False
    if not _HAS_SHIBOKEN:
        return True
    try:
        return shiboken6.isValid(obj)
    except Exception:
        return False


def _is_gui_thread() -> bool:
    app = QApplication.instance()
    if app is None:
        return True
    try:
        return QThread.currentThread() == app.thread()
    except Exception:
        return True


class IconProvider:
    _icons_enabled: bool = True
    _cache: Dict[Tuple[str, int], QIcon] = {}
    _style: Optional[QStyle] = None
    _theme_available_cache: "OrderedDict[str, Tuple[bool, float]]" = \
        OrderedDict()
    _cache_lock = threading.RLock()

    @classmethod
    def initialize(cls, style: QStyle):
        with cls._cache_lock:
            cls._style = style

    @classmethod
    def set_enabled(cls, enabled: bool):
        cls._icons_enabled = bool(enabled)

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._icons_enabled

    @classmethod
    def _theme_icon(cls, theme_name: str) -> Optional[QIcon]:
        if not theme_name or not _is_gui_thread():
            return None
        now = time.time()
        with cls._cache_lock:
            entry = cls._theme_available_cache.get(theme_name)
            if entry is not None:
                available, ts = entry
                if (available is False
                        and (now - ts) < THEME_ICON_NEGATIVE_TTL_SEC):
                    return None
        try:
            icon = QIcon.fromTheme(
                theme_name, QIcon.fromTheme("hicolor"))
            if not icon.isNull():
                with cls._cache_lock:
                    cls._theme_available_cache[theme_name] = (
                        True, now)
                    cls._theme_available_cache.move_to_end(
                        theme_name)
                    while (len(cls._theme_available_cache)
                           > THEME_ICON_CACHE_MAX):
                        cls._theme_available_cache.popitem(
                            last=False)
                return icon
        except Exception:
            pass
        with cls._cache_lock:
            cls._theme_available_cache[theme_name] = (False, now)
            cls._theme_available_cache.move_to_end(theme_name)
            while (len(cls._theme_available_cache)
                   > THEME_ICON_CACHE_MAX):
                cls._theme_available_cache.popitem(last=False)
        return None

    @classmethod
    def get(cls, sp: Optional[QStyle.StandardPixmap] = None,
            theme_name: str = "") -> QIcon:
        if not cls._icons_enabled or not _is_gui_thread():
            return QIcon()
        key = (theme_name, int(sp) if sp is not None else -1)
        with cls._cache_lock:
            cached = cls._cache.get(key)
            if cached is not None:
                return cached

        icon = QIcon()
        if theme_name:
            themed = cls._theme_icon(theme_name)
            if themed is not None:
                icon = themed
        if icon.isNull() and sp is not None:
            style = cls._style
            if style is None:
                app = QApplication.instance()
                if app is not None:
                    style = app.style()
                    cls._style = style
            if style is not None:
                try:
                    icon = style.standardIcon(sp)
                except Exception:
                    icon = QIcon()
        if not icon.isNull():
            with cls._cache_lock:
                cls._cache[key] = icon
        return icon

    @classmethod
    def clear_cache(cls):
        with cls._cache_lock:
            cls._cache.clear()
            cls._theme_available_cache.clear()
            cls._style = None


class _NumericItem(QTableWidgetItem):
    def __init__(self, value: int):
        super().__init__(str(value))
        self._value = int(value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        try:
            return self._value < int(other.text())
        except (ValueError, AttributeError, TypeError):
            return False


class BaseDialog(QDialog):
    def __init__(self, title: str, parent=None,
                 size: Tuple[int, int] = (400, 300)):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        self.root = QVBoxLayout(self)

    def add_ok_cancel(self, ok_text: Optional[str] = None
                      ) -> QDialogButtonBox:
        bb = QDialogButtonBox(OK_CANCEL_BB)
        if ok_text:
            bb.button(QDialogButtonBox.StandardButton.Ok).setText(
                ok_text)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.root.addWidget(bb)
        return bb

    def add_close(self) -> QDialogButtonBox:
        bb = QDialogButtonBox(CLOSE_BB)
        bb.rejected.connect(self.reject)
        self.root.addWidget(bb)
        return bb

    def add_reset_button(
            self, on_reset: Callable[[], None]
    ) -> Optional[QPushButton]:
        """Добавляет кнопку «Сбросить» к нижней панели.

        Ищет первый QDialogButtonBox в диалоге. Если не найден
        (например, кнопки ещё не добавлены) — возвращает None.
        """
        bb = self.findChild(QDialogButtonBox)
        if bb is None:
            return None
        btn = bb.addButton(
            "Сбросить", QDialogButtonBox.ButtonRole.ResetRole)
        btn.clicked.connect(on_reset)
        return btn


def hint_label(text: str, parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setWordWrap(True)
    f = lbl.font()
    f.setItalic(True)
    lbl.setFont(f)
    lbl.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return lbl


def make_table(headers: List[str], parent=None, *,
               stretch_last: bool = True,
               select_rows: bool = True,
               select_mode: QAbstractItemView.SelectionMode =
                   QAbstractItemView.SelectionMode.ExtendedSelection,
               edit_disabled: bool = False) -> QTableWidget:
    t = QTableWidget(parent)
    t.setColumnCount(len(headers))
    t.setHorizontalHeaderLabels(headers)
    h = t.horizontalHeader()
    if stretch_last and headers:
        h.setStretchLastSection(True)
    if select_rows:
        t.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(select_mode)
    if edit_disabled:
        t.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
    return t


def make_action(parent, text: str, slot: Callable,
                shortcut: str = "", icon: Optional[QIcon] = None,
                tooltip: str = "") -> QAction:
    a = QAction(text, parent)
    if shortcut:
        a.setShortcut(shortcut)
    if icon is not None and not icon.isNull():
        a.setIcon(icon)
    tip = tooltip or text
    a.setToolTip(tip)
    a.setStatusTip(tip)

    def _safe_slot(checked=False, _slot=slot):
        # FIX 0.2.1: любая ошибка в слоте логируется,
        # а не роняет приложение через qFatal.
        try:
            _slot()
        except Exception:
            logger.exception("Action slot failed: %s", text)

    a.triggered.connect(_safe_slot)
    return a


def fill_channels_table(table: QTableWidget,
                        channels: List['ChannelData'],
                        max_url: int = 120) -> None:
    table.setRowCount(len(channels))
    for i, ch in enumerate(channels):
        table.setItem(i, 0, QTableWidgetItem(ch.meta.name or ""))
        table.setItem(i, 1, QTableWidgetItem(ch.meta.group or ""))
        table.setItem(i, 2,
                      QTableWidgetItem((ch.link.url or "")[:max_url]))


def json_import_dialog(parent, title: str,
                       on_items: Callable[[list], int]
                       ) -> Optional[int]:
    fp = open_file_dialog(parent, title, JSON_FILTER)
    if not fp:
        return None
    try:
        if os.path.getsize(fp) > MAX_JSON_BYTES:
            error_box(parent,
                      f"JSON-файл слишком большой "
                      f"(максимум "
                      f"{MAX_JSON_BYTES // (1024 * 1024)} МБ)")
            return None
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        error_box(parent, f"Некорректный JSON:\n{e}")
        return None
    except OSError as e:
        error_box(parent, str(e))
        return None
    except Exception as e:
        logger.exception("json_import_dialog")
        error_box(parent, str(e))
        return None
    if not isinstance(data, list):
        warn_box(parent, "Ожидался JSON-массив верхнего уровня")
        return None
    try:
        return on_items(data)
    except Exception as e:
        logger.exception("json_import_dialog: on_items failed")
        error_box(parent, f"Ошибка при обработке:\n{e}")
        return None


def json_export_dialog(parent, title: str, default_name: str,
                       items: list) -> bool:
    fp = save_file_dialog(parent, title, default_name, JSON_FILTER)
    if not fp:
        return False
    try:
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.exception("json_export_dialog")
        error_box(parent, str(e))
        return False