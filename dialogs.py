# -*- coding: utf-8 -*-
"""Все QDialog-классы + IconProvider + фабрики + HelpBrowser."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import weakref
from contextlib import suppress
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from PyQt6.QtCore import (Qt, QPoint, QTimer, QUrl, pyqtSignal, QThread)
from PyQt6.QtGui import (QAction, QBrush, QColor, QDesktopServices,
    QFont, QIcon, QKeySequence, QShortcut)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication,
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QSlider, QSpinBox, QSplitter,
    QStyle, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
    QTextEdit, QVBoxLayout, QWidget)

from constants import (ALL_FILTER, APP_VERSION, CHECK_RESULT_CACHE_TTL_HOURS,
    CLOSE_BB, CSV_FILTER, DEFAULT_GROUP, DONATION_URL, DONATION_WALLET,
    DUPLICATE_DIALOG_MAX_ROWS, EPG_CACHE_TTL_HOURS,
    EPG_FUZZY_ENABLED_DEFAULT, EPG_FUZZY_MIN_GAP_DEFAULT,
    EPG_FUZZY_MIN_LENGTH_DEFAULT, EPG_FUZZY_THRESHOLD_DEFAULT,
    FALLBACK_DAYS_DEFAULT, GROUP_FILTER_ALL, JSON_FILTER, M3U_FILTER,
    OK_CANCEL_BB, REPLACEMENT_MAX_WORKERS_DEFAULT,
    SOURCE_CHECK_BATCH_SIZE_DEFAULT, SOURCE_CHECK_TIMEOUT_DEFAULT,
    SOURCE_CHECK_TRUST_SEC_DEFAULT, SOURCE_CHECK_WORKERS_DEFAULT,
    SOURCES_FINDER_MAX_GITHUB_DEFAULT,
    SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT,
    SOURCES_FINDER_UPDATE_INTERVAL_HOURS, THEME_ICON_NEGATIVE_TTL_SEC,
    URL_CHECK_MAX_WORKERS, VLC_DEFAULT_CHECK_TIMEOUT, YES_NO)
from blacklists import (DomainUserAgentManager, DomainUserAgentRule)
from config import Config
from diff_utils import (DiffLine, DiffOp, channels_only, diff_lines,
    diff_stats, normalize_lines, to_unified_diff)
from models import ChannelData
from paths import (Paths, confirm, confirm_three, error_box, info_box,
    logger, open_external, open_file_dialog, save_file_dialog, warn_box)
from sources import LinkSource, LinkSourceManager
from undo import SimpleDuplicateFinder
from utils import URLUtils
from workers import SourcesRefreshWorker

try:
    import shiboken6
    _HAS_SHIBOKEN = True
except ImportError:
    shiboken6 = None
    _HAS_SHIBOKEN = False


# =====================================================================
# Вспомогательные функции
# =====================================================================
_COLOR_ADD_BG = QColor(220, 255, 220)
_COLOR_DEL_BG = QColor(255, 220, 220)
_COLOR_REPL_BG = QColor(255, 250, 200)
_COLOR_LINENO_FG = QColor(120, 120, 120)


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


# =====================================================================
# IconProvider
# =====================================================================
class IconProvider:
    _icons_enabled: bool = True
    _cache: Dict[Tuple[str, int], QIcon] = {}
    _style: Optional[QStyle] = None
    _theme_available_cache: Dict[str, Tuple[bool, float]] = {}
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
            icon = QIcon.fromTheme(theme_name)
            if not icon.isNull():
                with cls._cache_lock:
                    cls._theme_available_cache[theme_name] = (True, now)
                return icon
        except Exception:
            pass
        with cls._cache_lock:
            cls._theme_available_cache[theme_name] = (False, now)
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
    """QTableWidgetItem с числовым сравнением для сортировки."""

    def __init__(self, value: int):
        super().__init__(str(value))
        self._value = int(value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        try:
            return self._value < int(other.text())
        except (ValueError, AttributeError):
            return super().__lt__(other)


# =====================================================================
# BaseDialog + фабрики
# =====================================================================
class BaseDialog(QDialog):
    def __init__(self, title: str, parent=None,
                 size: Tuple[int, int] = (400, 300)):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        self.root = QVBoxLayout(self)

    def add_ok_cancel(self, ok_text: Optional[str] = None) -> QDialogButtonBox:
        bb = QDialogButtonBox(OK_CANCEL_BB)
        if ok_text:
            bb.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.root.addWidget(bb)
        return bb

    def add_close(self) -> QDialogButtonBox:
        bb = QDialogButtonBox(CLOSE_BB)
        bb.rejected.connect(self.reject)
        self.root.addWidget(bb)
        return bb


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
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
    a.triggered.connect(lambda checked=False, _slot=slot: _slot())
    return a


def fill_channels_table(table: QTableWidget,
                        channels: List[ChannelData],
                        max_url: int = 120):
    table.setRowCount(len(channels))
    for i, ch in enumerate(channels):
        table.setItem(i, 0, QTableWidgetItem(ch.meta.name or ""))
        table.setItem(i, 1, QTableWidgetItem(ch.meta.group or ""))
        table.setItem(i, 2,
                      QTableWidgetItem((ch.link.url or "")[:max_url]))


def make_form(rows: List[Tuple[str, QWidget]]) -> QFormLayout:
    f = QFormLayout()
    for label, widget in rows:
        f.addRow(label, widget)
    return f


def json_import_dialog(parent, title: str,
                       on_items: Callable[[list], int]) -> Optional[int]:
    fp = open_file_dialog(parent, title, JSON_FILTER)
    if not fp:
        return None
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        error_box(parent, f"Некорректный JSON:\n{e}")
        return None
    except Exception as e:
        error_box(parent, str(e))
        return None
    if not isinstance(data, list):
        warn_box(parent, "Ожидался JSON-массив верхнего уровня")
        return None
    try:
        return on_items(data)
    except Exception as e:
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
        error_box(parent, str(e))
        return False


# =====================================================================
# HelpBrowser — встроенный просмотрщик справки
# =====================================================================
class HelpBrowser(QDialog):
    """Встроенный просмотрщик справки (index.html + соседние файлы).

    Стиль DEFAULT_CSS применяется ко всем документам через
    document().setDefaultStyleSheet(). Файлы справки НЕ должны
    содержать собственных <style>.
    """

    DEFAULT_CSS = """
        html, body, div, h1, h2, h3, h4, h5, h6, p, pre,
        ol, ul, li, table, thead, tbody, tr, th, td,
        blockquote, code, kbd, span, a, hr {
            margin: 0;
            padding: 0;
        }
        body {
            background-color: #fbfbfe;
            color: #222222;
            font-family: "Open Sans", "Segoe UI", "Noto Sans", Arial, sans-serif;
            font-size: 15px;
            line-height: 1.55;
            margin: 28px 36px;
        }
        a { color: #00538a; text-decoration: none; }
        a:hover { color: #001066; text-decoration: underline; }
        p { margin: 0 0 1.2em 0; padding-top: 0.4em; }

        kbd {
            font-family: "Consolas", "Menlo", "Courier New", monospace;
            font-weight: bold;
            background-color: #dcdcd5;
            padding: 2px 5px;
            color: #222222;
        }
        span.code {
            font-family: "Consolas", "Menlo", "Courier New", monospace;
            font-size: 13px;
            background-color: #dcdcd5;
            padding: 2px 5px;
            color: #222222;
        }
        ul, ol { margin: 1em 0 1.4em 1.5em; padding: 0; line-height: 140%; }
        li { margin: 0 0 0.5em 0; padding: 0; }

        h1, h2, h3, h4, h5, h6 {
            margin: 0.93em 0 0.2em 0;
            color: #0a2a5c;
            font-weight: bold;
        }
        h1 {
            font-weight: normal;
            font-size: 1.8em;
            padding: 4px 0 8px 0;
            border-bottom: 2px solid #5198cc;
            margin-bottom: 12px;
        }
        h2 {
            font-size: 1.5em;
            padding: 4px 0 6px 0;
            border-bottom: 1px solid rgba(53, 86, 129, 0.3);
            margin-top: 34px;
        }
        h3 { font-weight: 600; font-size: 1.25em; margin-top: 22px; }
        h4 { font-size: 1.1em; font-style: italic; margin-top: 16px; }
        h5 { font-size: 1.02em; font-style: italic; margin-top: 12px; }
        h6 { font-size: 0.85em; text-transform: uppercase; }

        pre {
            font-family: "Consolas", "Menlo", "Liberation Mono", monospace;
            font-size: 13px;
            background-color: #10131e;
            color: #e6e6e6;
            padding: 12px 14px;
            margin: 10px 0 16px 0;
            white-space: pre-wrap;
            border: 1px solid #303030;
        }
        pre code { background: none; color: inherit; padding: 0; font-size: 13px; }
        code {
            font-family: "Consolas", "Menlo", "Liberation Mono", monospace;
            font-size: 13px;
            background-color: #eef0f3;
            color: #222222;
            padding: 1px 5px;
        }

        blockquote, .box-text {
            background-color: rgba(51, 56, 71, 0.08);
            border-left: 4px solid #5198cc;
            padding: 10px 14px;
            margin: 12px 0;
        }
        blockquote p { margin: 0; }
        .important {
            background-color: #fec8c8;
            border-left: 4px solid #b30000;
            padding: 10px 14px;
            margin: 12px 0;
        }
        .important p { margin: 0; }
        .note {
            background-color: #dde6f4;
            border-left: 4px solid #5198cc;
            padding: 10px 14px;
            margin: 12px 0;
        }
        .note p { margin: 0; }

        table {
            border-collapse: collapse;
            width: 100%;
            margin: 12px 0 20px 0;
            font-size: 0.94em;
        }
        th, td {
            border: 1px solid #6cc0ff;
            padding: 6px 10px;
            text-align: left;
            vertical-align: top;
        }
        th {
            background-color: #10131e;
            color: #f0f0f0;
            font-weight: bold;
        }
        hr { border: none; border-top: 1px solid #bbcaff; margin: 28px 0; }
        .meta { color: #57606a; font-size: 13px; margin: 4px 0 20px 0; }
        .meta strong { color: #222222; }
    """

    def __init__(self, index_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Справка — Ksenia M3U Editor")
        self.resize(1000, 720)
        self.root = QVBoxLayout(self)

        self.index_path = os.path.abspath(index_path)
        self.help_dir = os.path.dirname(self.index_path)
        self.start_page = os.path.basename(self.index_path)

        if not os.path.isfile(self.index_path):
            error_box(self,
                      f"Файл справки не найден:\n{self.index_path}",
                      "Справка")
            QTimer.singleShot(0, self.reject)
            return

        # Верхняя панель
        nav = QHBoxLayout()
        self.btn_back = QPushButton("← Назад")
        self.btn_fwd = QPushButton("→ Вперёд")
        self.btn_home = QPushButton("🏠 Домой")
        self.btn_refresh = QPushButton("🔄")
        self.btn_refresh.setToolTip("Обновить страницу (F5)")
        self.btn_back.setEnabled(False)
        self.btn_fwd.setEnabled(False)

        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_fwd)
        nav.addWidget(self.btn_home)
        nav.addWidget(self.btn_refresh)
        nav.addStretch()

        self.path_label = QLabel("")
        self.path_label.setStyleSheet("color: gray; font-size: 11px;")
        nav.addWidget(self.path_label)
        self.root.addLayout(nav)

        # Splitter: оглавление + содержимое
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.toc = QListWidget()
        self.toc.setMaximumWidth(280)
        self.toc.itemClicked.connect(self._on_toc_click)
        splitter.addWidget(self.toc)

        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(False)
        self.viewer.setOpenLinks(False)
        self.viewer.anchorClicked.connect(self._on_anchor)
        self.viewer.backwardAvailable.connect(self.btn_back.setEnabled)
        self.viewer.forwardAvailable.connect(self.btn_fwd.setEnabled)
        self.viewer.sourceChanged.connect(self._on_source_changed)
        self.viewer.document().setDefaultStyleSheet(self.DEFAULT_CSS)
        splitter.addWidget(self.viewer)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.root.addWidget(splitter, 1)

        self.btn_back.clicked.connect(self.viewer.backward)
        self.btn_fwd.clicked.connect(self.viewer.forward)
        self.btn_home.clicked.connect(self._go_home)
        self.btn_refresh.clicked.connect(self._reload)

        QShortcut(QKeySequence("Alt+Left"), self,
                  activated=self.viewer.backward)
        QShortcut(QKeySequence("Alt+Right"), self,
                  activated=self.viewer.forward)
        QShortcut(QKeySequence("F5"), self, activated=self._reload)
        QShortcut(QKeySequence("Home"), self, activated=self._go_home)

        self._fill_toc()
        self._go_home()

        bb = QDialogButtonBox(CLOSE_BB)
        bb.rejected.connect(self.reject)
        self.root.addWidget(bb)

    # --- Навигация ---
    def _go_home(self):
        url = QUrl.fromLocalFile(self.index_path)
        self.viewer.setSource(url)
        QTimer.singleShot(
            0, lambda: self.viewer.verticalScrollBar().setValue(0))

    def _reload(self):
        cur = self.viewer.source()
        if cur.isEmpty():
            self._go_home()
            return
        self.viewer.setSource(QUrl())
        self.viewer.setSource(cur)

    def _on_source_changed(self, url: QUrl):
        name = url.fileName() or ""
        self.path_label.setText(name)
        self._sync_toc_selection(url)

    def _on_anchor(self, url: QUrl):
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        if url.scheme() == "" and url.path() == "":
            self.viewer.scrollToAnchor(url.fragment())
            return
        if url.scheme() == "":
            base_dir = os.path.dirname(
                self.viewer.source().toLocalFile() or self.index_path)
            target = os.path.normpath(
                os.path.join(base_dir, url.path()))
            new_url = QUrl.fromLocalFile(target)
            if url.fragment():
                new_url.setFragment(url.fragment())
            self.viewer.setSource(new_url)
            if url.fragment():
                self.viewer.scrollToAnchor(url.fragment())
            return
        QDesktopServices.openUrl(url)

    # --- Оглавление ---
    def _fill_toc(self):
        if not os.path.isfile(self.index_path):
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception:
            return
        pattern = re.compile(
            r'<h2[^>]*id=["\']([^"\']+)["\'][^>]*>(.*?)</h2>',
            re.DOTALL | re.IGNORECASE)
        for match in pattern.finditer(html):
            anchor = match.group(1)
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if not title:
                continue
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, anchor)
            self.toc.addItem(item)

    def _on_toc_click(self, item: QListWidgetItem):
        anchor = item.data(Qt.ItemDataRole.UserRole)
        if not anchor:
            return
        current = self.viewer.source().toLocalFile()
        if current and os.path.normpath(current) == \
                os.path.normpath(self.index_path):
            self.viewer.scrollToAnchor(anchor)
            return
        url = QUrl.fromLocalFile(self.index_path)
        url.setFragment(anchor)
        self.viewer.setSource(url)
        self.viewer.scrollToAnchor(anchor)

    def _sync_toc_selection(self, url: QUrl):
        frag = url.fragment()
        for i in range(self.toc.count()):
            item = self.toc.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == frag:
                self.toc.setCurrentItem(item)
                return


# =====================================================================
# SupportDialog, HelpDialog
# =====================================================================
class SupportDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__("Поддержка проекта", parent, size=(500, 400))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(500)
        l = self.root
        title = QLabel("☕ Поддержать проект")
        f = title.font()
        f.setPointSize(16)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l.addWidget(title)
        info = QLabel(
            "Если вам нравится этот инструмент и вы хотите поддержать его развитие,\n"
            "вы можете отправить добровольное пожертвование.\n\n"
            "Спасибо за вашу поддержку! 💝")
        info.setWordWrap(True)
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l.addWidget(info)
        btn = QPushButton("💰 Отправить перевод")
        btn.setMinimumHeight(50)
        btn.clicked.connect(lambda: open_external(DONATION_URL))
        l.addWidget(btn)
        wallet_label = QLabel(f"Кошелёк: <b>{DONATION_WALLET}</b> (ЮMoney)")
        wallet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wallet_label.setTextFormat(Qt.TextFormat.RichText)
        l.addWidget(wallet_label)
        self.add_close()


class HelpDialog(BaseDialog):
    """Расширенный диалог справки: О проекте, Клавиши, Лицензия, Поддержка."""

    def __init__(self, parent=None):
        super().__init__("Справка", parent, size=(700, 600))
        self.setMinimumWidth(700)
        self.setMinimumHeight(600)
        layout = self.root
        tabs = QTabWidget()

        general = QWidget()
        gl = QVBoxLayout(general)
        gl.addWidget(self._html_label(
            f"<h2>Ksenia M3U Editor {APP_VERSION}</h2>"
            "<p>Редактор и менеджер IPTV плейлистов в формате M3U/M3U8.</p>"
            "<h3>О проекте</h3>"
            "<table cellpadding='4'>"
            "<tr><td><b>Автор:</b></td><td>SmolNP (Смольянинов Николай)</td></tr>"
            "<tr><td><b>Профиль автора:</b></td>"
            "<td><a href='https://github.com/smolnp'>https://github.com/smolnp</a></td></tr>"
            "<tr><td><b>Репозиторий:</b></td>"
            "<td><a href='https://github.com/smolnp/Ksenia'>https://github.com/smolnp/Ksenia</a></td></tr>"
            "<tr><td><b>Версия:</b></td><td>" + APP_VERSION + "</td></tr>"
            "<tr><td><b>Лицензия:</b></td>"
            "<td>GNU GPL v3.0</td></tr>"
            "</table>"
            "<p style='margin-top:1em;'>"
            "<b>F1</b> — открыть встроенное руководство пользователя."
            "</p>"))
        gl.addStretch()
        tabs.addTab(general, "О проекте")

        shortcuts = QWidget()
        shl = QVBoxLayout(shortcuts)
        shl.addWidget(self._html_label(
            "<h3>Горячие клавиши</h3>"
            "<ul>"
            "<li><b>F1</b> — руководство пользователя</li>"
            "<li><b>F11</b> — полноэкранный режим</li>"
            "<li><b>Ctrl+N</b> — новый плейлист</li>"
            "<li><b>Ctrl+O</b> — открыть</li>"
            "<li><b>Ctrl+S</b> — сохранить</li>"
            "<li><b>Ctrl+Z / Ctrl+Y</b> — отменить/повторить</li>"
            "<li><b>Ctrl+C / X / V</b> — копировать/вырезать/вставить</li>"
            "<li><b>Delete</b> — удалить канал</li>"
            "</ul>"))
        shl.addStretch()
        tabs.addTab(shortcuts, "Клавиши")

        lt_tab = QWidget()
        lt_l = QVBoxLayout(lt_tab)
        lt_l.setContentsMargins(8, 8, 8, 8)
        lt_header = QLabel("GNU General Public License v3.0")
        _f = lt_header.font()
        _f.setPointSize(14)
        _f.setBold(True)
        lt_header.setFont(_f)
        lt_header.setTextFormat(Qt.TextFormat.RichText)
        lt_l.addWidget(lt_header)
        lt_text = QPlainTextEdit()
        lt_text.setReadOnly(True)
        lt_text.setPlainText(
            "This program is free software: you can redistribute it "
            "and/or modify it under the terms of the GNU General "
            "Public License as published by the Free Software "
            "Foundation, either version 3 of the License, or "
            "(at your option) any later version.\n\n"
            "This program is distributed in the hope that it will "
            "be useful, but WITHOUT ANY WARRANTY; without even the "
            "implied warranty of MERCHANTABILITY or FITNESS FOR A "
            "PARTICULAR PURPOSE. See the GNU General Public License "
            "for more details.\n\n"
            "You should have received a copy of the GNU General "
            "Public License along with this program. If not, see "
            "<https://www.gnu.org/licenses/>.\n")
        lt_l.addWidget(lt_text, 1)
        lt_link = QLabel(
            "<a href='https://www.gnu.org/licenses/gpl-3.0.html'>"
            "Полный текст лицензии GPLv3</a>")
        lt_link.setTextFormat(Qt.TextFormat.RichText)
        lt_link.setOpenExternalLinks(True)
        lt_l.addWidget(lt_link)
        tabs.addTab(lt_tab, "Лицензия GPLv3")

        sup_tab = QWidget()
        spl = QVBoxLayout(sup_tab)
        spl.addWidget(QLabel("Поддержать развитие проекта:"))
        b = QPushButton("☕ Поддержать проект")
        b.clicked.connect(lambda: SupportDialog(self).exec())
        spl.addWidget(b)
        spl.addStretch()
        tabs.addTab(sup_tab, "Поддержка")

        layout.addWidget(tabs)
        self.add_close()

    @staticmethod
    def _html_label(html: str) -> QLabel:
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(True)
        return lbl


# =====================================================================
# PlaylistHeaderDialog
# =====================================================================
class PlaylistHeaderDialog(BaseDialog):
    def __init__(self, header_manager, parent=None):
        super().__init__("Редактор заголовка плейлиста", parent,
                         size=(600, 450))
        self.header_manager = header_manager.copy()
        self._setup_ui()
        self._load_current()

    def _setup_ui(self):
        layout = self.root
        g = QGroupBox("Основные")
        gl = QFormLayout(g)
        self.playlist_name_edit = QLineEdit()
        gl.addRow("Название плейлиста:", self.playlist_name_edit)
        layout.addWidget(g)

        eg = QGroupBox("Источники EPG (url-tvg)")
        el = QVBoxLayout(eg)
        info = QLabel("✓ EPG-источники сохраняются в заголовок.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #2E7D32;")
        el.addWidget(info)
        self.epg_list = QListWidget()
        el.addWidget(self.epg_list)
        bl = QHBoxLayout()
        for t, s in (("Добавить", self._add_epg),
                     ("Редактировать", self._edit_epg),
                     ("Удалить", self._remove_epg)):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        el.addLayout(bl)
        layout.addWidget(eg)

        cg = QGroupBox("Пользовательские атрибуты")
        cl = QVBoxLayout(cg)
        self.custom_attrs_table = make_table(["Ключ", "Значение"])
        cl.addWidget(self.custom_attrs_table)
        cbl = QHBoxLayout()
        for t, s in (("Добавить", self._add_attr),
                     ("Редактировать", self._edit_attr),
                     ("Удалить", self._remove_attr)):
            b = QPushButton(t)
            b.clicked.connect(s)
            cbl.addWidget(b)
        cl.addLayout(cbl)
        layout.addWidget(cg)

        pg = QGroupBox("Предпросмотр")
        pl = QVBoxLayout(pg)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setFont(QFont("Courier New", 10))
        self.preview_text.setMaximumHeight(100)
        self.preview_text.setReadOnly(True)
        pl.addWidget(self.preview_text)
        layout.addWidget(pg)

        self.add_ok_cancel()

    def _load_current(self):
        self.playlist_name_edit.setText(self.header_manager.playlist_name)
        self.epg_list.clear()
        for s in self.header_manager.epg_sources:
            self.epg_list.addItem(s)
        self._update_custom_attrs()
        self._update_preview()

    def _update_custom_attrs(self):
        attrs = self.header_manager.custom_attributes
        self.custom_attrs_table.setRowCount(len(attrs))
        for i, (k, v) in enumerate(attrs.items()):
            self.custom_attrs_table.setItem(i, 0, QTableWidgetItem(k))
            self.custom_attrs_table.setItem(i, 1, QTableWidgetItem(v))

    def _update_preview(self):
        self.preview_text.setPlainText(self.header_manager.get_header_text())

    def _add_epg(self):
        url, ok = QInputDialog.getText(
            self, "Добавить EPG", "URL:", QLineEdit.EchoMode.Normal,
            "http://example.com/epg.xml")
        if ok and url:
            self.epg_list.addItem(url)

    def _edit_epg(self):
        it = self.epg_list.currentItem()
        if not it:
            return
        url, ok = QInputDialog.getText(
            self, "Редактировать EPG", "URL:",
            QLineEdit.EchoMode.Normal, it.text())
        if ok and url:
            it.setText(url)

    def _remove_epg(self):
        it = self.epg_list.currentItem()
        if it:
            self.epg_list.takeItem(self.epg_list.row(it))

    def _add_attr(self):
        k, ok1 = QInputDialog.getText(self, "Добавить", "Ключ:")
        if ok1 and k:
            v, ok2 = QInputDialog.getText(self, "Добавить",
                                          f"Значение для '{k}':")
            if ok2:
                self.header_manager.add_custom_attribute(k, v)
                self._update_custom_attrs()
                self._update_preview()

    def _edit_attr(self):
        row = self.custom_attrs_table.currentRow()
        if row < 0:
            return
        ki = self.custom_attrs_table.item(row, 0)
        vi = self.custom_attrs_table.item(row, 1)
        if not (ki and vi):
            return
        nk, ok1 = QInputDialog.getText(
            self, "Редактировать", "Новый ключ:",
            QLineEdit.EchoMode.Normal, ki.text())
        if not (ok1 and nk):
            return
        nv, ok2 = QInputDialog.getText(
            self, "Редактировать", f"Значение для '{nk}':",
            QLineEdit.EchoMode.Normal, vi.text())
        if ok2:
            self.header_manager.remove_custom_attribute(ki.text())
            self.header_manager.add_custom_attribute(nk, nv)
            self._update_custom_attrs()
            self._update_preview()

    def _remove_attr(self):
        row = self.custom_attrs_table.currentRow()
        if row < 0:
            return
        ki = self.custom_attrs_table.item(row, 0)
        if ki:
            self.header_manager.remove_custom_attribute(ki.text())
            self._update_custom_attrs()
            self._update_preview()

    def accept(self):
        epg = [self.epg_list.item(i).text()
               for i in range(self.epg_list.count())]
        self.header_manager.update_epg_sources(epg)
        self.header_manager.set_playlist_name(self.playlist_name_edit.text())
        super().accept()


# =====================================================================
# RemoveMetadataDialog, MassEditDialog
# =====================================================================
class RemoveMetadataDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__("Удаление метаданных", parent, size=(400, 320))
        l = self.root
        l.addWidget(QLabel("Выберите метаданные:"))
        g = QGroupBox("Параметры")
        gl = QVBoxLayout(g)
        self.tvg_id_check = QCheckBox("Удалить tvg-id")
        self.tvg_name_check = QCheckBox("Удалить tvg-name")
        self.tvg_logo_check = QCheckBox("Удалить tvg-logo")
        self.group_title_check = QCheckBox("Удалить group-title")
        self.user_agent_check = QCheckBox("Удалить User Agent")
        for w in (self.tvg_id_check, self.tvg_name_check,
                  self.tvg_logo_check, self.group_title_check,
                  self.user_agent_check):
            gl.addWidget(w)
        l.addWidget(g)
        sg = QGroupBox("Область")
        sl = QVBoxLayout(sg)
        self._scope_group = QButtonGroup(self)
        self._scope_group.addButton(QRadioButton("Текущий канал"), 0)
        self._scope_group.addButton(QRadioButton("Выбранные"), 1)
        self._scope_group.addButton(QRadioButton("Все каналы"), 2)
        self._scope_group.button(0).setChecked(True)
        for btn in self._scope_group.buttons():
            sl.addWidget(btn)
        l.addWidget(sg)
        self.add_ok_cancel()

    def get_options(self) -> Dict[str, bool]:
        return {
            'tvg_id': self.tvg_id_check.isChecked(),
            'tvg_name': self.tvg_name_check.isChecked(),
            'tvg_logo': self.tvg_logo_check.isChecked(),
            'group_title': self.group_title_check.isChecked(),
            'user_agent': self.user_agent_check.isChecked(),
        }

    def get_scope(self) -> str:
        return {0: "current", 1: "selected", 2: "all"}.get(
            self._scope_group.checkedId(), "all")


class MassEditDialog(BaseDialog):
    def __init__(self, count: int, parent=None):
        super().__init__(f"Массовая правка ({count} каналов)",
                         parent, size=(520, 320))
        l = self.root
        l.addWidget(QLabel(f"<b>Выбрано каналов:</b> {count}"))
        g = QGroupBox("Что изменить")
        gl = QVBoxLayout(g)
        self.ua_check = QCheckBox("User-Agent")
        self.tvg_id_check = QCheckBox("TVG-ID")
        self.tvg_logo_check = QCheckBox("TVG-Logo")
        self.group_check = QCheckBox("Group-title")
        for w in (self.ua_check, self.tvg_id_check,
                  self.tvg_logo_check, self.group_check):
            gl.addWidget(w)
        l.addWidget(g)
        self.ua_edit = QLineEdit()
        self.ua_edit.setPlaceholderText("Оставьте пустым, чтобы удалить")
        self.tvg_id_edit = QLineEdit()
        self.tvg_logo_edit = QLineEdit()
        self.group_edit = QLineEdit()
        l.addLayout(make_form([
            ("User-Agent:", self.ua_edit),
            ("TVG-ID:", self.tvg_id_edit),
            ("TVG-Logo:", self.tvg_logo_edit),
            ("Group-title:", self.group_edit),
        ]))
        self.add_ok_cancel()

    def get_changes(self) -> Dict[str, str]:
        result = {}
        if self.ua_check.isChecked():
            result['user_agent'] = self.ua_edit.text().strip()
        if self.tvg_id_check.isChecked():
            result['tvg_id'] = self.tvg_id_edit.text().strip()
        if self.tvg_logo_check.isChecked():
            result['tvg_logo'] = self.tvg_logo_edit.text().strip()
        if self.group_check.isChecked():
            result['group'] = self.group_edit.text().strip()
        return result

    def accept(self):
        if not any((self.ua_check.isChecked(), self.tvg_id_check.isChecked(),
                    self.tvg_logo_check.isChecked(),
                    self.group_check.isChecked())):
            warn_box(self, "Выберите хотя бы одно поле")
            return
        super().accept()


# =====================================================================
# LinkReplacementSettingsDialog
# =====================================================================
class LinkReplacementSettingsDialog(BaseDialog):
    """Настройки замены ссылок (вкладки: Поиск, Нормализация,
    Автозамена, Сеть, Фильтрация, EPG)."""

    def __init__(self, config: Config, parent=None):
        super().__init__("Настройки замены ссылок", parent, size=(640, 720))
        self.config = config
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        layout = self.root
        tabs = QTabWidget()

        # --- Поиск ---
        search_tab = QWidget()
        f1 = QFormLayout(search_tab)
        self.search_type_combo = QComboBox()
        self.search_type_combo.addItem("Точное совпадение", "exact")
        self.search_type_combo.addItem("Похожие (SequenceMatcher)", "similar")
        self.search_type_combo.addItem("Нечёткий (Jaccard)", "fuzzy")
        f1.addRow("Тип поиска:", self.search_type_combo)

        self.match_threshold_spin = QDoubleSpinBox()
        self.match_threshold_spin.setRange(0.0, 100.0)
        self.match_threshold_spin.setSuffix(" %")
        self.match_threshold_spin.setDecimals(1)
        f1.addRow("Порог совпадения:", self.match_threshold_spin)

        self.min_similarity_spin = QDoubleSpinBox()
        self.min_similarity_spin.setRange(0.0, 1.0)
        self.min_similarity_spin.setSingleStep(0.05)
        self.min_similarity_spin.setDecimals(2)
        f1.addRow("Мин. схожесть имён:", self.min_similarity_spin)

        self.use_fuzzy_check = QCheckBox("Использовать нечёткое сравнение")
        f1.addRow(self.use_fuzzy_check)
        tabs.addTab(search_tab, "Поиск")

        # --- Нормализация ---
        norm_tab = QWidget()
        f2 = QFormLayout(norm_tab)
        self.ignore_special_check = QCheckBox("Игнорировать спецсимволы")
        self.remove_parens_check = QCheckBox("Удалять (...) ")
        self.remove_brackets_check = QCheckBox("Удалять [...]")
        self.remove_emoji_check = QCheckBox("Удалять эмодзи")
        for w in (self.ignore_special_check, self.remove_parens_check,
                  self.remove_brackets_check, self.remove_emoji_check):
            f2.addRow(w)
        tabs.addTab(norm_tab, "Нормализация")

        # --- Автозамена ---
        auto_tab = QWidget()
        f3 = QFormLayout(auto_tab)
        self.auto_broken_check = QCheckBox("Заменять битые ссылки")
        self.auto_missing_check = QCheckBox("Заполнять отсутствующие ссылки")
        self.keep_backup_check = QCheckBox("Сохранять старые ссылки")
        for w in (self.auto_broken_check, self.auto_missing_check,
                  self.keep_backup_check):
            f3.addRow(w)
        self.max_alt_spin = QSpinBox()
        self.max_alt_spin.setRange(1, 50)
        f3.addRow("Макс. альтернативных ссылок:", self.max_alt_spin)

        self.replacement_workers_spin = QSpinBox()
        self.replacement_workers_spin.setRange(1, 8)
        f3.addRow("Потоков замены (каналов):", self.replacement_workers_spin)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        f3.addRow(sep)

        self.fast_replacement_check = QCheckBox(
            "⚡ Быстрая замена (агрессивные таймауты)")
        self.fast_replacement_check.setToolTip(
            "Использовать короткие таймауты и без повторов.")
        f3.addRow(self.fast_replacement_check)

        self.replace_timeout_spin = QSpinBox()
        self.replace_timeout_spin.setRange(1, 60)
        self.replace_timeout_spin.setSuffix(" сек")
        f3.addRow("Таймаут замены:", self.replace_timeout_spin)

        self.replace_retries_spin = QSpinBox()
        self.replace_retries_spin.setRange(0, 5)
        f3.addRow("Повторов при замене:", self.replace_retries_spin)

        self.max_urls_per_channel_spin = QSpinBox()
        self.max_urls_per_channel_spin.setRange(1, 50)
        f3.addRow("Макс. URL на канал:", self.max_urls_per_channel_spin)

        self.cache_trust_spin = QSpinBox()
        self.cache_trust_spin.setRange(60, 86400)
        self.cache_trust_spin.setSuffix(" сек")
        self.cache_trust_spin.setToolTip(
            "Если URL проверялся меньше N секунд назад и он живой —\n"
            "используем результат кэша без повторной проверки.")
        f3.addRow("Доверять кэшу проверок (сек):", self.cache_trust_spin)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        f3.addRow(sep2)

        self.source_check_workers_spin = QSpinBox()
        self.source_check_workers_spin.setRange(1, 32)
        f3.addRow("Потоков проверки источников:",
                  self.source_check_workers_spin)

        self.source_check_timeout_spin = QSpinBox()
        self.source_check_timeout_spin.setRange(1, 60)
        self.source_check_timeout_spin.setSuffix(" сек")
        f3.addRow("Таймаут проверки источников:",
                  self.source_check_timeout_spin)

        self.source_check_trust_spin = QSpinBox()
        self.source_check_trust_spin.setRange(60, 86400)
        self.source_check_trust_spin.setSuffix(" сек")
        f3.addRow("Доверять кэшу (источники):",
                  self.source_check_trust_spin)

        self.source_check_batch_spin = QSpinBox()
        self.source_check_batch_spin.setRange(10, 1000)
        f3.addRow("Размер батча записи:", self.source_check_batch_spin)

        info_fast = QLabel(
            "⚡ Быстрая замена эффективна для массовой замены сотен\n"
            "битых каналов. Для точной проверки одной ссылки используйте\n"
            "«Проверить все ссылки».\n\n"
            "🔄 Фоновое наполнение кэша ускоряет последующие замены:\n"
            "если URL уже проверен и жив, замена произойдёт мгновенно.\n\n"
            "Источники проверяются ТОЛЬКО через «Обновить всё»\n"
            "в менеджере источников.")
        info_fast.setWordWrap(True)
        info_fast.setStyleSheet("color: gray; font-style: italic;")
        f3.addRow(info_fast)
        tabs.addTab(auto_tab, "Автозамена")

        # --- Сеть ---
        net_tab = QWidget()
        f4 = QFormLayout(net_tab)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 120)
        self.timeout_spin.setSuffix(" сек")
        f4.addRow("Таймаут проверки (обычная):", self.timeout_spin)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, URL_CHECK_MAX_WORKERS)
        f4.addRow("Потоков проверки:", self.workers_spin)
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        f4.addRow("Повторов (обычная):", self.retries_spin)
        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0.0, 10.0)
        self.retry_delay_spin.setSingleStep(0.1)
        self.retry_delay_spin.setSuffix(" сек")
        f4.addRow("Задержка между повторами:", self.retry_delay_spin)
        self.verify_ssl_check = QCheckBox("Проверять SSL сертификаты")
        f4.addRow(self.verify_ssl_check)
        tabs.addTab(net_tab, "Сеть")

        # --- Фильтрация ---
        filt_tab = QWidget()
        f5 = QFormLayout(filt_tab)
        self.use_ip_filter_check = QCheckBox("Использовать IP/домен-фильтры")
        f5.addRow(self.use_ip_filter_check)
        info_bl = QLabel(
            "ЧС домен/IP управляется отдельно. По умолчанию: "
            "ОЧИЩАЕТ ссылку, канал сохраняется.")
        info_bl.setWordWrap(True)
        info_bl.setStyleSheet("color: gray; font-style: italic;")
        f5.addRow(info_bl)

        self.temporary_domains_edit = QTextEdit()
        self.temporary_domains_edit.setMaximumHeight(50)
        f5.addRow("Временные домены:", self.temporary_domains_edit)

        self.unsafe_domains_edit = QTextEdit()
        self.unsafe_domains_edit.setMaximumHeight(50)
        f5.addRow("Небезопасные домены:", self.unsafe_domains_edit)
        tabs.addTab(filt_tab, "Фильтрация")

        # --- EPG-метаданные ---
        epg_tab = QWidget()
        f6 = QFormLayout(epg_tab)
        self.epg_overwrite_check = QCheckBox(
            "Перезаписывать существующие метаданные из EPG")
        f6.addRow(self.epg_overwrite_check)

        self.epg_fuzzy_enabled_check = QCheckBox(
            "Нечёткий поиск EPG по имени канала")
        f6.addRow(self.epg_fuzzy_enabled_check)

        self.epg_fuzzy_threshold_spin = QDoubleSpinBox()
        self.epg_fuzzy_threshold_spin.setRange(0.5, 1.0)
        self.epg_fuzzy_threshold_spin.setSingleStep(0.01)
        self.epg_fuzzy_threshold_spin.setDecimals(2)
        f6.addRow("Порог нечёткого поиска:", self.epg_fuzzy_threshold_spin)

        self.epg_fuzzy_min_length_spin = QSpinBox()
        self.epg_fuzzy_min_length_spin.setRange(1, 50)
        f6.addRow("Мин. длина имени:", self.epg_fuzzy_min_length_spin)

        self.epg_fuzzy_min_gap_spin = QDoubleSpinBox()
        self.epg_fuzzy_min_gap_spin.setRange(0.0, 0.5)
        self.epg_fuzzy_min_gap_spin.setSingleStep(0.01)
        self.epg_fuzzy_min_gap_spin.setDecimals(2)
        f6.addRow("Мин. отрыв от 2-го места:", self.epg_fuzzy_min_gap_spin)

        info_epg = QLabel(
            "Имена каналов (meta.name) никогда не перезаписываются из EPG. "
            "Обновляются только tvg-id, tvg-name, tvg-logo, tvg-chno. "
            "Поле meta.epg_source фиксирует источник EPG.")
        info_epg.setWordWrap(True)
        info_epg.setStyleSheet("color: gray; font-style: italic;")
        f6.addRow(info_epg)
        tabs.addTab(epg_tab, "EPG-метаданные")

        layout.addWidget(tabs)
        self.add_ok_cancel()

        self.fast_replacement_check.toggled.connect(
            self._on_fast_replacement_toggled)

    def _on_fast_replacement_toggled(self, checked: bool):
        for w in (self.replace_timeout_spin,
                  self.replace_retries_spin,
                  self.max_urls_per_channel_spin,
                  self.cache_trust_spin):
            w.setEnabled(bool(checked))

    def _load(self):
        c = self.config
        idx = self.search_type_combo.findData(c.get('search_type', 'exact'))
        if idx >= 0:
            self.search_type_combo.setCurrentIndex(idx)
        self.match_threshold_spin.setValue(
            float(c.get('match_threshold_percent', 80.0)))
        self.min_similarity_spin.setValue(
            float(c.get('min_name_similarity', 0.1)))
        self.use_fuzzy_check.setChecked(
            bool(c.get('use_fuzzy_matching', True)))
        self.ignore_special_check.setChecked(
            bool(c.get('ignore_special_chars_in_names', True)))
        self.remove_parens_check.setChecked(
            bool(c.get('remove_parentheses_in_names', True)))
        self.remove_brackets_check.setChecked(
            bool(c.get('remove_brackets_in_names', True)))
        self.remove_emoji_check.setChecked(
            bool(c.get('remove_emojis_in_names', True)))
        self.auto_broken_check.setChecked(
            bool(c.get('auto_replace_broken', True)))
        self.auto_missing_check.setChecked(
            bool(c.get('auto_replace_missing', True)))
        self.keep_backup_check.setChecked(
            bool(c.get('keep_backup_links', True)))
        self.max_alt_spin.setValue(int(c.get('max_alternative_urls', 5)))
        self.replacement_workers_spin.setValue(
            int(c.get('replacement_max_workers',
                      REPLACEMENT_MAX_WORKERS_DEFAULT)))
        self.fast_replacement_check.setChecked(
            bool(c.get('fast_replacement_mode', True)))
        self.replace_timeout_spin.setValue(
            int(c.get('replace_check_timeout', 2)))
        self.replace_retries_spin.setValue(
            int(c.get('replace_max_retries', 0)))
        self.max_urls_per_channel_spin.setValue(
            int(c.get('max_urls_to_check_per_channel', 3)))
        self.cache_trust_spin.setValue(
            int(c.get('check_cache_trust_seconds', 3600)))
        self.source_check_workers_spin.setValue(
            int(c.get('source_check_workers', SOURCE_CHECK_WORKERS_DEFAULT)))
        self.source_check_timeout_spin.setValue(
            int(c.get('source_check_timeout', SOURCE_CHECK_TIMEOUT_DEFAULT)))
        self.source_check_trust_spin.setValue(
            int(c.get('source_check_trust_sec',
                      SOURCE_CHECK_TRUST_SEC_DEFAULT)))
        self.source_check_batch_spin.setValue(
            int(c.get('source_check_batch_size',
                      SOURCE_CHECK_BATCH_SIZE_DEFAULT)))
        self._on_fast_replacement_toggled(
            self.fast_replacement_check.isChecked())

        self.timeout_spin.setValue(
            int(c.get('check_timeout', VLC_DEFAULT_CHECK_TIMEOUT)))
        self.workers_spin.setValue(
            int(c.get('max_workers', URL_CHECK_MAX_WORKERS)))
        self.retries_spin.setValue(int(c.get('max_retries', 0)))
        self.retry_delay_spin.setValue(float(c.get('retry_delay', 0.5)))
        self.verify_ssl_check.setChecked(bool(c.get('verify_ssl', False)))
        self.use_ip_filter_check.setChecked(
            bool(c.get('use_ip_filtering', True)))
        self.temporary_domains_edit.setPlainText(
            "\n".join(c.get('temporary_domains', [])))
        self.unsafe_domains_edit.setPlainText(
            "\n".join(c.get('unsafe_domains', [])))
        self.epg_overwrite_check.setChecked(
            bool(c.get('epg_overwrite_metadata', True)))
        self.epg_fuzzy_enabled_check.setChecked(
            bool(c.get('epg_fuzzy_match_enabled',
                       EPG_FUZZY_ENABLED_DEFAULT)))
        self.epg_fuzzy_threshold_spin.setValue(
            float(c.get('epg_fuzzy_threshold',
                        EPG_FUZZY_THRESHOLD_DEFAULT)))
        self.epg_fuzzy_min_length_spin.setValue(
            int(c.get('epg_fuzzy_min_length',
                      EPG_FUZZY_MIN_LENGTH_DEFAULT)))
        self.epg_fuzzy_min_gap_spin.setValue(
            float(c.get('epg_fuzzy_min_gap',
                        EPG_FUZZY_MIN_GAP_DEFAULT)))

    @staticmethod
    def _split_lines(text: str) -> List[str]:
        result: List[str] = []
        for line in text.replace(',', '\n').splitlines():
            line = line.strip()
            if line:
                result.append(line)
        return result

    def _apply(self):
        c = self.config
        c.set('search_type', self.search_type_combo.currentData() or "exact")
        c.set('match_threshold_percent', self.match_threshold_spin.value())
        c.set('min_name_similarity', self.min_similarity_spin.value())
        c.set('use_fuzzy_matching', self.use_fuzzy_check.isChecked())
        c.set('ignore_special_chars_in_names',
              self.ignore_special_check.isChecked())
        c.set('remove_parentheses_in_names',
              self.remove_parens_check.isChecked())
        c.set('remove_brackets_in_names',
              self.remove_brackets_check.isChecked())
        c.set('remove_emojis_in_names', self.remove_emoji_check.isChecked())
        c.set('auto_replace_broken', self.auto_broken_check.isChecked())
        c.set('auto_replace_missing', self.auto_missing_check.isChecked())
        c.set('keep_backup_links', self.keep_backup_check.isChecked())
        c.set('max_alternative_urls', self.max_alt_spin.value())
        c.set('replacement_max_workers',
              self.replacement_workers_spin.value())
        c.set('fast_replacement_mode',
              self.fast_replacement_check.isChecked())
        c.set('replace_check_timeout', self.replace_timeout_spin.value())
        c.set('replace_max_retries', self.replace_retries_spin.value())
        c.set('replace_retry_delay', 0.0)
        c.set('max_urls_to_check_per_channel',
              self.max_urls_per_channel_spin.value())
        c.set('check_cache_trust_seconds', self.cache_trust_spin.value())
        c.set('source_check_workers', self.source_check_workers_spin.value())
        c.set('source_check_timeout',
              self.source_check_timeout_spin.value())
        c.set('source_check_trust_sec',
              self.source_check_trust_spin.value())
        c.set('source_check_batch_size',
              self.source_check_batch_spin.value())

        c.set('check_timeout', self.timeout_spin.value())
        c.set('max_workers', self.workers_spin.value())
        c.set('max_retries', self.retries_spin.value())
        c.set('retry_delay', self.retry_delay_spin.value())
        c.set('verify_ssl', self.verify_ssl_check.isChecked())
        c.set('use_ip_filtering', self.use_ip_filter_check.isChecked())
        c.set('temporary_domains',
              self._split_lines(self.temporary_domains_edit.toPlainText()))
        c.set('unsafe_domains',
              self._split_lines(self.unsafe_domains_edit.toPlainText()))
        c.set('epg_overwrite_metadata',
              self.epg_overwrite_check.isChecked())
        c.set('epg_fuzzy_match_enabled',
              self.epg_fuzzy_enabled_check.isChecked())
        c.set('epg_fuzzy_threshold',
              self.epg_fuzzy_threshold_spin.value())
        c.set('epg_fuzzy_min_length',
              self.epg_fuzzy_min_length_spin.value())
        c.set('epg_fuzzy_min_gap', self.epg_fuzzy_min_gap_spin.value())

    def accept(self):
        try:
            self._apply()
        except Exception as e:
            warn_box(self, f"Не удалось применить: {e}")
            return
        self.config.save()
        super().accept()


# =====================================================================
# DuplicateFinderDialog
# =====================================================================
class DuplicateFinderDialog(BaseDialog):
    duplicates_removed = pyqtSignal(int)

    def __init__(self, channels: List[ChannelData], parent=None,
                 use_tvg_id: bool = False, keep_duplicates: bool = False):
        super().__init__("Поиск дубликатов", parent, size=(900, 600))
        self.channels = channels
        self.use_tvg_id = use_tvg_id
        self.keep_duplicates = keep_duplicates
        self._setup_ui()
        self._update_stats()

    def _setup_ui(self):
        l = self.root
        self.stats_label = QLabel()
        l.addWidget(self.stats_label)

        opts = QHBoxLayout()
        self.tvg_check = QCheckBox("Учитывать TVG-ID при поиске по названию")
        self.tvg_check.setChecked(self.use_tvg_id)
        self.tvg_check.toggled.connect(self._on_tvg_toggled)
        opts.addWidget(self.tvg_check)
        self.keep_check = QCheckBox("Сохранять дубликаты (только показать)")
        self.keep_check.setChecked(self.keep_duplicates)
        opts.addWidget(self.keep_check)
        opts.addStretch()
        l.addLayout(opts)

        tabs = QTabWidget()

        url_tab = QWidget()
        url_l = QVBoxLayout(url_tab)
        self.url_table = make_table(["Название", "Группа", "URL"])
        url_l.addWidget(self.url_table)
        self.remove_url_btn = QPushButton("Удалить дубликаты по URL")
        self.remove_url_btn.clicked.connect(self._remove_by_url)
        url_l.addWidget(self.remove_url_btn)
        tabs.addTab(url_tab, "По URL")

        name_tab = QWidget()
        name_l = QVBoxLayout(name_tab)
        self.name_table = make_table(["Название", "Группа", "URL"])
        name_l.addWidget(self.name_table)
        self.remove_name_btn = QPushButton("Удалить дубликаты по названию")
        self.remove_name_btn.clicked.connect(self._remove_by_name)
        name_l.addWidget(self.remove_name_btn)
        tabs.addTab(name_tab, "По названию")

        l.addWidget(tabs)
        self.add_close()

    def _on_tvg_toggled(self, checked: bool):
        self.use_tvg_id = checked
        self._update_stats()

    def _update_stats(self):
        report = SimpleDuplicateFinder.get_duplicate_report(
            self.channels, use_tvg_id=self.use_tvg_id)
        self.stats_label.setText(
            f"<b>Всего каналов:</b> {len(self.channels)} | "
            f"<b>Дубликатов по URL:</b> {report['total_url_duplicates']} | "
            f"<b>Дубликатов по названию:</b> "
            f"{report['total_name_duplicates']}")
        self._fill_dup_table(self.url_table, report['by_url'])
        self._fill_dup_table(self.name_table, report['by_name'])

    @staticmethod
    def _fill_dup_table(table: QTableWidget,
                        groups: List[List[ChannelData]]):
        rows_total = sum(len(g) for g in groups)
        displayed = min(rows_total, DUPLICATE_DIALOG_MAX_ROWS)
        table.setRowCount(displayed)
        row = 0
        for group in groups:
            for ch in group:
                if row >= displayed:
                    return
                table.setItem(row, 0, QTableWidgetItem(ch.meta.name or ""))
                table.setItem(row, 1, QTableWidgetItem(ch.meta.group or ""))
                table.setItem(row, 2,
                              QTableWidgetItem((ch.link.url or "")[:80]))
                row += 1

    def _mutate_channels(self, new_list: List[ChannelData]):
        self.channels.clear()
        self.channels.extend(new_list)

    def _remove_by_url(self):
        if not confirm(self,
                       "Удалить дубликаты по URL? Останется первый канал."):
            return
        new_list, removed = SimpleDuplicateFinder.remove_duplicates_by_url(
            self.channels)
        if removed:
            self._mutate_channels(new_list)
        self.duplicates_removed.emit(removed)
        self._update_stats()
        info_box(self, f"Удалено: {removed}", "Готово")

    def _remove_by_name(self):
        if not confirm(self,
                       "Удалить дубликаты по названию? Останется первый канал."):
            return
        new_list, removed = SimpleDuplicateFinder.remove_duplicates_by_name(
            self.channels, use_tvg_id=self.use_tvg_id)
        if removed:
            self._mutate_channels(new_list)
        self.duplicates_removed.emit(removed)
        self._update_stats()
        info_box(self, f"Удалено: {removed}", "Готово")


# =====================================================================
# ComparePlaylistsDialog
# =====================================================================
class ComparePlaylistsDialog(BaseDialog):
    """Построчный diff двух плейлистов (как git diff)."""

    def __init__(self, current_text: str,
                 current_label: str = "current", parent=None):
        super().__init__("Сравнение плейлистов", parent, size=(1100, 700))
        self._current_text = current_text
        self._current_label = current_label
        self._other_text = ""
        self._other_label = "other"
        self._diff: List[DiffLine] = []
        self._setup_ui()
        self._update_stats()

    def _setup_ui(self):
        l = self.root

        top = QHBoxLayout()
        self.load_btn = QPushButton("Загрузить второй плейлист…")
        self.load_btn.clicked.connect(self._load_other)
        top.addWidget(self.load_btn)

        self.file_label = QLabel("Второй файл не загружен")
        self.file_label.setStyleSheet("color: #666;")
        top.addWidget(self.file_label, 1)

        top.addWidget(QLabel("Режим:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Построчный (как git)", "lines")
        self.mode_combo.addItem("По каналам", "channels")
        self.mode_combo.currentIndexChanged.connect(self._recompute)
        top.addWidget(self.mode_combo)

        self.ignore_ws_check = QCheckBox("Игнорировать пробелы")
        self.ignore_ws_check.toggled.connect(self._recompute)
        top.addWidget(self.ignore_ws_check)

        self.export_btn = QPushButton("Экспорт diff…")
        self.export_btn.clicked.connect(self._export_diff)
        self.export_btn.setEnabled(False)
        top.addWidget(self.export_btn)

        l.addLayout(top)

        self.stats_label = QLabel("")
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        l.addWidget(self.stats_label)

        self.unified_check = QCheckBox("Показать unified diff (текстом)")
        self.unified_check.toggled.connect(self._on_unified_toggled)
        l.addWidget(self.unified_check)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            ["№", self._current_label, "№", self._other_label])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)

        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(2, 55)

        mono = QFont("Courier New", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.table.setFont(mono)
        l.addWidget(self.table, 1)

        self.unified_view = QPlainTextEdit()
        self.unified_view.setReadOnly(True)
        self.unified_view.setFont(mono)
        self.unified_view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.NoWrap)
        self.unified_view.setVisible(False)
        l.addWidget(self.unified_view, 1)

        self.add_close()

    def _load_other(self):
        fp = open_file_dialog(self, "Второй плейлист", M3U_FILTER)
        if not fp:
            return
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                self._other_text = f.read()
        except Exception as e:
            error_box(self, str(e))
            return
        self._other_label = os.path.basename(fp)
        self.file_label.setText(f"Второй файл: {self._other_label}")
        self.table.setHorizontalHeaderLabels(
            ["№", self._current_label, "№", self._other_label])
        self.export_btn.setEnabled(True)
        self._recompute()

    def _prepare_lines(self, text: str) -> List[str]:
        lines = normalize_lines(text, self.ignore_ws_check.isChecked())
        if self.mode_combo.currentData() == "channels":
            lines = channels_only(lines)
        return lines

    def _recompute(self):
        if not self._other_text:
            self._diff = []
            self.table.setRowCount(0)
            self._update_stats()
            return
        left = self._prepare_lines(self._current_text)
        right = self._prepare_lines(self._other_text)
        self._diff = diff_lines(left, right)
        self._fill_table()
        self._update_stats()
        if self.unified_check.isChecked():
            self._fill_unified()

    def _fill_table(self):
        self.table.setRowCount(len(self._diff))
        mono = self.table.font()
        for row, d in enumerate(self._diff):
            self._set_row(row, d, mono)

    def _set_row(self, row: int, d: DiffLine, font: QFont):
        left_no = QTableWidgetItem(str(d.left_no) if d.left_no else "")
        left_no.setForeground(_COLOR_LINENO_FG)
        left_no.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                 Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 0, left_no)

        left_item = QTableWidgetItem(d.left)
        left_item.setFont(font)
        self.table.setItem(row, 1, left_item)

        right_no = QTableWidgetItem(str(d.right_no) if d.right_no else "")
        right_no.setForeground(_COLOR_LINENO_FG)
        right_no.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                  Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 2, right_no)

        right_item = QTableWidgetItem(d.right)
        right_item.setFont(font)
        self.table.setItem(row, 3, right_item)

        bg_left = None
        bg_right = None
        if d.op == DiffOp.DELETE:
            bg_left = _COLOR_DEL_BG
        elif d.op == DiffOp.INSERT:
            bg_right = _COLOR_ADD_BG
        elif d.op == DiffOp.REPLACE:
            bg_left = _COLOR_REPL_BG
            bg_right = _COLOR_REPL_BG

        if bg_left is not None:
            left_item.setBackground(QBrush(bg_left))
            left_no.setBackground(QBrush(bg_left))
        if bg_right is not None:
            right_item.setBackground(QBrush(bg_right))
            right_no.setBackground(QBrush(bg_right))

    def _fill_unified(self):
        left = self._prepare_lines(self._current_text)
        right = self._prepare_lines(self._other_text)
        text = to_unified_diff(left, right,
                               left_label=self._current_label,
                               right_label=self._other_label)
        self.unified_view.setPlainText(text or "(нет различий)")

    def _on_unified_toggled(self, checked: bool):
        self.table.setVisible(not checked)
        self.unified_view.setVisible(checked)
        if checked and self._other_text:
            self._fill_unified()

    def _update_stats(self):
        if self._diff:
            st = diff_stats(self._diff)
        else:
            st = {'added': 0, 'removed': 0, 'changed': 0,
                  'equal': 0, 'total': 0}
        self.stats_label.setText(
            f"<b>Добавлено:</b> <span style='color:#1b7e1b'>"
            f"+{st['added']}</span> | "
            f"<b>Удалено:</b> <span style='color:#b02020'>"
            f"-{st['removed']}</span> | "
            f"<b>Изменено:</b> <span style='color:#b08000'>"
            f"~{st['changed']}</span> | "
            f"<b>Без изменений:</b> {st['equal']} | "
            f"<b>Строк diff:</b> {st['total']}")

    def _export_diff(self):
        if not self._diff:
            warn_box(self, "Нет данных для экспорта")
            return
        fp = save_file_dialog(
            self, "Экспорт diff", "playlists.diff",
            "Diff (*.diff *.patch);;Все файлы (*.*)")
        if not fp:
            return
        try:
            left = self._prepare_lines(self._current_text)
            right = self._prepare_lines(self._other_text)
            text = to_unified_diff(left, right,
                                   left_label=self._current_label,
                                   right_label=self._other_label)
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(text or "")
            info_box(self, f"Diff сохранён:\n{fp}", "Экспорт")
        except Exception as e:
            error_box(self, str(e))


# =====================================================================
# BlockDomainDialog, DomainBlacklistDialog
# =====================================================================
class BlockDomainDialog(BaseDialog):
    def __init__(self, value: str, all_channels: List[ChannelData],
                 parent=None):
        super().__init__("Заблокировать домен/IP", parent, size=(720, 460))
        self._all_channels = list(all_channels)
        self._normalized = URLUtils.normalize_host(value)
        self._is_ip = URLUtils.is_ip_address(self._normalized)
        self._current: List[ChannelData] = []

        layout = self.root
        self.header_label = QLabel()
        self.header_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.header_label)

        form = QFormLayout()
        self.value_edit = QLineEdit(self._normalized)
        self.value_edit.setReadOnly(True)
        form.addRow("Домен/IP:", self.value_edit)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Необязательная заметка")
        form.addRow("Заметка:", self.note_edit)
        layout.addLayout(form)

        self.subdomain_check = QCheckBox(
            "Включая поддомены (например, cdn.example.com для example.com)")
        layout.addWidget(self.subdomain_check)

        layout.addWidget(QLabel("Будут очищены ссылки у каналов:"))
        self.preview = make_table(
            ["Название", "Группа", "URL"],
            edit_disabled=True,
            select_mode=QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.preview)

        self.add_ok_cancel("Заблокировать и очистить ссылки")

        if self._is_ip:
            self.subdomain_check.setChecked(False)
            self.subdomain_check.setEnabled(False)
            self.subdomain_check.setToolTip(
                "Для IP-адресов поддомены не применяются")
        else:
            self.subdomain_check.setChecked(True)
        self.subdomain_check.toggled.connect(self._on_toggled)

        self._refresh()

    def _match(self, ch: ChannelData) -> bool:
        if not ch.link.url:
            return False
        if self._is_ip:
            return URLUtils.ip_matches(ch.link.url, self._normalized)
        return URLUtils.host_matches(
            ch.link.url, self._normalized,
            include_subdomains=self.subdomain_check.isChecked())

    def _on_toggled(self, _checked: bool):
        self._refresh()

    def _refresh(self):
        filtered = [ch for ch in self._all_channels if self._match(ch)]
        self._current = filtered
        kind = "IP-адрес" if self._is_ip else "Домен"
        self.header_label.setText(
            f"<b>{kind}:</b> {self._normalized}<br>"
            f"<b>Будет очищено ссылок:</b> {len(filtered)} "
            f"(каналы сохранятся)")
        fill_channels_table(self.preview, filtered, max_url=120)

    def get_result(self) -> Tuple[str, bool, str, List[ChannelData]]:
        include = (False if self._is_ip
                   else self.subdomain_check.isChecked())
        return (self._normalized, include,
                self.note_edit.text().strip(), list(self._current))


class DomainBlacklistDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Менеджер чёрного списка домен/IP",
                         parent, size=(880, 600))
        self.core = core
        self._setup_ui()
        self._load_table()

    def _setup_ui(self):
        l = self.root
        info = QLabel(
            "Единый чёрный список доменов и IP-адресов для ссылок.\n"
            "При срабатывании правила ссылка ОЧИЩАЕТСЯ, но канал "
            "сохраняется вместе с метаданными.")
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        l.addWidget(info)

        self.table = make_table(
            ["Значение", "Тип", "Поддомены", "Заметка", "Дата"],
            stretch_last=False,
            select_mode=QAbstractItemView.SelectionMode.ExtendedSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        l.addWidget(self.table)

        bl = QHBoxLayout()
        for t, s in (("Добавить", self._add_manual),
                     ("Удалить", self._remove_selected),
                     ("Очистить", self._clear_all),
                     ("Экспорт", self._export_bl)):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        l.addLayout(bl)
        self.add_close()

    def _load_table(self):
        items = self.core.get_domain_blacklist()
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            value = it.get('value', '')
            is_ip = bool(it.get('is_ip', False))
            inc = bool(it.get('include_subdomains', True))
            note = it.get('note', '') or ''
            ad = it.get('added_date', '') or ''

            vi = QTableWidgetItem(value)
            vi.setData(Qt.ItemDataRole.UserRole, value)
            self.table.setItem(i, 0, vi)

            type_item = QTableWidgetItem("IP" if is_ip else "Домен")
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_ip:
                type_item.setForeground(QColor("darkblue"))
            self.table.setItem(i, 1, type_item)

            sub_item = QTableWidgetItem(
                "—" if is_ip else ("Да" if inc else "Нет"))
            sub_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, sub_item)

            ni = QTableWidgetItem(note)
            ni.setToolTip(note)
            self.table.setItem(i, 3, ni)

            self.table.setItem(i, 4, QTableWidgetItem(ad))

    def _add_manual(self):
        raw, ok = QInputDialog.getText(
            self, "Добавить правило",
            "Домен или IP (можно вставить URL):",
            QLineEdit.EchoMode.Normal, "")
        if not ok or not raw.strip():
            return
        normalized = URLUtils.normalize_host(raw)
        if not normalized:
            warn_box(self, "Пустое значение")
            return

        all_channels: List[ChannelData] = []
        main_win = self.parent()
        cur_tab = getattr(main_win, 'current_tab', None) if main_win else None
        if cur_tab is not None and _is_qobject_valid(cur_tab):
            all_channels = list(cur_tab.all_channels)
        else:
            for tab in self.core.get_all_tabs():
                if _is_qobject_valid(tab):
                    all_channels = list(tab.all_channels)
                    break

        dlg = BlockDomainDialog(normalized, all_channels, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value, include, note, selected = dlg.get_result()
        if not value:
            return
        if not note:
            note = "Добавлено вручную"
        self.core.add_domain_to_blacklist(
            value, include_subdomains=include, note=note)
        self._load_table()

    def _remove_selected(self):
        rows = sorted([r.row() for r in
                       self.table.selectionModel().selectedRows()],
                      reverse=True)
        if not rows:
            return
        for r in rows:
            it = self.table.item(r, 0)
            if it:
                value = it.data(Qt.ItemDataRole.UserRole) or it.text()
                self.core.remove_domain_from_blacklist(value)
        self._load_table()

    def _clear_all(self):
        if confirm(self, "Очистить чёрный список домен/IP?\n"
                         "Уже очищенные ссылки не восстановятся."):
            self.core.clear_domain_blacklist()
            self._load_table()

    def _export_bl(self):
        items = self.core.get_domain_blacklist()
        if json_export_dialog(self, "Экспорт ч.с. домен/IP",
                              "domain_blacklist.json", items):
            info_box(self, f"Сохранено правил: {len(items)}", "Экспорт")


# =====================================================================
# LinkSourceEditDialog
# =====================================================================
class LinkSourceEditDialog(BaseDialog):
    def __init__(self, parent=None, source: Optional[LinkSource] = None,
                 existing_names: Optional[Set[str]] = None):
        super().__init__("Правка источника" if source
                         else "Добавление источника",
                         parent, size=(500, 600))
        self.source = source
        self.existing_names = existing_names or set()
        self._setup_ui()
        if source:
            self._load_data()

    def _setup_ui(self):
        layout = self.root
        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("Название:", self.name_edit)
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Локальный файл", "Онлайн источник"])
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Тип:", self.type_combo)
        self.path_edit = QLineEdit()
        form.addRow("Путь/URL:", self.path_edit)
        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.clicked.connect(self._browse)
        form.addRow("", self.browse_btn)
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 10)
        self.priority_spin.setValue(5)
        form.addRow("Приоритет (1-10):", self.priority_spin)
        self.encoding_edit = QLineEdit("utf-8")
        form.addRow("Кодировка:", self.encoding_edit)
        layout.addLayout(form)

        g = QGroupBox("Дополнительно")
        gl = QVBoxLayout(g)
        self.enabled_check = QCheckBox("Включен")
        self.enabled_check.setChecked(True)
        gl.addWidget(self.enabled_check)
        self.auto_update_check = QCheckBox("Автоматическое обновление")
        gl.addWidget(self.auto_update_check)
        ul = QHBoxLayout()
        ul.addWidget(QLabel("Интервал (часы):"))
        self.update_interval_spin = QSpinBox()
        self.update_interval_spin.setRange(1, 168)
        self.update_interval_spin.setValue(24)
        ul.addWidget(self.update_interval_spin)
        ul.addStretch()
        gl.addLayout(ul)
        layout.addWidget(g)

        fg = QGroupBox("Фильтрация при загрузке")
        fl = QVBoxLayout(fg)
        self.apply_blacklist_check = QCheckBox(
            "Применять чёрный список каналов")
        self.apply_blacklist_check.setChecked(True)
        fl.addWidget(self.apply_blacklist_check)
        self.apply_domain_blacklist_check = QCheckBox(
            "Очищать ссылки по ЧС домен/IP (каналы сохраняются)")
        self.apply_domain_blacklist_check.setChecked(True)
        fl.addWidget(self.apply_domain_blacklist_check)
        layout.addWidget(fg)

        self.add_ok_cancel()

    def _load_data(self):
        s = self.source
        if not s:
            return
        self.name_edit.setText(s.name)
        self.type_combo.setCurrentIndex(
            0 if s.source_type == "local" else 1)
        self.path_edit.setText(s.path)
        self.priority_spin.setValue(s.priority)
        self.encoding_edit.setText(s.encoding)
        self.enabled_check.setChecked(s.enabled)
        self.auto_update_check.setChecked(s.auto_update)
        self.update_interval_spin.setValue(s.update_interval_hours)
        self.apply_blacklist_check.setChecked(s.apply_blacklist)
        self.apply_domain_blacklist_check.setChecked(
            s.apply_domain_blacklist)
        self._on_type_changed(self.type_combo.currentIndex())

    def _on_type_changed(self, idx: int):
        self.browse_btn.setEnabled(idx == 0)

    def _browse(self):
        fp = open_file_dialog(self, "Файл", M3U_FILTER)
        if fp:
            self.path_edit.setText(fp)

    def _validate(self) -> bool:
        name = self.name_edit.text().strip()
        if not name:
            warn_box(self, "Введите название")
            return False
        if self.source is None and name in self.existing_names:
            warn_box(self, f"Источник '{name}' уже существует")
            return False
        path = self.path_edit.text().strip()
        if not path:
            warn_box(self, "Введите путь/URL")
            return False
        if self.type_combo.currentIndex() == 0:
            if not os.path.exists(path):
                if not confirm(self, "Файл не существует. Продолжить?"):
                    return False
        else:
            p = urlparse(path)
            if p.scheme not in ('http', 'https') or not p.netloc:
                warn_box(self, "URL должен быть http(s)://host/path")
                return False
        return True

    def _create(self) -> LinkSource:
        s = LinkSource() if not self.source else self.source.copy()
        s.name = self.name_edit.text().strip()
        s.source_type = ("local" if self.type_combo.currentIndex() == 0
                         else "online")
        s.path = self.path_edit.text().strip()
        s.priority = self.priority_spin.value()
        s.encoding = self.encoding_edit.text().strip() or "utf-8"
        s.enabled = self.enabled_check.isChecked()
        s.auto_update = self.auto_update_check.isChecked()
        s.update_interval_hours = self.update_interval_spin.value()
        s.apply_blacklist = self.apply_blacklist_check.isChecked()
        s.apply_domain_blacklist = \
            self.apply_domain_blacklist_check.isChecked()
        return s

    def get_source(self) -> Optional[LinkSource]:
        if not self._validate():
            return None
        return self._create()

    def accept(self):
        if self._validate():
            super().accept()


# =====================================================================
# DomainUserAgentEditDialog, DomainUserAgentDialog
# =====================================================================
class DomainUserAgentEditDialog(BaseDialog):
    def __init__(self, parent=None,
                 rule: Optional[DomainUserAgentRule] = None):
        super().__init__("Правило User-Agent" if not rule
                         else "Редактирование",
                         parent, size=(600, 300))
        self.rule = rule
        l = self.root
        form = QFormLayout()
        self.domain_edit = QLineEdit()
        self.domain_edit.setPlaceholderText("example.com или cdn.example.com")
        form.addRow("Домен:", self.domain_edit)
        self.user_agent_edit = QTextEdit()
        self.user_agent_edit.setPlaceholderText(
            'WINK/1.40.1 (AndroidTV/9) HlsWinkPlayer\n\n'
            'Оставьте пустым, чтобы УДАЛИТЬ User-Agent для каналов '
            'с этим доменом')
        self.user_agent_edit.setMaximumHeight(120)
        form.addRow("User-Agent:", self.user_agent_edit)
        l.addLayout(form)
        info = QLabel(
            "Пустое поле UA = удалить User-Agent для всех каналов "
            "с этим доменом.")
        info.setWordWrap(True)
        info.setStyleSheet("color: gray; font-style: italic;")
        l.addWidget(info)
        self.add_ok_cancel()
        if rule:
            self.domain_edit.setText(rule.domain)
            self.user_agent_edit.setPlainText(rule.user_agent)

    def get_rule(self) -> Tuple[str, str]:
        return (self.domain_edit.text().strip(),
                self.user_agent_edit.toPlainText().strip())

    def accept(self):
        if not self.domain_edit.text().strip():
            warn_box(self, "Введите домен")
            return
        super().accept()


class DomainUserAgentDialog(BaseDialog):
    rules_updated = pyqtSignal()

    def __init__(self, manager: DomainUserAgentManager,
                 playlist_tab=None, parent=None):
        super().__init__("Правила User-Agent по доменам",
                         parent, size=(800, 500))
        self.manager = manager
        self._playlist_tab_ref = (weakref.ref(playlist_tab)
                                  if playlist_tab else None)
        self._loading = False
        layout = self.root
        info = QLabel(
            "Автоматическая установка User-Agent для каналов по домену.\n"
            "Пустое поле UA = удалить User-Agent у всех каналов "
            "с этим доменом.")
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        self.rules_table = make_table(
            ["Домен", "User-Agent", "Вкл"],
            stretch_last=False,
            select_mode=QAbstractItemView.SelectionMode.SingleSelection)
        h = self.rules_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.rules_table)

        bl = QHBoxLayout()
        for t, s in (("Добавить", self._add),
                     ("Редактировать", self._edit),
                     ("Удалить", self._remove)):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        bl.addStretch()
        layout.addLayout(bl)

        apply_btn = QPushButton(
            "Применить ко всем каналам текущего плейлиста")
        apply_btn.clicked.connect(self._apply_all)
        layout.addWidget(apply_btn)

        self.add_close()
        self._load_rules()

    def _load_rules(self):
        self._loading = True
        try:
            rules = self.manager.get_all_rules()
            self.rules_table.setRowCount(len(rules))
            for i, r in enumerate(rules):
                di = QTableWidgetItem(r.domain)
                di.setData(Qt.ItemDataRole.UserRole, r.domain)
                self.rules_table.setItem(i, 0, di)
                ua = r.user_agent
                disp = ua[:60] + "..." if len(ua) > 60 else ua
                it = QTableWidgetItem(disp if ua else "(удалить)")
                if not ua:
                    it.setForeground(QColor("orange"))
                it.setToolTip(ua)
                self.rules_table.setItem(i, 1, it)
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                             Qt.ItemFlag.ItemIsEnabled)
                chk.setCheckState(Qt.CheckState.Checked if r.enabled
                                  else Qt.CheckState.Unchecked)
                self.rules_table.setItem(i, 2, chk)
        finally:
            self._loading = False

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != 2:
            return
        di = self.rules_table.item(item.row(), 0)
        if not di:
            return
        domain = di.data(Qt.ItemDataRole.UserRole) or di.text()
        enabled = item.checkState() == Qt.CheckState.Checked
        if self.manager.set_enabled(domain, enabled):
            self.rules_updated.emit()

    def _add(self):
        dlg = DomainUserAgentEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            domain, ua = dlg.get_rule()
            if domain and self.manager.add_rule(domain, ua):
                self._load_rules()
                self.rules_updated.emit()

    def _edit(self):
        row = self.rules_table.currentRow()
        if row < 0:
            return
        di = self.rules_table.item(row, 0)
        if not di:
            return
        domain = di.data(Qt.ItemDataRole.UserRole) or di.text()
        rule = self.manager.get_rule(domain)
        if not rule:
            return
        dlg = DomainUserAgentEditDialog(self, rule)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            nd, nu = dlg.get_rule()
            if nd:
                if nd != rule.domain:
                    self.manager.remove_rule(rule.domain)
                self.manager.add_rule(nd, nu)
                self._load_rules()
                self.rules_updated.emit()

    def _remove(self):
        row = self.rules_table.currentRow()
        if row < 0:
            return
        it = self.rules_table.item(row, 0)
        if not it:
            return
        domain = it.data(Qt.ItemDataRole.UserRole) or it.text()
        if confirm(self, f"Удалить правило для '{domain}'?"):
            if self.manager.remove_rule(domain):
                self._load_rules()
                self.rules_updated.emit()

    def _apply_all(self):
        tab = self._playlist_tab_ref() if self._playlist_tab_ref else None
        if not tab or not _is_qobject_valid(tab):
            warn_box(self, "Нет открытого плейлиста")
            return
        if not confirm(self, "Применить правила ко всем каналам?"):
            return
        tab.flush_pending_state()
        modified = self.manager.apply_rules_to_channels(tab.all_channels)
        if modified > 0:
            tab.save_state(f"Применение UA-правил ({modified})")
            tab.flush_pending_state()
            tab.sync_to_core()
            tab.refresh_view()
            info_box(self, f"Обновлено {modified} каналов", "Успех")
        else:
            info_box(self, "Изменений не требуется")


# =====================================================================
# SourcesFinderDialog
# =====================================================================
class SourcesFinderDialog(BaseDialog):
    """Диалог авто-поиска источников."""

    def __init__(self, parent=None):
        super().__init__("🔍 Поиск источников автоматически",
                         parent, size=(760, 560))
        self._worker = None
        self._found: List[Dict[str, Any]] = []
        self._worker_cls = None
        try:
            from generator import SourcesFinderWorker
            self._worker_cls = SourcesFinderWorker
        except ImportError as e:
            logger.warning(f"generator недоступен: {e}")
        self._setup_ui()

    def _setup_ui(self):
        l = self.root
        info = QLabel(
            "Автоматический поиск URL-ов плейлистов в интернете.\n"
            "Найденные источники будут добавлены в менеджер источников\n"
            "с флагом «Автообновление» и сразу загружены в кэш.")
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        l.addWidget(info)

        g = QGroupBox("Что искать")
        gl = QVBoxLayout(g)
        self.github_check = QCheckBox(
            "GitHub (известные репозитории + поиск)")
        self.github_check.setChecked(True)
        gl.addWidget(self.github_check)
        self.m3uguide_check = QCheckBox("m3u.guide")
        self.m3uguide_check.setChecked(True)
        gl.addWidget(self.m3uguide_check)
        self.fast_check = QCheckBox(
            "FAST-каналы (Pluto, Samsung, Plex, ...)")
        self.fast_check.setChecked(True)
        gl.addWidget(self.fast_check)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Макс. GitHub:"))
        self.max_github_spin = QSpinBox()
        self.max_github_spin.setRange(1, 100)
        self.max_github_spin.setValue(SOURCES_FINDER_MAX_GITHUB_DEFAULT)
        opts.addWidget(self.max_github_spin)
        opts.addWidget(QLabel("Макс. m3u.guide:"))
        self.max_m3uguide_spin = QSpinBox()
        self.max_m3uguide_spin.setRange(1, 50)
        self.max_m3uguide_spin.setValue(SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT)
        opts.addWidget(self.max_m3uguide_spin)
        opts.addStretch()
        gl.addLayout(opts)
        l.addWidget(g)

        self.progress_label = QLabel("Готов к поиску")
        self.progress_label.setStyleSheet("color: #555;")
        l.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        l.addWidget(self.progress_bar)

        l.addWidget(QLabel("Найденные источники:"))
        self.table = make_table(
            ["✓", "Название", "Тип", "URL"],
            stretch_last=False,
            select_rows=False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        l.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        self.search_btn = QPushButton("🔍 Начать поиск")
        self.search_btn.clicked.connect(self._start_search)
        self.search_btn.setEnabled(self._worker_cls is not None)
        btn_row.addWidget(self.search_btn)

        self.stop_btn = QPushButton("⏹ Остановить")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_search)
        btn_row.addWidget(self.stop_btn)

        self.select_all_btn = QPushButton("Выбрать все")
        self.select_all_btn.clicked.connect(lambda: self._set_all(True))
        btn_row.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Снять все")
        self.deselect_all_btn.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addStretch()
        l.addLayout(btn_row)

        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Добавить выбранные и загрузить")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

        if self._worker_cls is None:
            self.progress_label.setText(
                "⚠️ Модуль generator.py не найден — поиск недоступен")

    def _set_all(self, checked: bool):
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 0)
            if it is not None:
                it.setCheckState(
                    Qt.CheckState.Checked if checked
                    else Qt.CheckState.Unchecked)

    def _start_search(self):
        if self._worker_cls is None:
            warn_box(self, "Модуль generator.py не найден")
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._found = []
        self.table.setRowCount(0)
        self.progress_label.setText("⏳ Поиск...")
        self.progress_bar.setVisible(True)
        self.search_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._worker = self._worker_cls(
            include_github=self.github_check.isChecked(),
            include_m3uguide=self.m3uguide_check.isChecked(),
            include_fast=self.fast_check.isChecked(),
            max_github=self.max_github_spin.value(),
            max_m3uguide=self.max_m3uguide_spin.value(),
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.found.connect(self._on_found)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop_search(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()

    def _on_progress(self, msg: str):
        self.progress_label.setText(msg)

    def _on_found(self, items: list):
        self._found = list(items)
        self._fill_table()

    def _on_finished(self, items: list):
        self._found = list(items)
        self._fill_table()
        self.progress_bar.setVisible(False)
        self.search_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_label.setText(
            f"✓ Найдено источников: {len(self._found)}")

    def _on_error(self, msg: str):
        self.progress_bar.setVisible(False)
        self.search_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_label.setText(f"✗ Ошибка: {msg}")
        error_box(self, msg, "Ошибка поиска")

    def _fill_table(self):
        self.table.setRowCount(len(self._found))
        for i, item in enumerate(self._found):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                         Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, chk)

            name_item = QTableWidgetItem(item.get('name') or '')
            name_item.setData(Qt.ItemDataRole.UserRole, item)
            self.table.setItem(i, 1, name_item)

            type_item = QTableWidgetItem(item.get('source') or '')
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, type_item)

            url_item = QTableWidgetItem(item.get('url') or '')
            url_item.setToolTip(item.get('url') or '')
            self.table.setItem(i, 3, url_item)

    def get_selected_items(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if chk is None or chk.checkState() != Qt.CheckState.Checked:
                continue
            name_item = self.table.item(row, 1)
            if name_item is None:
                continue
            data = name_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                result.append(data)
        return result

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            with suppress(Exception):
                self._worker.stop()
            self._worker.wait(3000)
        self._worker = None
        super().closeEvent(event)


# =====================================================================
# LinkSourceManagerDialog
# =====================================================================
class LinkSourceManagerDialog(BaseDialog):
    """Менеджер источников: «🔄 Обновить всё» + «🔍 Найти источники»."""

    sources_updated = pyqtSignal()

    def __init__(self, source_manager: LinkSourceManager,
                 config: Config, parent=None):
        super().__init__("Менеджер источников", parent, size=(1150, 680))
        self.source_manager = source_manager
        self.config = config
        self._refresh_worker: Optional[SourcesRefreshWorker] = None
        self._loading = False
        self._channel_emit_counter = 0
        self._setup_ui()
        self._load_sources()

    def _setup_ui(self):
        layout = self.root
        info_label = QLabel(
            "Обновление источника загружает каналы и проверяет все URL.\n"
            "Живые URL попадают в кэш url_status_cache, который используют\n"
            "автозамена и точечная проверка «Ссылки → Проверить все ссылки».\n"
            "Если источников нет — нажмите «🔍 Найти источники автоматически».")
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #555; font-style: italic;")
        layout.addWidget(info_label)

        self.sources_table = make_table(
            ["Вкл", "Название", "Тип", "Путь/URL", "Приоритет",
             "Авто", "Обновлено", "Всего", "Рабочих"],
            stretch_last=False,
            select_mode=QAbstractItemView.SelectionMode.SingleSelection)
        self.sources_table.setSortingEnabled(True)
        self.sources_table.itemChanged.connect(self._on_item_changed)
        h = self.sources_table.horizontalHeader()
        for i in range(9):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.sources_table)

        bl1 = QHBoxLayout()
        for t, s in (("Добавить", self._add),
                     ("Редактировать", self._edit),
                     ("Удалить", self._remove),
                     ("Импорт", self._import),
                     ("Экспорт", self._export)):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl1.addWidget(b)
        bl1.addStretch()
        layout.addLayout(bl1)

        bl2 = QHBoxLayout()
        self.auto_find_btn = QPushButton("🔍 Найти источники автоматически")
        self.auto_find_btn.setToolTip(
            "Найти публичные плейлисты в интернете "
            "(GitHub, m3u.guide, FAST)\n"
            "и добавить их как источники с автообновлением.")
        self.auto_find_btn.clicked.connect(self._auto_find_sources)
        bl2.addWidget(self.auto_find_btn)

        self.remove_generated_btn = QPushButton("🧹 Удалить найденные")
        self.remove_generated_btn.setToolTip(
            "Удалить все источники, добавленные авто-поиском.")
        self.remove_generated_btn.clicked.connect(self._remove_generated)
        bl2.addWidget(self.remove_generated_btn)

        bl2.addStretch()

        self.refresh_all_btn = QPushButton("🔄 Обновить всё")
        self.refresh_all_btn.clicked.connect(self._refresh_all)
        bl2.addWidget(self.refresh_all_btn)
        layout.addLayout(bl2)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #555;")
        layout.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        self._progress_bar.setTextVisible(True)
        layout.addWidget(self._progress_bar)

        self.add_close()

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != 0:
            return
        name_item = self.sources_table.item(item.row(), 1)
        if not name_item:
            return
        src = self.source_manager.get_source_by_name(name_item.text())
        if not src:
            return
        src.enabled = item.checkState() == Qt.CheckState.Checked
        self.source_manager._persist()
        self.sources_updated.emit()

    def _load_sources(self):
        self._loading = True
        try:
            self.sources_table.setSortingEnabled(False)
            sources = self.source_manager.get_all_sources()
            self.sources_table.setRowCount(len(sources))
            for i, s in enumerate(sources):
                ei = QTableWidgetItem()
                ei.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                            Qt.ItemFlag.ItemIsEnabled)
                ei.setCheckState(Qt.CheckState.Checked if s.enabled
                                 else Qt.CheckState.Unchecked)
                self.sources_table.setItem(i, 0, ei)

                name_text = s.name
                if s.generated:
                    name_text = f"✨ {name_text}"
                ni = QTableWidgetItem(name_text)
                ni.setData(Qt.ItemDataRole.UserRole, s.name)
                if s.generated:
                    ni.setToolTip("Источник добавлен авто-поиском")
                self.sources_table.setItem(i, 1, ni)

                self.sources_table.setItem(i, 2, QTableWidgetItem(
                    "Локальный" if s.source_type == "local" else "Онлайн"))

                pi = QTableWidgetItem(s.path)
                pi.setToolTip(s.path)
                self.sources_table.setItem(i, 3, pi)

                pr_item = QTableWidgetItem(str(s.priority))
                pr_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.sources_table.setItem(i, 4, pr_item)

                self.sources_table.setItem(i, 5, QTableWidgetItem(
                    "Да" if s.auto_update else "Нет"))

                dt = (s.last_updated.strftime("%Y-%m-%d %H:%M")
                      if s.last_updated else "Никогда")
                self.sources_table.setItem(i, 6, QTableWidgetItem(dt))

                raw_total = (s.raw_total_links
                             if s.raw_total_links else s.total_links)
                self.sources_table.setItem(i, 7, _NumericItem(raw_total))

                wrk = _NumericItem(int(s.total_working or 0))
                if s.working_checked_at:
                    ts = datetime.fromtimestamp(s.working_checked_at)
                    wrk.setToolTip(
                        f"Проверено: {ts.strftime('%Y-%m-%d %H:%M')}\n"
                        f"Рабочих: {s.total_working}\n"
                        f"Всего (после ЧС): {s.total_links}")
                    if s.total_working > 0:
                        wrk.setForeground(QColor("green"))
                else:
                    wrk.setToolTip("Не проверялось")
                    wrk.setForeground(QColor("gray"))
                self.sources_table.setItem(i, 8, wrk)
        finally:
            self.sources_table.setSortingEnabled(True)
            self._loading = False

    def _existing_names(self) -> Set[str]:
        return {s.name for s in self.source_manager.get_all_sources()}

    def _get_current_source_by_row(self, row: int) -> Optional[LinkSource]:
        if row < 0:
            return None
        name_item = self.sources_table.item(row, 1)
        if not name_item:
            return None
        raw = name_item.data(Qt.ItemDataRole.UserRole)
        name = raw if isinstance(raw, str) else name_item.text()
        if name.startswith("✨ "):
            name = name[2:]
        return self.source_manager.get_source_by_name(name)

    def _add(self):
        dlg = LinkSourceEditDialog(self,
                                   existing_names=self._existing_names())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src = dlg.get_source()
            if src and self.source_manager.add_source(src):
                self._load_sources()
                self.sources_updated.emit()

    def _edit(self):
        row = self.sources_table.currentRow()
        if row < 0:
            return
        src = self._get_current_source_by_row(row)
        if not src:
            return
        dlg = LinkSourceEditDialog(self, src)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ns = dlg.get_source()
            if ns and self.source_manager.update_source(src.name, ns):
                self._load_sources()
                self.sources_updated.emit()

    def _remove(self):
        row = self.sources_table.currentRow()
        if row < 0:
            return
        src = self._get_current_source_by_row(row)
        if not src:
            return
        if confirm(self, f"Удалить '{src.name}'?"):
            if self.source_manager.remove_source(src.name):
                self._load_sources()
                self.sources_updated.emit()

    def _auto_find_sources(self):
        dlg = SourcesFinderDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        items = dlg.get_selected_items()
        if not items:
            info_box(self, "Ничего не выбрано")
            return

        existing_paths = {s.path for s in self.source_manager.get_all_sources()}
        existing_names = {s.name for s in self.source_manager.get_all_sources()}

        added = 0
        skipped = 0
        for item in items:
            url = (item.get('url') or '').strip()
            if not url or url in existing_paths:
                skipped += 1
                continue
            base_name = item.get('name') or url
            name = base_name
            suffix = 2
            while name in existing_names:
                name = f"{base_name} #{suffix}"
                suffix += 1

            src = LinkSource()
            src.name = name
            src.path = url
            src.source_type = "online"
            src.priority = int(item.get('priority') or 5)
            src.enabled = True
            src.auto_update = True
            src.update_interval_hours = SOURCES_FINDER_UPDATE_INTERVAL_HOURS
            src.encoding = "utf-8"
            src.apply_blacklist = True
            src.apply_domain_blacklist = True
            src.generated = True

            if self.source_manager.add_source(src):
                added += 1
                existing_paths.add(url)
                existing_names.add(name)

        self._load_sources()
        self.sources_updated.emit()

        if added == 0:
            info_box(self,
                     f"Новых источников не добавлено "
                     f"(пропущено: {skipped}).")
            return

        auto_load = bool(self.config.get(
            'sources_finder_auto_load_after_add', True))
        if auto_load or confirm(
                self,
                f"Добавлено источников: {added}.\n"
                f"Пропущено (уже есть): {skipped}.\n\n"
                f"Сразу загрузить их в кэш?"):
            self._refresh_all()
        else:
            info_box(self,
                     f"Источники добавлены в менеджер.\n"
                     f"Загрузите их кнопкой «🔄 Обновить всё».",
                     "Готово")

    def _remove_generated(self):
        generated = [s for s in self.source_manager.get_all_sources()
                     if s.generated]
        if not generated:
            info_box(self, "Нет источников, добавленных авто-поиском.")
            return
        if not confirm(
                self,
                f"Удалить {len(generated)} источников, "
                f"добавленных авто-поиском?"):
            return
        n = self.source_manager.remove_generated_sources()
        self._load_sources()
        self.sources_updated.emit()
        info_box(self, f"Удалено: {n}", "Готово")

    def _refresh_all(self):
        self._channel_emit_counter = 0
        sources = self.source_manager.get_enabled_sources()
        if not sources:
            info_box(self, "Нет включённых источников")
            return
        if self._refresh_worker and self._refresh_worker.isRunning():
            info_box(self, "Уже идёт")
            return
        self._refresh_worker = SourcesRefreshWorker(
            sources, self.source_manager, self.config,
            check_urls=True)

        def on_progress(value, total, text):
            v = max(0, min(100, int(value)))
            self._progress_bar.setValue(v)
            self._progress_bar.setFormat(f"{text} (%p%)")
            self._progress_bar.setVisible(True)
            self._progress_label.setText(text)

        def on_source_checked(name, working, total):
            self._progress_label.setText(
                f"✓ Проверено [{name}]: {working}/{total}")

        def on_channel_checked(source, name, ok, msg):
            mark = "✓" if ok else "✗"
            short = (name or "")[:60]
            self._progress_label.setText(f"{mark} [{source[:20]}] {short}")
            self._channel_emit_counter += 1

        def on_done(success, total):
            self._load_sources()
            self.sources_updated.emit()
            self._progress_bar.setValue(100)
            self._progress_bar.setFormat(
                f"✓ Обновлено {success} из {total} (100%)")
            self._progress_label.setText(
                f"✓ Обновлено {success} из {total}")
            info_box(self, f"Обновлено {success} из {total}", "Успех")
            self._progress_bar.setVisible(False)
            w = self._refresh_worker
            self._refresh_worker = None
            if w is not None:
                with suppress(Exception):
                    w.deleteLater()

        def on_error(msg):
            error_box(self, msg)
            self._progress_label.setText(f"✗ Ошибка: {msg[:80]}")
            self._progress_bar.setVisible(False)
            self._refresh_worker = None

        self._refresh_worker.progress.connect(on_progress)
        self._refresh_worker.source_checked.connect(on_source_checked)
        self._refresh_worker.channel_checked.connect(on_channel_checked)
        self._refresh_worker.all_done.connect(on_done)
        self._refresh_worker.error.connect(on_error)
        self._progress_label.setText("⏳ Обновление...")
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("Подготовка... %p%")
        self._progress_bar.setVisible(True)
        self._refresh_worker.start()

    def _import(self):
        def on_items(data: list) -> int:
            imported = 0
            for item in data:
                if not isinstance(item, dict):
                    continue
                src = LinkSource.from_dict(item)
                if src.name and self.source_manager.add_source(src):
                    imported += 1
            self._load_sources()
            self.sources_updated.emit()
            return imported
        n = json_import_dialog(self, "Импорт источников", on_items)
        if n is not None:
            info_box(self, f"Импортировано {n}", "Успех")

    def _export(self):
        sources = self.source_manager.get_all_sources()
        if json_export_dialog(self, "Экспорт источников",
                              "link_sources.json",
                              [s.to_dict() for s in sources]):
            info_box(self, f"Экспортировано {len(sources)}", "Успех")

    def closeEvent(self, event):
        if self._refresh_worker and self._refresh_worker.isRunning():
            with suppress(Exception):
                self._refresh_worker.stop()
            self._refresh_worker.wait(10000)
        self._refresh_worker = None
        event.accept()


# =====================================================================
# LinkSelectionDialog, BlacklistDialog, PlaylistFromSourcesDialog
# =====================================================================
class LinkSelectionDialog(BaseDialog):
    def __init__(self, channel_name: str, alts: List[ChannelData],
                 parent=None):
        super().__init__(f"Выбор ссылки для '{channel_name}'",
                         parent, size=(900, 500))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.alts = alts
        self.selected_index = -1
        l = self.root
        l.addWidget(QLabel(f"Найдено {len(alts)} ссылок в разных источниках. "
                           f"Выберите нужную:"))
        self.table = make_table(
            ["Источник", "Ссылка", "Приоритет", "Выбрать"],
            stretch_last=False,
            select_mode=QAbstractItemView.SelectionMode.SingleSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setRowCount(len(alts))
        from ksenia_window import ApplicationCore
        core = ApplicationCore.instance()
        for i, a in enumerate(alts):
            self.table.setItem(i, 0,
                               QTableWidgetItem(a.link.link_source or "?"))
            ui = QTableWidgetItem(a.link.url)
            ui.setToolTip(a.link.url)
            self.table.setItem(i, 1, ui)
            src = core.link_source_manager.get_source_by_name(
                a.link.link_source)
            pr = src.priority if src else 5
            pi = QTableWidgetItem(str(pr))
            pi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, pi)
            btn = QPushButton("Выбрать")
            valid = a.has_valid_url
            btn.setEnabled(valid)
            if not valid:
                btn.setToolTip("Пустой URL")
            btn.clicked.connect(lambda checked, idx=i: self._choose(idx))
            self.table.setCellWidget(i, 3, btn)
        l.addWidget(self.table)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

    def _choose(self, idx: int):
        if 0 <= idx < len(self.alts):
            url = self.alts[idx].link.url
            if not url or not url.strip():
                warn_box(self, "Пустой URL нельзя выбрать")
                return
        self.selected_index = idx
        self.accept()

    def get_selected(self) -> Optional[ChannelData]:
        if 0 <= self.selected_index < len(self.alts):
            return self.alts[self.selected_index]
        return None


class BlacklistDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Чёрный список каналов", parent, size=(800, 600))
        self.core = core
        self._setup_ui()
        self._load_table()

    def _setup_ui(self):
        l = self.root
        info = QLabel("Блокировка каналов по имени или TVG-ID.\n"
                      "При срабатывании канал УДАЛЯЕТСЯ из плейлиста.")
        info.setStyleSheet("color: gray;")
        info.setWordWrap(True)
        l.addWidget(info)
        self.table = make_table(
            ["Название", "TVG-ID", "Дата"],
            stretch_last=False,
            select_mode=QAbstractItemView.SelectionMode.ExtendedSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        l.addWidget(self.table)

        bl = QHBoxLayout()
        for t, s in (("Добавить", self._add_manual),
                     ("Удалить", self._remove_selected),
                     ("Очистить", self._clear_all),
                     ("Импорт", self._import_bl),
                     ("Экспорт", self._export_bl)):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        l.addLayout(bl)
        self.add_close()

    def _load_table(self):
        items = self.core.get_blacklist()
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            self.table.setItem(i, 0, QTableWidgetItem(it.get('name', '')))
            self.table.setItem(i, 1, QTableWidgetItem(it.get('tvg_id', '')))
            self.table.setItem(i, 2,
                               QTableWidgetItem(it.get('added_date', '')))

    def _add_manual(self):
        name, ok1 = QInputDialog.getText(self, "Добавить", "Название:")
        if ok1 and name:
            tid, _ = QInputDialog.getText(self, "Добавить", "TVG-ID:")
            if self.core.add_to_blacklist(name.strip(), tid.strip()):
                self._load_table()

    def _remove_selected(self):
        rows = sorted([r.row() for r in
                       self.table.selectionModel().selectedRows()],
                      reverse=True)
        for r in rows:
            n = self.table.item(r, 0)
            t = self.table.item(r, 1)
            if n and t:
                self.core.remove_from_blacklist(n.text(), t.text())
        self._load_table()

    def _clear_all(self):
        if confirm(self, "Очистить?"):
            self.core.clear_blacklist()
            self._load_table()

    def _import_bl(self):
        def on_items(data: list) -> int:
            for it in data:
                if isinstance(it, dict):
                    self.core.add_to_blacklist(
                        it.get('name', ''), it.get('tvg_id', ''))
            self._load_table()
            return len(data)
        json_import_dialog(self, "Импорт ч.с. каналов", on_items)

    def _export_bl(self):
        json_export_dialog(self, "Экспорт ч.с. каналов",
                           "blacklist.json", self.core.get_blacklist())


class PlaylistFromSourcesDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Создать плейлист из источников",
                         parent, size=(700, 560))
        self.core = core
        self._sources_snapshot: List[LinkSource] = []
        self._setup_ui()
        self._load_sources()

    def _setup_ui(self):
        layout = self.root
        layout.addWidget(QLabel("Выберите источники для нового плейлиста:"))

        self.sources_table = make_table(
            ["Вкл", "Название", "Путь/URL"],
            stretch_last=False,
            select_rows=False)
        self.sources_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        h = self.sources_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.sources_table.verticalHeader().setVisible(False)
        layout.addWidget(self.sources_table)

        options_group = QGroupBox("Параметры")
        options_layout = QFormLayout(options_group)

        self.minimize_check = QCheckBox(
            "Объединять каналы по нормализованному имени")
        self.minimize_check.setChecked(True)
        options_layout.addRow(self.minimize_check)

        self.preserve_order_check = QCheckBox(
            "Сохранять порядок из источников (по приоритету)")
        self.preserve_order_check.setChecked(True)
        options_layout.addRow(self.preserve_order_check)

        self.apply_ua_check = QCheckBox(
            "Применять правила User-Agent по доменам")
        self.apply_ua_check.setChecked(True)
        options_layout.addRow(self.apply_ua_check)

        self.apply_bl_check = QCheckBox(
            "Применять чёрный список каналов (УДАЛЯЕТ)")
        self.apply_bl_check.setChecked(True)
        options_layout.addRow(self.apply_bl_check)

        self.apply_domain_bl_check = QCheckBox(
            "Применять ЧС домен/IP (ОЧИЩАЕТ ссылки, каналы сохраняются)")
        self.apply_domain_bl_check.setChecked(True)
        options_layout.addRow(self.apply_domain_bl_check)

        self.apply_epg_check = QCheckBox(
            "Применять метаданные из EPG (если загружен)")
        self.apply_epg_check.setChecked(True)
        options_layout.addRow(self.apply_epg_check)

        layout.addWidget(options_group)

        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _load_sources(self):
        self._sources_snapshot = list(self.core.get_sources())
        self.sources_table.setRowCount(len(self._sources_snapshot))
        for i, s in enumerate(self._sources_snapshot):
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                              Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(Qt.CheckState.Checked if s.enabled
                                   else Qt.CheckState.Unchecked)
            self.sources_table.setItem(i, 0, chk_item)
            self.sources_table.setItem(i, 1, QTableWidgetItem(s.name))
            self.sources_table.setItem(i, 2, QTableWidgetItem(s.path))

    def _selected_sources(self) -> List[LinkSource]:
        result = []
        for i, s in enumerate(self._sources_snapshot):
            item = self.sources_table.item(i, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                result.append(s)
        return result

    def get_result(self) -> Tuple[List[LinkSource], bool, bool, bool, bool,
                                   bool, bool]:
        return (self._selected_sources(),
                self.minimize_check.isChecked(),
                self.preserve_order_check.isChecked(),
                self.apply_ua_check.isChecked(),
                self.apply_bl_check.isChecked(),
                self.apply_domain_bl_check.isChecked(),
                self.apply_epg_check.isChecked())

    def accept(self):
        if not self._selected_sources():
            warn_box(self, "Выберите хотя бы один источник")
            return
        super().accept()


# =====================================================================
# GeneralSettingsDialog, CacheManagerDialog
# =====================================================================
class GeneralSettingsDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Общие настройки", parent, size=(520, 700))
        self.core = core
        l = self.root
        form = QFormLayout()
        c = core.config

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 24)
        self.font_size_spin.setValue(int(c.get('cell_font_size', 10)))
        form.addRow("Размер шрифта:", self.font_size_spin)

        self.save_extvlcopt_check = QCheckBox("Сохранять EXTVLCOPT-строки")
        self.save_extvlcopt_check.setChecked(
            bool(c.get('save_extvlcopt', True)))
        form.addRow(self.save_extvlcopt_check)

        self.limit_check_check = QCheckBox("Ограничивать число проверок")
        self.limit_check_check.setChecked(
            bool(c.get('limit_check_enabled', False)))
        form.addRow(self.limit_check_check)

        self.max_channels_spin = QSpinBox()
        self.max_channels_spin.setRange(10, 100000)
        self.max_channels_spin.setValue(
            int(c.get('max_channels_to_check', 2000)))
        form.addRow("Макс. каналов для проверки:", self.max_channels_spin)

        self.keep_dup_check = QCheckBox(
            "Сохранять дубликаты при дедупликации")
        self.keep_dup_check.setChecked(bool(c.get('keep_duplicates', False)))
        form.addRow(self.keep_dup_check)

        self.dedup_tvg_check = QCheckBox(
            "Учитывать TVG-ID при дедупликации по названию")
        self.dedup_tvg_check.setChecked(
            bool(c.get('dedup_by_name_use_tvg', False)))
        form.addRow(self.dedup_tvg_check)

        self.apply_filters_on_open_check = QCheckBox(
            "Применять ЧС каналов и ЧС домен/IP при открытии файла")
        self.apply_filters_on_open_check.setChecked(
            bool(c.get('apply_filters_on_file_open', True)))
        self.apply_filters_on_open_check.setToolTip(
            "При открытии файла:\n"
            "  • ЧС каналов удаляет каналы,\n"
            "  • ЧС домен/IP очищает ссылки, канал остаётся.")
        form.addRow(self.apply_filters_on_open_check)

        self.epg_overwrite_check = QCheckBox(
            "Перезаписывать существующие метаданные из EPG")
        self.epg_overwrite_check.setChecked(
            bool(c.get('epg_overwrite_metadata', True)))
        form.addRow(self.epg_overwrite_check)

        self.preserve_order_check = QCheckBox(
            "Сохранять оригинальный порядок каналов")
        self.preserve_order_check.setChecked(
            bool(c.get('preserve_original_order', True)))
        form.addRow(self.preserve_order_check)

        self.show_tvg_id_check = QCheckBox("Показывать TVG-ID")
        self.show_tvg_id_check.setChecked(bool(c.get('show_tvg_id', True)))
        form.addRow(self.show_tvg_id_check)

        self.show_tvg_logo_check = QCheckBox("Показывать логотип")
        self.show_tvg_logo_check.setChecked(
            bool(c.get('show_tvg_logo', True)))
        form.addRow(self.show_tvg_logo_check)

        self.show_catchup_check = QCheckBox("Показывать catchup")
        self.show_catchup_check.setChecked(
            bool(c.get('show_catchup', False)))
        form.addRow(self.show_catchup_check)

        self.show_status_bar_check = QCheckBox("Показывать статус-бар")
        self.show_status_bar_check.setChecked(
            bool(c.get('show_status_bar', True)))
        form.addRow(self.show_status_bar_check)

        self.enable_icons_check = QCheckBox("Показывать иконки")
        self.enable_icons_check.setChecked(
            bool(c.get('enable_icons', True)))
        form.addRow(self.enable_icons_check)

        self.auto_update_sources_check = QCheckBox(
            "Автоматически обновлять источники")
        self.auto_update_sources_check.setChecked(
            bool(c.get('auto_update_sources', True)))
        form.addRow(self.auto_update_sources_check)

        self.auto_backup_check = QCheckBox(
            "Автобэкап перед сохранением (.bak)")
        self.auto_backup_check.setChecked(
            bool(c.get('auto_backup_before_save', True)))
        form.addRow(self.auto_backup_check)

        self.days_to_check_spin = QSpinBox()
        self.days_to_check_spin.setRange(0, 30)
        self.days_to_check_spin.setValue(
            int(c.get('days_to_check', FALLBACK_DAYS_DEFAULT)))
        form.addRow("Дней для fallback:", self.days_to_check_spin)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        form.addRow(sep)

        form.addRow(QLabel("<b>Авто-поиск источников:</b>"))

        self.sf_bootstrap_check = QCheckBox(
            "Добавлять стартовый набор источников при первом запуске")
        self.sf_bootstrap_check.setChecked(
            bool(c.get('sources_finder_bootstrap_defaults_on_empty', True)))
        self.sf_bootstrap_check.setToolTip(
            "Только при пустом link_sources.json:\n"
            "SlyNet, iptv-org, Free-TV, Spirt007, smolnp")
        form.addRow(self.sf_bootstrap_check)

        self.sf_auto_load_check = QCheckBox(
            "Сразу загружать в кэш после авто-поиска")
        self.sf_auto_load_check.setChecked(
            bool(c.get('sources_finder_auto_load_after_add', True)))
        form.addRow(self.sf_auto_load_check)

        self.sf_github_check = QCheckBox("Включать GitHub в авто-поиск")
        self.sf_github_check.setChecked(
            bool(c.get('sources_finder_include_github', True)))
        form.addRow(self.sf_github_check)

        self.sf_m3uguide_check = QCheckBox("Включать m3u.guide в авто-поиск")
        self.sf_m3uguide_check.setChecked(
            bool(c.get('sources_finder_include_m3uguide', True)))
        form.addRow(self.sf_m3uguide_check)

        self.sf_fast_check = QCheckBox("Включать FAST в авто-поиск")
        self.sf_fast_check.setChecked(
            bool(c.get('sources_finder_include_fast', True)))
        form.addRow(self.sf_fast_check)

        self.sf_max_github_spin = QSpinBox()
        self.sf_max_github_spin.setRange(1, 100)
        self.sf_max_github_spin.setValue(
            int(c.get('sources_finder_max_github',
                      SOURCES_FINDER_MAX_GITHUB_DEFAULT)))
        form.addRow("Макс. GitHub-источников:", self.sf_max_github_spin)

        self.sf_max_m3uguide_spin = QSpinBox()
        self.sf_max_m3uguide_spin.setRange(1, 50)
        self.sf_max_m3uguide_spin.setValue(
            int(c.get('sources_finder_max_m3uguide',
                      SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT)))
        form.addRow("Макс. m3u.guide-источников:",
                    self.sf_max_m3uguide_spin)

        self.sf_update_interval_spin = QSpinBox()
        self.sf_update_interval_spin.setRange(1, 720)
        self.sf_update_interval_spin.setSuffix(" ч")
        self.sf_update_interval_spin.setValue(
            int(c.get('sources_finder_update_interval_hours',
                      SOURCES_FINDER_UPDATE_INTERVAL_HOURS)))
        form.addRow("Интервал авто-обновления:", self.sf_update_interval_spin)

        l.addLayout(form)
        self.add_ok_cancel()

    def apply(self):
        c = self.core.config
        c.update({
            'cell_font_size': self.font_size_spin.value(),
            'save_extvlcopt': self.save_extvlcopt_check.isChecked(),
            'limit_check_enabled': self.limit_check_check.isChecked(),
            'max_channels_to_check': self.max_channels_spin.value(),
            'keep_duplicates': self.keep_dup_check.isChecked(),
            'dedup_by_name_use_tvg': self.dedup_tvg_check.isChecked(),
            'apply_filters_on_file_open':
                self.apply_filters_on_open_check.isChecked(),
            'epg_overwrite_metadata': self.epg_overwrite_check.isChecked(),
            'preserve_original_order': self.preserve_order_check.isChecked(),
            'show_tvg_id': self.show_tvg_id_check.isChecked(),
            'show_tvg_logo': self.show_tvg_logo_check.isChecked(),
            'show_catchup': self.show_catchup_check.isChecked(),
            'show_status_bar': self.show_status_bar_check.isChecked(),
            'enable_icons': self.enable_icons_check.isChecked(),
            'auto_update_sources': self.auto_update_sources_check.isChecked(),
            'auto_backup_before_save': self.auto_backup_check.isChecked(),
            'days_to_check': self.days_to_check_spin.value(),
            'sources_finder_bootstrap_defaults_on_empty':
                self.sf_bootstrap_check.isChecked(),
            'sources_finder_auto_load_after_add':
                self.sf_auto_load_check.isChecked(),
            'sources_finder_include_github':
                self.sf_github_check.isChecked(),
            'sources_finder_include_m3uguide':
                self.sf_m3uguide_check.isChecked(),
            'sources_finder_include_fast':
                self.sf_fast_check.isChecked(),
            'sources_finder_max_github': self.sf_max_github_spin.value(),
            'sources_finder_max_m3uguide':
                self.sf_max_m3uguide_spin.value(),
            'sources_finder_update_interval_hours':
                self.sf_update_interval_spin.value(),
        })
        c.save()
        self.core.apply_auto_update_setting()
        self.core.settings_changed.emit()


class CacheManagerDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Управление кэшем", parent, size=(660, 560))
        self.core = core
        self._setup_ui()
        self._load_settings()
        self._refresh_stats()

    def _setup_ui(self):
        layout = self.root
        self.tabs = QTabWidget()

        checks_tab = QWidget()
        cl = QVBoxLayout(checks_tab)
        self.checks_stats_label = QLabel()
        self.checks_stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.checks_stats_label.setWordWrap(True)
        cl.addWidget(self.checks_stats_label)

        c_form = QFormLayout()
        self.use_check_cache_check = QCheckBox(
            "Использовать кэш результатов проверок")
        c_form.addRow(self.use_check_cache_check)
        self.check_ttl_spin = QSpinBox()
        self.check_ttl_spin.setRange(1, 720)
        self.check_ttl_spin.setSuffix(" ч")
        c_form.addRow("TTL результатов:", self.check_ttl_spin)
        cl.addLayout(c_form)

        c_btns = QHBoxLayout()
        b1 = QPushButton("Очистить кэш проверок")
        b1.clicked.connect(self._clear_checks)
        c_btns.addWidget(b1)
        b2 = QPushButton("Удалить устаревшие")
        b2.clicked.connect(self._cleanup_checks)
        c_btns.addWidget(b2)
        c_btns.addStretch()
        cl.addLayout(c_btns)
        cl.addStretch()
        self.tabs.addTab(checks_tab, "Проверки")

        lc_tab = QWidget()
        lcl = QVBoxLayout(lc_tab)
        self.lc_stats_label = QLabel()
        self.lc_stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.lc_stats_label.setWordWrap(True)
        lcl.addWidget(self.lc_stats_label)

        lc_form = QFormLayout()
        self.use_lc_check = QCheckBox("Использовать LinkCache")
        lc_form.addRow(self.use_lc_check)
        self.lc_hours_spin = QSpinBox()
        self.lc_hours_spin.setRange(1, 720)
        self.lc_hours_spin.setSuffix(" ч")
        lc_form.addRow("TTL:", self.lc_hours_spin)
        self.lc_max_files_spin = QSpinBox()
        self.lc_max_files_spin.setRange(100, 1_000_000)
        lc_form.addRow("Макс. файлов:", self.lc_max_files_spin)
        self.lc_max_mb_spin = QSpinBox()
        self.lc_max_mb_spin.setRange(1, 8192)
        self.lc_max_mb_spin.setSuffix(" МБ")
        lc_form.addRow("Макс. размер:", self.lc_max_mb_spin)
        lcl.addLayout(lc_form)

        lc_btns = QHBoxLayout()
        b3 = QPushButton("Очистить LinkCache")
        b3.clicked.connect(self._clear_link_cache)
        lc_btns.addWidget(b3)
        lc_btns.addStretch()
        lcl.addLayout(lc_btns)
        lcl.addStretch()
        self.tabs.addTab(lc_tab, "LinkCache")

        epg_tab = QWidget()
        el = QVBoxLayout(epg_tab)
        self.epg_stats_label = QLabel()
        self.epg_stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.epg_stats_label.setWordWrap(True)
        el.addWidget(self.epg_stats_label)

        e_form = QFormLayout()
        self.epg_ttl_spin = QSpinBox()
        self.epg_ttl_spin.setRange(1, 720)
        self.epg_ttl_spin.setSuffix(" ч")
        e_form.addRow("TTL:", self.epg_ttl_spin)
        el.addLayout(e_form)

        epg_btns = QHBoxLayout()
        b4 = QPushButton("Очистить EPG-кэш")
        b4.clicked.connect(self._clear_epg)
        epg_btns.addWidget(b4)
        epg_btns.addStretch()
        el.addLayout(epg_btns)
        el.addStretch()
        self.tabs.addTab(epg_tab, "EPG")

        layout.addWidget(self.tabs)

        common = QHBoxLayout()
        self.use_cache_check = QCheckBox("SQLite-кэш включён")
        self.use_cache_check.toggled.connect(self._on_cache_toggled)
        common.addWidget(self.use_cache_check)
        common.addStretch()

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self._refresh_stats)
        common.addWidget(self.refresh_btn)

        self.vacuum_btn = QPushButton("VACUUM")
        self.vacuum_btn.clicked.connect(self._vacuum)
        common.addWidget(self.vacuum_btn)

        self.clear_all_btn = QPushButton("Очистить всё")
        self.clear_all_btn.clicked.connect(self._clear_all)
        common.addWidget(self.clear_all_btn)
        layout.addLayout(common)

        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.accepted.connect(self._save_and_accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _load_settings(self):
        c = self.core.config
        self.use_cache_check.setChecked(
            bool(c.get('use_cache_manager', True)))
        self.use_check_cache_check.setChecked(
            bool(c.get('use_check_result_cache', True)))
        self.check_ttl_spin.setValue(
            int(c.get('check_result_cache_ttl_hours',
                      CHECK_RESULT_CACHE_TTL_HOURS)))
        self.use_lc_check.setChecked(bool(c.get('use_link_cache', True)))
        self.lc_hours_spin.setValue(int(c.get('link_cache_hours', 6)))
        self.lc_max_files_spin.setValue(
            int(c.get('link_cache_max_files', 10000)))
        self.lc_max_mb_spin.setValue(int(c.get('link_cache_max_mb', 64)))
        self.epg_ttl_spin.setValue(
            int(c.get('epg_cache_ttl_hours', EPG_CACHE_TTL_HOURS)))
        self._on_cache_toggled(self.use_cache_check.isChecked())

    def _on_cache_toggled(self, enabled: bool):
        for w in (self.use_check_cache_check, self.check_ttl_spin,
                  self.use_lc_check, self.lc_hours_spin,
                  self.lc_max_files_spin, self.lc_max_mb_spin,
                  self.epg_ttl_spin, self.vacuum_btn,
                  self.clear_all_btn, self.refresh_btn):
            w.setEnabled(enabled)
        for w in self.tabs.findChildren(QPushButton):
            w.setEnabled(enabled)

    @staticmethod
    def _fmt_mb(b: int) -> str:
        return f"{b / (1024 * 1024):.1f} МБ"

    @staticmethod
    def _fmt_int(n: int) -> str:
        return f"{n:,}".replace(',', ' ')

    def _refresh_stats(self):
        cm = self.core.cache_manager
        if cm is None:
            empty = "<i>Кэш отключён в настройках.</i>"
            self.checks_stats_label.setText(empty)
            self.lc_stats_label.setText(empty)
            self.epg_stats_label.setText(empty)
            return

        check_ttl = self.check_ttl_spin.value()
        epg_ttl = self.epg_ttl_spin.value()
        st = cm.get_stats(check_ttl, epg_ttl)

        cr = st['check_results']
        self.checks_stats_label.setText(
            f"<b>Всего записей:</b> {self._fmt_int(cr['count'])}<br>"
            f"<b>Устаревших (>{check_ttl} ч):</b> "
            f"{self._fmt_int(cr['old_count'])}<br>"
            f"<b>Размер БД:</b> {self._fmt_mb(st['db_size_bytes'])}")

        lc = st['link_cache']
        self.lc_stats_label.setText(
            f"<b>Файлов:</b> {self._fmt_int(lc['files'])}<br>"
            f"<b>Размер:</b> {self._fmt_mb(lc['bytes'])}<br>"
            f"<b>Лимит файлов:</b> "
            f"{self._fmt_int(self.lc_max_files_spin.value())}<br>"
            f"<b>Лимит размера:</b> {self.lc_max_mb_spin.value()} МБ")

        epg = st['epg_entries']
        epg_ch = st.get('epg_channels', {'count': 0, 'old_count': 0})
        loaded = ("загружено" if self.core.epg_db.is_loaded
                  else "не загружено")
        self.epg_stats_label.setText(
            f"<b>Программ в БД:</b> {self._fmt_int(epg['count'])}<br>"
            f"<b>Каналов EPG в БД:</b> {self._fmt_int(epg_ch['count'])}<br>"
            f"<b>Устаревших (>{epg_ttl} ч):</b> "
            f"{self._fmt_int(epg['old_count'])}<br>"
            f"<b>Статус:</b> {loaded}")

    def _confirm_and_clear(self, msg: str, action: Callable[[], None],
                           refresh: bool = True) -> bool:
        if not confirm(self, msg):
            return False
        action()
        if refresh:
            self._refresh_stats()
        return True

    def _clear_checks(self):
        if self.core.cache_manager is None:
            return
        self._confirm_and_clear(
            "Очистить кэш проверок ссылок?",
            self.core.cache_manager.clear)

    def _cleanup_checks(self):
        if self.core.cache_manager is None:
            return
        days = max(1, self.check_ttl_spin.value() // 24)
        n = self.core.cache_manager.cleanup_old(days)
        info_box(self, f"Удалено записей: {n}", "Готово")
        self._refresh_stats()

    def _clear_link_cache(self):
        if self.core.cache_manager is None:
            return

        def _do():
            self.core.cache_manager.clear_link_cache()
            self.core.link_source_manager.invalidate_cache()

        self._confirm_and_clear("Очистить LinkCache?", _do)

    def _clear_epg(self):
        if self.core.cache_manager is None:
            return

        def _do():
            self.core.cache_manager.clear_epg()
            self.core.epg_db.clear()

        self._confirm_and_clear("Очистить EPG-кэш?", _do)

    def _vacuum(self):
        if self.core.cache_manager is None:
            return
        try:
            self.core.cache_manager.vacuum()
            info_box(self, "База сжата", "Готово")
            self._refresh_stats()
        except Exception as e:
            error_box(self, str(e))

    def _clear_all(self):
        if self.core.cache_manager is None:
            return

        def _do():
            self.core.cache_manager.clear()
            self.core.cache_manager.clear_link_cache()
            self.core.cache_manager.clear_epg()
            self.core.epg_db.clear()
            self.core.link_source_manager.invalidate_cache()

        if self._confirm_and_clear(
                "Очистить ВСЕ кэши: проверки, LinkCache, EPG?\n"
                "Это действие необратимо.", _do):
            info_box(self, "Все кэши очищены", "Готово")

    def _save_and_accept(self):
        c = self.core.config
        old_cache = bool(c.get('use_cache_manager', True))
        new_cache = self.use_cache_check.isChecked()

        c.set('use_cache_manager', new_cache)
        c.set('use_check_result_cache',
              self.use_check_cache_check.isChecked())
        c.set('check_result_cache_ttl_hours',
              self.check_ttl_spin.value())
        c.set('use_link_cache', self.use_lc_check.isChecked())
        c.set('link_cache_hours', self.lc_hours_spin.value())
        c.set('link_cache_max_files', self.lc_max_files_spin.value())
        c.set('link_cache_max_mb', self.lc_max_mb_spin.value())
        c.set('epg_cache_ttl_hours', self.epg_ttl_spin.value())
        c.save()

        if new_cache != old_cache:
            self.core.set_cache_manager_enabled(new_cache)

        if self.core.cache_manager is not None:
            with suppress(Exception):
                self.core.cache_manager.configure_link_cache(
                    max_files=self.lc_max_files_spin.value(),
                    max_mb=self.lc_max_mb_spin.value())

        self.core.settings_changed.emit()
        super().accept()