# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Главное окно."""
from __future__ import annotations

import json
import os
import threading
import weakref
from contextlib import suppress
from functools import partial
from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import (
    QSettings, QRunnable, Qt, QThreadPool, pyqtSignal)
from PyQt6.QtGui import QAction, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QStatusBar, QStyle, QTabWidget, QToolBar, QVBoxLayout,
    QWidget)

from app_core import ApplicationCore
from channel_table_model import ChannelTableModel
from playlist_tab import PlaylistTab

from dialogs import (
    BlacklistDialog,
    CacheManagerDialog,
    DomainBlacklistDialog,
    DomainUserAgentDialog,
    GeneralSettingsDialog,
    GroupTemplateListDialog,
    HelpBrowser,
    HelpDialog,
    IconProvider,
    LinkReplacementSettingsDialog,
    LinkSourceManagerDialog,
    PlaylistCreationOptions,
    PlaylistFromSourcesDialog,
    RemoveMetadataDialog,
    SourcesFinderDialog,
    SupportDialog,
    _is_qobject_valid,
    make_action,
)

from config import Config
from constants import (
    APP_VERSION, DEFAULT_EPG_URL, GROUP_FILTER_ALL,
    GROUP_FILTER_DUPLICATES,
    M3U_FILTER, RECENT_FILES_MAX,
    SOURCES_FINDER_UPDATE_INTERVAL_HOURS,
    STATUS_FILTER_ALL, STATUS_FILTER_ITEMS, StatusText,
)
from logging_setup import logger
from models import ChannelData, LinkQuality
from name_normalizer import display_name as _display_name
from parsers import M3UParser
from paths import (Paths, confirm, confirm_three, error_box,
                    info_box, open_external, save_file_dialog,
                    warn_box)
from sources import LinkSource
from theme import invalidate_cache as theme_invalidate
from utils import _StopToken


OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)
YES_NO = (QMessageBox.StandardButton.Yes |
          QMessageBox.StandardButton.No)


def _settings() -> QSettings:
    org = QApplication.organizationName() or "Ksenia"
    app = QApplication.applicationName() or "KseniaM3UEditor"
    return QSettings(org, app)


class _PlaylistCreateTask(QRunnable):
    def __init__(self, core, sources_to_load, opts, stop_token,
                 win_ref, epg_sources=None):
        super().__init__()
        self.core = core
        self.sources_to_load = list(sources_to_load)
        self.opts = opts
        self.stop = stop_token
        self.win_ref = win_ref
        self.epg_sources = [u for u in (epg_sources or [])
                            if u and u.strip()]
        self.setAutoDelete(True)

    @staticmethod
    def _dedupe_by_url(
            channels: List[ChannelData]
    ) -> List[ChannelData]:
        seen: Set[str] = set()
        result: List[ChannelData] = []
        for ch in channels:
            url = (ch.link.url or "").strip()
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            result.append(ch)
        return result

    def _emit_progress(self, text: str):
        w = self.win_ref()
        if w is None or not _is_qobject_valid(w):
            return
        try:
            w._bg_progress.emit(0, 0, text)
        except Exception:
            logger.exception("_bg_progress.emit")

    def _load_epg_if_needed(self) -> bool:
        if not self.opts.apply_epg:
            return False
        if self.core.epg_db.has_channel_info:
            return True
        urls = (list(self.epg_sources)
                if self.epg_sources
                else [self.core.get_default_epg_url()])
        urls = [u for u in urls if u and u.strip()]
        if not urls:
            logger.info(
                "EPG не задан (default_epg_url пуст) — "
                "создание плейлиста без EPG.")
            return False
        self._emit_progress(f"⏳ Загрузка EPG: {urls[0]}")
        try:
            count, errors = self.core.epg_db.load_from_urls(
                urls, timeout=20, stop_token=self.stop)
        except Exception:
            logger.exception("EPG load in _PlaylistCreateTask")
            return False
        if errors:
            logger.warning("EPG errors in _PlaylistCreateTask: %s",
                           errors[:3])
        if count <= 0:
            logger.info("EPG loaded 0 programmes, continue anyway")
            return False
        self._emit_progress(f"✓ EPG загружен: {count} программ")
        try:
            self.core.epg_updated.emit()
        except Exception:
            logger.exception("epg_updated.emit")
        return True

    def _try_load_from_normalized_index(
            self,
    ) -> Optional[List[ChannelData]]:
        if self.core.cache_manager is None:
            return None
        settings = self.core.get_replacement_settings()
        try:
            if not self.core.has_normalized_index():
                return None
        except Exception:
            logger.exception("has_normalized_index")
            return None

        try:
            settings_fp = self.core.link_source_manager\
                ._settings_fingerprint(settings)
        except Exception:
            logger.exception("_settings_fingerprint")
            return None
        try:
            data = self.core.cache_manager.load_normalized_index(
                settings_fp, max_age_hours=24)
        except Exception:
            logger.exception("load_normalized_index")
            return None
        if not data:
            return None

        names = {s.name for s in self.sources_to_load}
        filtered: Dict[str, List[Dict]] = {
            k: v for k, v in data.items() if k in names}
        if not filtered:
            return None

        seen: Set[Tuple[str, str, str]] = set()
        result: List[ChannelData] = []
        for src_name, rows in filtered.items():
            for row in rows:
                url = (row.get('url') or '').strip()
                if not url:
                    continue
                name = (row.get('name') or '').strip()
                tvg_id = (row.get('tvg_id') or '').strip()
                key = (name, url, tvg_id)
                if key in seen:
                    continue
                seen.add(key)
                ch = ChannelData()
                ch.meta.name = name
                ch.meta.tvg_id = tvg_id
                ch.link.url = url
                ch.link.has_url = True
                ch.link.link_source = src_name
                ch.update_extinf()
                ch.status.url_status = True
                ch.status.status_text = StatusText.WORKING
                ch.status.link_quality = LinkQuality.WORKING
                result.append(ch)
        if not result:
            return None
        return result

    def run(self):
        try:
            all_channels: List[ChannelData] = []
            settings = self.core.get_replacement_settings()
            epg_db = self.core.epg_db

            epg_ready = self._load_epg_if_needed()
            if self.stop.is_set():
                return

            cached = self._try_load_from_normalized_index()
            if cached is not None:
                self._emit_progress(
                    f"✓ Загружено из кэша: "
                    f"{len(cached)} каналов (только живые)")
                all_channels = cached
            else:
                for src in self.sources_to_load:
                    if self.stop.is_set():
                        return
                    chs = self.core.link_source_manager \
                        .load_links_from_source(
                            src, use_cache=False,
                            config=self.core.config,
                            update_meta=False,
                            stop_token=self.stop,
                            blacklist_manager=(
                                self.core.blacklist_manager),
                            domain_blacklist_manager=(
                                self.core.domain_blacklist_manager),
                            domain_user_agent_manager=(
                                self.core.domain_user_agent_manager),
                            settings=settings)
                    for ch in chs:
                        ch.link.link_source = src.name
                    all_channels.extend(chs)

            if (self.opts.apply_blacklist
                    and self.core.blacklist_manager.get_all()):
                all_channels, _ = \
                    self.core.apply_blacklist_to_channels(
                        all_channels)

            if (self.opts.apply_domain_blacklist
                    and self.core.domain_blacklist_manager.get_all()):
                all_channels, _ = \
                    self.core.apply_domain_blacklist_clean(
                        all_channels)

            if self.stop.is_set():
                return
            if (self.opts.apply_ua
                    and (self.core.domain_user_agent_manager
                         .get_all_rules())):
                self.core.apply_domain_user_agent(all_channels)

            if epg_ready:
                try:
                    epg_db.set_prefer_tvg_id(
                        bool(self.core.config.get(
                            'epg_prefer_tvg_id_over_name', True)))
                    overwrite = bool(self.core.config.get(
                        'epg_overwrite_metadata', True))
                    rename = bool(self.core.config.get(
                        'epg_overwrite_channel_name', False))
                    for ch in all_channels:
                        if self.stop.is_set():
                            return
                        epg_db.apply_metadata_to_channel(
                            ch, settings,
                            overwrite_metadata=overwrite,
                            rename_channel=rename)
                except Exception:
                    logger.exception(
                        "EPG apply during playlist creation")

            for ch in all_channels:
                try:
                    new_name = _display_name(
                        ch.meta.name or "",
                        settings=settings,
                        epg_db=epg_db if epg_ready else None,
                        tvg_id=ch.meta.tvg_id,
                        tvg_name=ch.meta.tvg_name,
                        alternative_names=(
                            ch.meta.epg_alternative_names))
                except Exception:
                    logger.exception(
                        "display_name failed for %r",
                        ch.meta.name)
                    new_name = ch.meta.name or ""
                if new_name and new_name != ch.meta.name:
                    ch.meta.name = new_name
                    ch.update_extinf()

            if self.opts.dedupe_by_url:
                all_channels = self._dedupe_by_url(all_channels)

            if self.stop.is_set():
                return

            if self.opts.preserve_order:
                priority_map = {s.name: s.priority
                                for s in self.sources_to_load}
                all_channels.sort(key=lambda ch: (
                    priority_map.get(ch.link.link_source, 999),
                    ch.original_index
                    if ch.original_index >= 0
                    else float('inf')))
            else:
                all_channels.sort(
                    key=lambda ch: ch.original_index
                    if ch.original_index >= 0
                    else float('inf'))

            if self.stop.is_set():
                return
            w = self.win_ref()
            if w is None or not _is_qobject_valid(w):
                return
            self.core.playlist_from_sources_ready.emit(
                all_channels)
        except Exception as e:
            logger.exception("Creating playlist failed")
            w = self.win_ref()
            if w is None or not _is_qobject_valid(w):
                return
            self.core.playlist_from_sources_failed.emit(str(e))
        finally:
            # Снимаем регистрацию — критично, иначе при выходе
            # или отключении кэша воркер останется «живым».
            with suppress(Exception):
                self.core._workers.remove_runnable(self)


class _StableExportTask(QRunnable):
    def __init__(self, tab_ref, fp, win_ref):
        super().__init__()
        self.tab_ref = tab_ref
        self.fp = fp
        self.win_ref = win_ref
        self.setAutoDelete(True)

    def run(self):
        def progress(i, total, text):
            w = self.win_ref()
            if w is not None and _is_qobject_valid(w):
                try:
                    w._bg_progress.emit(i, total, text)
                except Exception:
                    logger.exception("_bg_progress.emit")

        try:
            t = self.tab_ref()
            if t is None:
                return
            written, skipped = t.export_stable(
                self.fp, progress_cb=progress)
            w = self.win_ref()
            if w is not None and _is_qobject_valid(w):
                w._bg_export_done.emit(written, skipped)
        except Exception as e:
            w = self.win_ref()
            if w is not None and _is_qobject_valid(w):
                w._bg_export_failed.emit(str(e))


class MainWindow(QMainWindow):
    _bg_progress = pyqtSignal(int, int, str)
    _bg_export_done = pyqtSignal(int, int)
    _bg_export_failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            f"Ksenia M3U Editor {APP_VERSION}")
        self.resize(1400, 900)
        self.core = ApplicationCore.instance()
        self.tabs: Dict[QWidget, PlaylistTab] = {}
        self.current_tab: Optional[PlaylistTab] = None
        self.copied_channel: Optional[ChannelData] = None
        self.copied_channels: Optional[List[ChannelData]] = None
        self.recent_files: List[str] = []
        self._open_dialogs: List[weakref.ref] = []
        self._toolbar: Optional[QToolBar] = None
        self._toolbar_actions: List[QAction] = []
        self.toolbar_undo_action: Optional[QAction] = None
        self.toolbar_redo_action: Optional[QAction] = None
        self._playlist_creation_stop: Optional[_StopToken] = None

        IconProvider.initialize(self.style())
        IconProvider.set_enabled(
            bool(self.core.config.get('enable_icons', True)))

        self._setup_ui()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_statusbar()
        self._bg_progress.connect(self.show_progress)
        self._bg_export_done.connect(self._on_stable_export_done)
        self._bg_export_failed.connect(
            self._on_stable_export_failed)
        self._load_settings()
        self._apply_config()
        self._update_window_title()

        app = QApplication.instance()
        if app:
            app.paletteChanged.connect(self._on_palette_changed)
        self.core.settings_changed.connect(
            self._on_settings_changed)
        self.core.blacklist_updated.connect(
            self._on_blacklist_updated)
        self.core.domain_blacklist_updated.connect(
            self._on_domain_blacklist_updated)
        self.core.playlist_from_sources_ready.connect(
            self._finish_create_playlist_from_sources)
        self.core.playlist_from_sources_failed.connect(
            self._handle_playlist_creation_error)

        self.core.load_epg_from_cache_async()

    def _on_settings_changed(self):
        self._apply_config()
        for tab in self.tabs.values():
            if not _is_qobject_valid(tab):
                continue
            with suppress(Exception):
                with tab._suppress_save():
                    tab._rebuild_model_safely()

    def _on_blacklist_updated(self):
        for tab in list(self.tabs.values()):
            if not _is_qobject_valid(tab):
                continue
            selected_uids = {ch.uid for ch in tab.selected_channels}
            current_uid = (tab.current_channel.uid
                           if tab.current_channel else None)
            filtered, removed = \
                self.core.apply_blacklist_to_channels(
                    tab.all_channels)
            if removed > 0:
                if tab._state_save_timer.isActive():
                    tab._state_save_timer.stop()
                tab.undo_manager.save_state(
                    tab.all_channels,
                    "Применение ч.с. каналов")
                tab._pending_state_desc = ""
                tab.all_channels = filtered
                tab._normalize_order()
                tab.sync_to_core()
                tab.selected_channels = [
                    ch for ch in tab.all_channels
                    if ch.uid in selected_uids]
                if current_uid is not None:
                    tab.current_channel = next(
                        (ch for ch in tab.all_channels
                         if ch.uid == current_uid), None)
                else:
                    tab.current_channel = None
                tab.refresh_view()
            tab.update_info()

    def _on_domain_blacklist_updated(self):
        for tab in list(self.tabs.values()):
            if not _is_qobject_valid(tab):
                continue
            cleaned = tab._clean_blocked_by_domain()
            if cleaned > 0:
                logger.info(
                    "Cleaned %d URLs by domain blacklist", cleaned)
            tab.update_info()

    def _on_palette_changed(self, *args):
        theme_invalidate()
        IconProvider.clear_cache()
        self._rebuild_ui_icons()
        for tab in list(self.tabs.values()):
            if not _is_qobject_valid(tab):
                continue
            with suppress(Exception):
                with tab._suppress_save():
                    tab.model.refresh_all()
        self._apply_statusbar_palette()

    def _std_icon(
            self,
            sp: Optional[QStyle.StandardPixmap] = None,
            theme_name: str = "") -> QIcon:
        return IconProvider.get(sp, theme_name)

    def _rebuild_ui_icons(self):
        if self._toolbar is None:
            return
        IconProvider.clear_cache()
        self._populate_toolbar()
        if self.current_tab:
            self._on_undo_state(
                self.current_tab.undo_manager.can_undo(),
                self.current_tab.undo_manager.can_redo())

    def _apply_config(self):
        c = self.core.config
        IconProvider.set_enabled(
            bool(c.get('enable_icons', True)))
        if self._toolbar is not None:
            self._toolbar.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly
                if IconProvider.is_enabled()
                else Qt.ToolButtonStyle.ToolButtonTextOnly)
        font_size = int(c.get('cell_font_size', 10))
        for tab in self.tabs.values():
            if not _is_qobject_valid(tab):
                continue
            f = tab.table.font()
            f.setPointSize(font_size)
            tab.table.setFont(f)
        self.status_bar.setVisible(
            bool(c.get('show_status_bar', True)))
        self._apply_statusbar_palette()

    def _apply_statusbar_palette(self):
        if not hasattr(self, 'action_label'):
            return
        pal = self.action_label.palette()
        pal.setColor(QPalette.ColorRole.Window,
                     pal.color(QPalette.ColorRole.AlternateBase))
        pal.setColor(QPalette.ColorRole.WindowText,
                     pal.color(QPalette.ColorRole.Text))
        self.action_label.setPalette(pal)
        self.action_label.setAutoFillBackground(True)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.tab_widget.currentChanged.connect(
            self._on_tab_changed)
        layout.addWidget(self.tab_widget)

        fl = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Поиск... (префиксы: is:orphan, is:broken, is:working)")
        self.search_edit.textChanged.connect(
            self._on_search_changed)
        fl.addWidget(self.search_edit, 3)

        self.group_combo = QComboBox()
        self.group_combo.addItem(GROUP_FILTER_ALL)
        self.group_combo.setMinimumWidth(180)
        self.group_combo.currentTextChanged.connect(
            self._on_group_changed)
        fl.addWidget(self.group_combo, 0)

        self.status_combo = QComboBox()
        for label in STATUS_FILTER_ITEMS:
            self.status_combo.addItem(label)
        self.status_combo.setMinimumWidth(150)
        self.status_combo.setToolTip(
            "Фильтр по наличию и состоянию URL:\n"
            "• Всего — без фильтра;\n"
            "• С URL — has_valid_url;\n"
            "• Без URL — каналы-заготовки;\n"
            "• Работают — url_status is True;\n"
            "• Не работают — url_status is False;\n"
            "• Не проверены — с URL, но url_status is None.")
        self.status_combo.currentTextChanged.connect(
            self._on_status_filter_changed)
        fl.addWidget(self.status_combo, 0)

        self.duplicates_btn = QPushButton(GROUP_FILTER_DUPLICATES)
        self.duplicates_btn.setCheckable(True)
        self.duplicates_btn.setToolTip(
            "Показать только дубликаты.\n"
            "Каждая ГРУППА дубликатов подсвечивается своим "
            "оттенком.")
        self.duplicates_btn.toggled.connect(
            self._on_duplicates_toggled)
        fl.addWidget(self.duplicates_btn, 0)

        layout.addLayout(fl)

    def _setup_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("Файл")
        self._add_action(fm, "Новый", self._new_file, "Ctrl+N")
        self._add_action(fm, "Открыть", self._open_file, "Ctrl+O")
        self._add_action(
            fm, "Создать плейлист из источников...",
            self._create_playlist_from_sources)
        self.recent_menu = fm.addMenu("Открыть недавние")
        fm.addSeparator()
        self._add_action(fm, "Сохранить", self._save_file,
                         "Ctrl+S")
        self._add_action(fm, "Сохранить как", self._save_as,
                         "Ctrl+Shift+S")
        self._add_action(fm, "Сохранить всё", self._save_all)
        fm.addSeparator()
        self._add_action(fm, "Экспорт Stable-плейлиста...",
                         self._export_stable)
        fm.addSeparator()
        self._add_action(fm, "Импорт", self._import_file)
        self._add_action(fm, "Экспорт в CSV/JSON...",
                         self._export_file)
        fm.addSeparator()
        self._add_action(fm, "Выход", self.close, "Alt+F4")

        cm = mb.addMenu("Каналы")
        self._add_action(cm, "Новый канал",
                         partial(self._with_tab, '_new_channel'),
                         "Ctrl+Shift+A")
        self._add_action(cm, "Вырезать",
                         partial(self._with_tab, '_cut_channel'),
                         "Ctrl+X")
        self._add_action(cm, "Копировать",
                         partial(self._with_tab, '_copy_channel'),
                         "Ctrl+C")
        self._add_action(cm, "Вставить",
                         partial(self._with_tab, '_paste_channel'),
                         "Ctrl+V")
        cm.addSeparator()
        self._add_action(cm, "Массовая правка выбранных...",
                         partial(self._with_tab,
                                 '_mass_edit_selected'))
        cm.addSeparator()
        self._add_action(cm, "Удалить каналы без ссылок",
                         partial(self._with_tab,
                                 'delete_channels_without_urls'))
        self._add_action(cm, "Удалить каналы без метаданных",
                         partial(self._with_tab,
                                 'delete_channels_without_metadata'))
        cm.addSeparator()
        self._add_action(cm, "Сравнить плейлисты...",
                         self._show_compare)
        cm.addSeparator()
        self._add_action(cm, "Удалить метаданные...",
                         self._remove_metadata)

        lm = mb.addMenu("Ссылки")
        self._add_action(lm, "Проверить все ссылки",
                         partial(self._with_tab, 'check_all_urls'))
        self._add_action(lm, "Проверить выбранные ссылки",
                         partial(self._with_tab,
                                 '_check_selected_urls'))
        lm.addSeparator()
        self._add_action(lm, "Заменить все ссылки (из кэша)",
                         partial(self._with_tab,
                                 'replace_all_links'))
        self._add_action(lm, "Заменить выбранные ссылки",
                         partial(self._with_tab,
                                 'replace_selected_links'))
        lm.addSeparator()
        self._add_action(lm, "Удалить все ссылки",
                         partial(self._with_tab,
                                 'remove_all_urls'))
        self._add_action(lm, "Удалить выбранные ссылки",
                         partial(self._with_tab,
                                 '_remove_selected_urls'))
        lm.addSeparator()
        self._add_action(lm, "Удалить все битые ссылки",
                         partial(self._with_tab,
                                 '_remove_all_broken_urls'))
        lm.addSeparator()
        self._add_action(lm, "🚫 Заблокировать домен/IP…",
                         self._block_domain_manual)

        tm = mb.addMenu("Инструменты")
        self._add_action(
            tm,
            "Источники ссылок (обновить + проверить URL)...",
            self._manage_sources)
        self._add_action(
            tm, "🔍 Найти источники автоматически...",
            self._auto_find_sources)
        self._add_action(tm, "Редактировать заголовок...",
                         self._edit_header)
        tm.addSeparator()
        self._add_action(
            tm, "📋 Шаблоны группировки каналов…",
            self._manage_group_templates)
        self._add_action(
            tm, "💾 Запомнить группировку текущего плейлиста…",
            self._save_current_grouping_as_template)
        self._add_action(
            tm, "🎯 Применить шаблон группировки…",
            self._apply_group_template_menu)
        tm.addSeparator()
        self._add_action(tm, "Загрузить EPG", self._load_epg)
        self._add_action(
            tm,
            "Применить метаданные из EPG ко всем каналам",
            self._apply_epg_metadata_all)
        tm.addSeparator()
        self._add_action(tm, "Управление кэшем...",
                         self._manage_cache)
        tm.addSeparator()
        self._add_action(tm, "Менеджер чёрного списка каналов",
                         self._manage_blacklist)
        self._add_action(tm, "Применить чёрный список каналов",
                         self._apply_blacklist)
        tm.addSeparator()
        self._add_action(
            tm, "Менеджер чёрного списка домен/IP",
            self._manage_domain_blacklist)
        self._add_action(
            tm, "Очистить ссылки по чёрному списку домен/IP",
            self._apply_domain_blacklist)
        tm.addSeparator()
        self._add_action(tm, "User-Agent по доменам",
                         self._manage_ua_rules)
        tm.addSeparator()
        self._add_action(tm, "Удалить дубликаты...",
                         self._show_duplicates)
        tm.addSeparator()
        self._add_action(tm, "Показать лог", self._show_log)

        sm = mb.addMenu("Настройки")
        self._add_action(sm, "Настройки замены ссылок",
                         self._manage_replacement_settings)
        self._add_action(sm, "Общие настройки",
                         self._manage_general_settings)
        sm.addSeparator()
        self._add_action(sm, "Экспорт настроек...",
                         self._export_settings)
        self._add_action(sm, "Импорт настроек...",
                         self._import_settings)
        sm.addSeparator()
        self._add_action(sm, "Полноэкранный режим",
                         self._toggle_fullscreen, "F11")
        sm.addSeparator()
        self._add_action(sm, "Увеличить",
                         lambda: self._change_font(+1), "Ctrl++")
        self._add_action(sm, "Уменьшить",
                         lambda: self._change_font(-1), "Ctrl+-")
        self._add_action(sm, "Сбросить масштаб",
                         lambda: self._change_font(0), "Ctrl+0")

        hm = mb.addMenu("Справка")
        self._add_action(hm, "Руководство пользователя",
                         self._show_help, "F1")
        self._add_action(hm, "О программе и лицензия",
                         self._show_about)
        self._add_action(hm, "Поддержка проекта",
                         self._show_support)
        hm.addSeparator()
        self._add_action(hm, "О Qt",
                         lambda: QMessageBox.aboutQt(self))

    def _add_action(self, container, text: str, slot,
                    shortcut: str = "",
                    icon: Optional[QIcon] = None,
                    tooltip: str = "") -> QAction:
        a = make_action(self, text, slot, shortcut, icon, tooltip)
        if container is not None:
            container.addAction(a)
        return a

    def _setup_toolbar(self):
        if self._toolbar is None:
            self._toolbar = QToolBar("Главная", self)
            self._toolbar.setMovable(False)
            self.addToolBar(self._toolbar)
        self._populate_toolbar()

    def _populate_toolbar(self):
        if self._toolbar is None:
            return
        for a in self._toolbar_actions:
            with suppress(TypeError, RuntimeError):
                a.triggered.disconnect()
            a.setParent(None)
            a.deleteLater()
        self._toolbar_actions.clear()
        self._toolbar.clear()

        SP = QStyle.StandardPixmap
        self._toolbar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonIconOnly
            if IconProvider.is_enabled()
            else Qt.ToolButtonStyle.ToolButtonTextOnly)

        def add(text, slot, icon, tooltip):
            a = self._add_action(self._toolbar, text, slot,
                                 icon=icon, tooltip=tooltip)
            self._toolbar_actions.append(a)
            return a

        add("Новый", self._new_file,
            self._std_icon(SP.SP_FileIcon, "document-new"),
            "Новый плейлист")
        add("Открыть", self._open_file,
            self._std_icon(SP.SP_DialogOpenButton,
                           "document-open"),
            "Открыть файл")
        add("Сохранить", self._save_file,
            self._std_icon(SP.SP_DialogSaveButton,
                           "document-save"),
            "Сохранить (Ctrl+S)")
        self._toolbar.addSeparator()

        self.toolbar_undo_action = add(
            "Отменить", partial(self._with_tab, 'undo'),
            self._std_icon(SP.SP_ArrowBack, "edit-undo"),
            "Отменить (Ctrl+Z)")
        self.toolbar_redo_action = add(
            "Повторить", partial(self._with_tab, 'redo'),
            self._std_icon(SP.SP_ArrowForward, "edit-redo"),
            "Повторить (Ctrl+Y)")
        self.toolbar_undo_action.setEnabled(False)
        self.toolbar_redo_action.setEnabled(False)
        self._toolbar.addSeparator()

        add("Копировать",
            partial(self._with_tab, '_copy_channel'),
            self._std_icon(SP.SP_FileDialogDetailedView,
                           "edit-copy"),
            "Копировать (Ctrl+C)")
        add("Вырезать",
            partial(self._with_tab, '_cut_channel'),
            self._std_icon(SP.SP_FileDialogListView, "edit-cut"),
            "Вырезать (Ctrl+X)")
        add("Вставить",
            partial(self._with_tab, '_paste_channel'),
            self._std_icon(SP.SP_FileDialogContentsView,
                           "edit-paste"),
            "Вставить (Ctrl+V)")
        self._toolbar.addSeparator()

        add("Плеер", self._play_selected,
            self._std_icon(SP.SP_MediaPlay,
                           "media-playback-start"),
            "Открыть в плеере")
        self._toolbar.addSeparator()

        add("Увеличить", lambda: self._change_font(+1),
            self._std_icon(SP.SP_TitleBarMaxButton,
                           "zoom-in"),
            "Увеличить шрифт")
        add("Уменьшить", lambda: self._change_font(-1),
            self._std_icon(SP.SP_TitleBarMinButton,
                           "zoom-out"),
            "Уменьшить шрифт")

        if self.current_tab:
            self._on_undo_state(
                self.current_tab.undo_manager.can_undo(),
                self.current_tab.undo_manager.can_redo())

    def _setup_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.action_label = QLabel("Готов")
        self.action_label.setMinimumWidth(260)
        self.status_bar.addWidget(self.action_label, 1)

        self.info_label = QLabel("")
        self.status_bar.addPermanentWidget(self.info_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumWidth(220)
        self.progress_bar.setMaximumWidth(320)
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        self.status_bar.addPermanentWidget(self.progress_bar)

        self._apply_statusbar_palette()

    def _on_stable_export_done(self, written: int, skipped: int):
        if not _is_qobject_valid(self):
            return
        self.hide_progress()
        info_box(
            self,
            f"Stable-экспорт завершён.\n\n"
            f"Записано: {written}\n"
            f"Пропущено (нет живых URL): {skipped}\n\n"
            f"Состояние сохранено в stable_state.json.",
            "Stable")

    def _on_stable_export_failed(self, err: str):
        if not _is_qobject_valid(self):
            return
        self.hide_progress()
        error_box(self, err)

    def set_action(self, text: str):
        if hasattr(self, 'action_label'):
            self.action_label.setText(text or "Готов")

    def show_progress(self, value: int, total: int,
                      text: str = ""):
        if total <= 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, max(1, total))
            self.progress_bar.setValue(value)
        self.progress_bar.setVisible(True)
        if text:
            self.set_action(text)

    def hide_progress(self):
        self.progress_bar.setVisible(False)
        self.set_action("Готов")

    def _load_settings(self):
        s = _settings()
        g = s.value("window_geometry")
        if g:
            self.restoreGeometry(g)
        st = s.value("window_state")
        if st:
            self.restoreState(st)
        recent = s.value("recent_files", [])
        try:
            if recent is None:
                recent_list = []
            elif isinstance(recent, str):
                recent_list = [recent]
            else:
                recent_list = list(recent)
        except TypeError:
            recent_list = []
        self.recent_files = [f for f in recent_list
                             if isinstance(f, str)]
        self._update_recent_menu()

    def _save_settings(self):
        s = _settings()
        s.setValue("window_geometry", self.saveGeometry())
        s.setValue("window_state", self.saveState())
        s.setValue("recent_files", self.recent_files)
        s.sync()
        self.core.config.save()

    def _export_settings(self):
        fp = save_file_dialog(
            self, "Экспорт настроек", "ksenia_settings.json",
            "JSON (*.json);;Все файлы (*.*)", ".json")
        if not fp:
            return
        try:
            payload = {
                'version': APP_VERSION,
                'config': dict(self.core.config.config),
            }
            with open(fp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            info_box(self, f"Настройки сохранены:\n{fp}",
                     "Экспорт")
        except Exception as e:
            logger.exception("export_settings")
            error_box(self, str(e))

    def _import_settings(self):
        fp = QFileDialog.getOpenFileName(
            self, "Импорт настроек", "",
            "JSON (*.json);;Все файлы (*.*)")[0]
        if not fp:
            return
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            cfg = payload.get('config')
            if not isinstance(cfg, dict):
                warn_box(self, "Некорректный файл настроек")
                return
            if not confirm(
                    self,
                    "Заменить все текущие настройки?\n"
                    "Приложение будет перезапущено."):
                return
            for k, v in cfg.items():
                if k in Config.DEFAULT:
                    self.core.config.set(
                        k, self.core.config._coerce(k, v))
            self.core.config.save()
            info_box(self,
                     "Настройки импортированы. Перезапустите "
                     "приложение.", "Импорт")
        except Exception as e:
            logger.exception("import_settings")
            error_box(self, str(e))

    def _update_recent_menu(self):
        self.recent_menu.clear()
        self.recent_files = [fp for fp in self.recent_files
                             if os.path.exists(fp)]
        if not self.recent_files:
            a = QAction("Нет недавних", self)
            a.setEnabled(False)
            self.recent_menu.addAction(a)
            return
        for fp in self.recent_files:
            a = QAction(os.path.basename(fp), self)
            a.setToolTip(fp)
            a.triggered.connect(
                lambda checked, f=fp: self._open_recent(f))
            self.recent_menu.addAction(a)
        self.recent_menu.addSeparator()
        a = QAction("Очистить", self)
        a.triggered.connect(self._clear_recent)
        self.recent_menu.addAction(a)

    def _add_recent(self, fp: str):
        if fp in self.recent_files:
            self.recent_files.remove(fp)
        self.recent_files.insert(0, fp)
        self.recent_files = self.recent_files[:RECENT_FILES_MAX]
        self._update_recent_menu()

    def _open_recent(self, fp: str):
        if not os.path.exists(fp):
            warn_box(self, f"Файл не найден:\n{fp}")
            if fp in self.recent_files:
                self.recent_files.remove(fp)
            self._update_recent_menu()
            return
        self._open_file_in_tab(fp)

    def _clear_recent(self):
        self.recent_files.clear()
        self._update_recent_menu()

    def _update_window_title(self):
        if self.current_tab:
            fn = (os.path.basename(self.current_tab.filepath)
                  if self.current_tab.filepath else "Безымянный")
            mod = " *" if self.current_tab.modified else ""
            self.setWindowTitle(
                f"{fn}{mod} - Ksenia M3U Editor {APP_VERSION}")
        else:
            self.setWindowTitle(
                f"Ksenia M3U Editor {APP_VERSION}")

    def _with_tab(self, method_name: str):
        if self.current_tab:
            getattr(self.current_tab, method_name)()

    def _create_tab(self, filepath: Optional[str] = None,
                    title: Optional[str] = None) -> PlaylistTab:
        tab = PlaylistTab(filepath, parent_window=self)
        idx = self.tab_widget.addTab(
            tab,
            title or (os.path.basename(filepath)
                      if filepath else "Безымянный"))
        self.tabs[tab] = tab
        self.tab_widget.setCurrentIndex(idx)
        tab.undo_state_changed.connect(self._on_undo_state)
        tab.info_changed.connect(self._on_info)
        tab.groups_changed.connect(self._on_groups_changed)
        self.current_tab = tab
        tab.set_search_text(self.search_edit.text())
        self.duplicates_btn.blockSignals(True)
        self.duplicates_btn.setChecked(False)
        self.duplicates_btn.blockSignals(False)
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentIndex(0)
        self.status_combo.blockSignals(False)
        self._apply_config()
        self._update_window_title()
        self._update_groups()
        tab.set_group_filter(self.group_combo.currentText())
        return tab

    def _new_file(self):
        self.set_action("Новый плейлист")
        self._create_tab()

    def _open_file(self):
        fp = QFileDialog.getOpenFileName(
            self, "Открыть", "", M3U_FILTER)[0]
        if fp:
            self._open_file_in_tab(fp)

    def _create_playlist_from_sources(self):
        dialog = PlaylistFromSourcesDialog(self.core, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        opts: PlaylistCreationOptions = dialog.get_result()
        if not opts.sources:
            return

        self.set_action("⏳ Загрузка из источников...")
        sources_to_load = sorted(opts.sources,
                                  key=lambda s: s.priority)
        _stop = _StopToken()
        self._playlist_creation_stop = _stop
        win_ref = weakref.ref(self)

        task = _PlaylistCreateTask(
            self.core, sources_to_load, opts, _stop, win_ref,
            epg_sources=None)
        # Регистрируем задачу, чтобы stop_all() мог её
        # корректно остановить.
        self.core._workers.add_runnable(task)
        QThreadPool.globalInstance().start(task)

    def _finish_create_playlist_from_sources(
            self, channels: List[ChannelData]):
        if not _is_qobject_valid(self):
            return
        self.hide_progress()
        if not channels:
            info_box(
                self,
                "Источники не содержат каналов "
                "(или все отфильтрованы ЧС).")
            self.set_action("Готов")
            return
        tab = PlaylistTab(parent_window=self)
        tab.all_channels = channels
        tab._normalize_order()
        tab.undo_manager.reset(tab.all_channels)
        tab.modified = True

        idx = self.tab_widget.addTab(tab, "Из источников")
        self.tabs[tab] = tab
        self.tab_widget.setCurrentIndex(idx)
        tab.undo_state_changed.connect(self._on_undo_state)
        tab.info_changed.connect(self._on_info)
        tab.groups_changed.connect(self._on_groups_changed)
        self.current_tab = tab
        tab.sync_to_core()
        tab.refresh_view()
        tab.update_modified_status()
        self._apply_config()
        self.duplicates_btn.blockSignals(True)
        self.duplicates_btn.setChecked(False)
        self.duplicates_btn.blockSignals(False)
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentIndex(0)
        self.status_combo.blockSignals(False)
        self._update_window_title()
        self._update_groups()
        tab.set_group_filter(self.group_combo.currentText()
                              or GROUP_FILTER_ALL)
        self.set_action(
            f"✓ Создан плейлист: {len(channels)} каналов")

    def _handle_playlist_creation_error(self, error_message: str):
        if not _is_qobject_valid(self):
            return
        self.hide_progress()
        error_box(self,
                   f"Не удалось создать плейлист:\n"
                   f"{error_message}")
        self.set_action("✗ Ошибка создания плейлиста")

    def _open_file_in_tab(self, fp: str):
        logger.info("Opening file: %s", fp)
        for tab in self.tabs.values():
            if tab.filepath == fp:
                self.tab_widget.setCurrentIndex(
                    self.tab_widget.indexOf(tab))
                return
        try:
            self.set_action(
                f"⏳ Открытие: {os.path.basename(fp)}")
            self._create_tab(filepath=fp)
            self._add_recent(fp)
            self.set_action(
                f"✓ Открыт: {os.path.basename(fp)}")
        except Exception as e:
            logger.exception("Open failed")
            error_box(self, f"Не удалось открыть:\n{e}")

    def _save_file(self):
        if not self.current_tab:
            return
        if not self.current_tab.filepath:
            self._save_as()
        else:
            self.current_tab.flush_sync()
            if self.current_tab.save_to_file():
                self.set_action("✓ Файл сохранён")
                self._update_window_title()

    def _save_as(self):
        if not self.current_tab:
            return
        default = self.current_tab.filepath or "playlist.m3u"
        fp = save_file_dialog(self, "Сохранить как",
                              default, M3U_FILTER, ".m3u")
        if not fp:
            return
        self.current_tab.flush_sync()
        if self.current_tab.save_to_file(fp):
            idx = self.tab_widget.indexOf(self.current_tab)
            if idx >= 0:
                self.tab_widget.setTabText(
                    idx, os.path.basename(fp))
            self.set_action("✓ Файл сохранён")
            self._update_window_title()
            self._add_recent(fp)

    def _save_all(self):
        modified = [t for t in self.tabs.values() if t.modified]
        if not modified:
            return
        for tab in modified:
            if not tab.filepath:
                default = f"playlist_{tab.tab_id[:6]}.m3u"
                fp = save_file_dialog(
                    self,
                    f"Сохранить '{tab.filepath or 'Безымянный'}'",
                    default, M3U_FILTER, ".m3u")
                if not fp:
                    if not confirm(self,
                                    "Продолжить сохранение "
                                    "остальных?"):
                        return
                    continue
                idx = self.tab_widget.indexOf(tab)
                if idx >= 0:
                    self.tab_widget.setTabText(
                        idx, os.path.basename(fp))
                self._add_recent(fp)
                tab.filepath = fp
            tab.flush_sync()
            tab.save_to_file()
        self.set_action("✓ Все файлы сохранены")

    def _export_stable(self):
        if not self.current_tab:
            info_box(self, "Нет открытого плейлиста")
            return
        fp = save_file_dialog(self, "Экспорт Stable",
                              "stable.m3u", M3U_FILTER, ".m3u")
        if not fp:
            return
        tab = self.current_tab
        win_ref = weakref.ref(self)
        tab_ref = weakref.ref(tab)
        task = _StableExportTask(tab_ref, fp, win_ref)
        QThreadPool.globalInstance().start(task)

    def _import_file(self):
        fp = QFileDialog.getOpenFileName(
            self, "Импорт", "", "Все файлы (*.*)")[0]
        if not fp:
            return
        try:
            with open(fp, 'r', encoding='utf-8',
                      errors='replace') as f:
                content = f.read()
            if not self.current_tab:
                self._new_file()
            tab = self.current_tab
            if not tab.header_manager.has_extm3u:
                tab.header_manager.parse_header(content)
            new_chs = M3UParser.parse(
                content, os.path.basename(fp))
            tab.all_channels.extend(new_chs)
            tab.save_state("Импорт файла")
            tab._normalize_order()
            tab.sync_to_core()
            with tab._suppress_save():
                tab._rebuild_model_safely()
            self._update_groups()
            self.set_action(f"✓ Импортировано: {len(new_chs)}")
        except Exception as e:
            logger.exception("import failed")
            error_box(self, str(e))

    def _export_file(self):
        if self.current_tab:
            self.current_tab.export_as()

    def _close_tab(self, index: int):
        w = self.tab_widget.widget(index)
        if w not in self.tabs:
            return
        tab = self.tabs[w]
        if tab.modified:
            r = confirm_three(self, "Файл изменён. Сохранить?")
            if r == 'yes':
                if not tab.filepath:
                    fp = save_file_dialog(
                        self, "Сохранить как", "playlist.m3u",
                        M3U_FILTER, ".m3u")
                    if not fp:
                        return
                    tab.filepath = fp
                tab.flush_sync()
                if not tab.save_to_file():
                    return
            elif r == 'cancel':
                return
        tab.flush_sync()
        with suppress(TypeError, RuntimeError):
            tab.undo_state_changed.disconnect(
                self._on_undo_state)
        with suppress(TypeError, RuntimeError):
            tab.info_changed.disconnect(self._on_info)
        with suppress(TypeError, RuntimeError):
            tab.groups_changed.disconnect(
                self._on_groups_changed)
        tab.disconnect_signals()
        self.core.unregister_tab(tab.tab_id)
        del self.tabs[w]
        self.tab_widget.removeTab(index)
        tab.deleteLater()
        if self.tab_widget.count() == 0:
            self.current_tab = None
            self.duplicates_btn.blockSignals(True)
            self.duplicates_btn.setChecked(False)
            self.duplicates_btn.blockSignals(False)
            self.status_combo.blockSignals(True)
            self.status_combo.setCurrentIndex(0)
            self.status_combo.blockSignals(False)
            self._update_window_title()
            self._update_groups()
            self._on_info("Готов")
            self._on_undo_state(False, False)
            self.set_action("Готов")

    def _on_tab_changed(self, index: int):
        if index >= 0:
            w = self.tab_widget.widget(index)
            if w in self.tabs:
                self.current_tab = self.tabs[w]
                self.current_tab.set_search_text(
                    self.search_edit.text())
                self._update_groups()
                in_dup = \
                    self.current_tab.model.is_duplicate_mode()
                self.duplicates_btn.blockSignals(True)
                self.duplicates_btn.setChecked(in_dup)
                self.duplicates_btn.blockSignals(False)

                cur_status = self.current_tab.model.status_filter()
                self.status_combo.blockSignals(True)
                idx_status = self.status_combo.findText(
                    cur_status or STATUS_FILTER_ALL)
                self.status_combo.setCurrentIndex(
                    idx_status if idx_status >= 0 else 0)
                self.status_combo.blockSignals(False)

                if in_dup:
                    self.current_tab.set_duplicate_filter(True)
                else:
                    self.current_tab.set_group_filter(
                        self.group_combo.currentText()
                        or GROUP_FILTER_ALL)
                self._update_window_title()
                self.current_tab.update_info()
                self._on_undo_state(
                    self.current_tab.undo_manager.can_undo(),
                    self.current_tab.undo_manager.can_redo())
                return
        self.current_tab = None
        self.duplicates_btn.blockSignals(True)
        self.duplicates_btn.setChecked(False)
        self.duplicates_btn.blockSignals(False)
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentIndex(0)
        self.status_combo.blockSignals(False)
        self._update_window_title()
        self._update_groups()
        self._on_info("Готов")
        self._on_undo_state(False, False)
        self.set_action("Готов")

    def _on_search_changed(self, text: str):
        if self.current_tab:
            self.current_tab.set_search_text(text)

    def _on_group_changed(self, group: str):
        if self.duplicates_btn.isChecked():
            self.duplicates_btn.blockSignals(True)
            self.duplicates_btn.setChecked(False)
            self.duplicates_btn.blockSignals(False)
        if self.current_tab:
            self.current_tab.set_duplicate_filter(False)
            self.current_tab.set_group_filter(group)

    def _on_status_filter_changed(self, status: str):
        if self.current_tab:
            self.current_tab.set_status_filter(status)

    def _on_duplicates_toggled(self, checked: bool):
        if not self.current_tab:
            self.duplicates_btn.blockSignals(True)
            self.duplicates_btn.setChecked(False)
            self.duplicates_btn.blockSignals(False)
            return
        if checked:
            self.current_tab.set_duplicate_filter(True)
        else:
            self.current_tab.set_duplicate_filter(False)
            self.current_tab.set_group_filter(
                self.group_combo.currentText()
                or GROUP_FILTER_ALL)

    def _on_groups_changed(self):
        if not _is_qobject_valid(self):
            return
        self._update_groups()
        if (not self.duplicates_btn.isChecked()
                and self.current_tab):
            self.current_tab.set_group_filter(
                self.group_combo.currentText()
                or GROUP_FILTER_ALL)

    def _update_groups(self):
        cur = self.group_combo.currentText()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem(GROUP_FILTER_ALL)
        if self.current_tab:
            groups = sorted({
                ch.meta.group
                for ch in self.current_tab.all_channels
                if ch.meta.group
            })
            for g in groups:
                self.group_combo.addItem(g)
        idx = self.group_combo.findText(cur)
        if idx < 0:
            idx = 0
        self.group_combo.setCurrentIndex(idx)
        self.group_combo.blockSignals(False)

    def _on_undo_state(self, cu: bool, cr: bool):
        sender = self.sender()
        if sender is not None and self.current_tab is not None:
            if sender is not self.current_tab:
                return
        if self.toolbar_undo_action is not None:
            self.toolbar_undo_action.setEnabled(cu)
        if self.toolbar_redo_action is not None:
            self.toolbar_redo_action.setEnabled(cr)

    def _on_info(self, info: str):
        self.info_label.setText(info)

    def _play_selected(self):
        if not self.current_tab:
            return
        tab = self.current_tab
        if (tab.current_channel
                and tab.current_channel.has_valid_url):
            cur_uid = tab.current_channel.uid
            for r in range(tab.model.rowCount()):
                ch = tab.channel_for_row(r)
                if ch and ch.uid == cur_uid:
                    tab._play_in_player(r)
                    return
        for r in range(tab.model.rowCount()):
            ch = tab.channel_for_row(r)
            if ch and ch.has_valid_url:
                tab._play_in_player(r)
                return
        info_box(self, "Нет каналов с URL в текущем фильтре",
                 "Плеер")

    def _edit_header(self):
        if self.current_tab:
            self.current_tab.edit_playlist_header()

    def _manage_group_templates(self):
        dlg = self._track_dialog(GroupTemplateListDialog(
            self.core.group_template_manager,
            playlist_tab=self.current_tab,
            parent=self))
        dlg.exec()

    def _save_current_grouping_as_template(self):
        if not self.current_tab or not self.current_tab.all_channels:
            info_box(self, "Нет открытого плейлиста с каналами")
            return
        tab = self.current_tab

        default_name = "Моя группировка"
        if tab.filepath:
            base = os.path.splitext(
                os.path.basename(tab.filepath))[0]
            if base:
                default_name = base

        name, ok = QInputDialog.getText(
            self, "Запомнить группировку",
            "Имя шаблона:", QLineEdit.EchoMode.Normal,
            default_name)
        if not ok or not name.strip():
            return
        name = name.strip()

        mgr = self.core.group_template_manager
        if mgr.exists(name):
            if not confirm(
                    self,
                    f"Шаблон «{name}» уже есть. Заменить?"):
                return

        include_only = confirm(
            self,
            "Включить только каналы с непустым group-title?")

        settings = self.core.get_replacement_settings()
        template, msg = mgr.build_from_channels(
            tab.all_channels, name, settings=settings,
            include_only_existing_groups=include_only)
        if template is None:
            warn_box(self, msg)
            return
        ok, err = mgr.save_template(template, overwrite=True)
        if not ok:
            error_box(self, err)
            return
        info_box(
            self,
            f"Шаблон «{name}» сохранён.\n\n{msg}",
            "Запомнить группировку")

    def _apply_group_template_menu(self):
        if not self.current_tab or not self.current_tab.all_channels:
            info_box(self, "Нет открытого плейлиста с каналами")
            return
        names = self.core.group_template_manager.get_names()
        if not names:
            if confirm(
                    self,
                    "Шаблонов пока нет.\n\n"
                    "Создать шаблон из текущего плейлиста?"):
                self._save_current_grouping_as_template()
            return

        chosen, ok = QInputDialog.getItem(
            self, "Применить шаблон группировки",
            "Шаблон:", names, 0, False)
        if not ok or not chosen:
            return
        self._apply_group_template_by_name(chosen)

    def _apply_group_template_by_name(self, name: str):
        tab = self.current_tab
        if tab is None or not tab.all_channels:
            info_box(self, "Нет открытого плейлиста с каналами")
            return
        template = self.core.group_template_manager.get(name)
        if template is None:
            warn_box(self, f"Шаблон «{name}» не найден")
            return

        keep_unmatched = confirm(
            self,
            f"Применить шаблон «{name}»?\n\n"
            f"Правил: {len(template.rules)}\n\n"
            "Каналы, которых нет в шаблоне:\n"
            "Да — сохранить им текущий group-title;\n"
            "Нет — присвоить «Без группы».")

        tab.save_state(f"Применение шаблона «{name}»")

        settings = self.core.get_replacement_settings()
        stats = self.core.group_template_manager.apply_to_channels(
            template, tab.all_channels, settings=settings,
            keep_unmatched=keep_unmatched)

        if stats['changed'] > 0:
            tab.sync_to_core()
            with tab._suppress_save():
                tab._rebuild_model_safely(preserve_selection=True)
            tab.update_info()
            tab.groups_changed.emit()

        info_box(
            self,
            f"Шаблон «{name}» применён.\n\n"
            f"Изменено каналов: {stats['changed']}\n"
            f"Совпало с шаблоном: {stats['matched']}\n"
            f"Не найдено в шаблоне: {stats['unmatched']}\n"
            f"Правил без каналов: {stats['missing']}",
            "Шаблоны группировки")

    def _load_epg(self):
        if self.current_tab:
            self.current_tab.load_epg_async()

    def _apply_epg_metadata_all(self):
        if not self.current_tab:
            info_box(self, "Нет открытого плейлиста")
            return
        self.current_tab.apply_epg_metadata_to_all(silent=False)

    def _remove_metadata(self):
        if not self.current_tab:
            return
        dlg = RemoveMetadataDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            opts = dlg.get_options()
            if not any(opts.values()):
                info_box(self, "Не выбрано")
                return
            self.current_tab.remove_metadata(
                opts, scope=dlg.get_scope())

    def _show_duplicates(self):
        if self.current_tab:
            self.current_tab.show_duplicate_finder()

    def _show_compare(self):
        if self.current_tab:
            self.current_tab.show_compare()

    def _apply_blacklist(self):
        if self.current_tab:
            removed = self.current_tab.apply_blacklist()
            if removed > 0:
                self.set_action(
                    f"✓ Удалено по ч.с. каналов: {removed}")

    def _apply_domain_blacklist(self):
        if not self.current_tab:
            return
        cleaned = self.current_tab._clean_blocked_by_domain()
        if cleaned > 0:
            self.set_action(
                f"✓ Очищено ссылок по ч.с. домен/IP: {cleaned}")
        else:
            info_box(self, "Совпадений не найдено")

    def _block_domain_manual(self):
        if not self.current_tab:
            info_box(self, "Нет открытого плейлиста")
            return
        from utils import URLUtils as _U
        raw, ok = QInputDialog.getText(
            self, "Заблокировать домен/IP",
            "Домен или IP (можно вставить URL):")
        if not ok or not raw.strip():
            return
        value = _U.normalize_host(raw)
        if not value:
            warn_box(self, "Не удалось разобрать домен/IP")
            return

        from dialogs import BlockDomainDialog
        tab = self.current_tab
        dlg = BlockDomainDialog(value, tab.all_channels, tab)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v, include, note, selected = dlg.get_result()
        if not v:
            return
        added = self.core.add_domain_to_blacklist(
            v, include_subdomains=include, note=note)
        if selected:
            tab._clean_blocked_by_domain()
            info_box(
                self,
                f"Правило «{v}» добавлено.\n"
                f"Очищено ссылок: {len(selected)}.\n"
                f"Каналы сохранены с метаданными.")
        else:
            info_box(self, (f"Правило «{v}» добавлено. "
                            f"Совпадений в плейлисте не найдено.")
                      if added else
                      (f"Правило «{v}» уже существует. "
                       f"Совпадений нет."))

    def _track_dialog(self, dlg: QDialog) -> QDialog:
        ref = weakref.ref(dlg)
        self._open_dialogs.append(ref)
        self._open_dialogs = [r for r in self._open_dialogs
                              if r() is not None]
        return dlg

    def _manage_blacklist(self):
        dlg = self._track_dialog(
            BlacklistDialog(self.core, self))
        dlg.exec()

    def _manage_domain_blacklist(self):
        dlg = self._track_dialog(
            DomainBlacklistDialog(self.core, self))
        dlg.exec()

    def _manage_sources(self):
        dlg = self._track_dialog(LinkSourceManagerDialog(
            self.core.link_source_manager, self.core.config,
            self))
        dlg.sources_updated.connect(
            self.core.sources_updated.emit)
        dlg.exec()

    def _auto_find_sources(self):
        dlg = SourcesFinderDialog(self.core, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        items = dlg.get_selected_items()
        if not items:
            info_box(self, "Ничего не выбрано")
            return

        mgr = self.core.link_source_manager
        existing_paths = {s.path
                          for s in mgr.get_all_sources()}
        existing_names = {s.name
                          for s in mgr.get_all_sources()}

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
            src.update_interval_hours = int(
                self.core.config.get(
                    'sources_finder_update_interval_hours',
                    SOURCES_FINDER_UPDATE_INTERVAL_HOURS))
            src.encoding = "utf-8"
            src.apply_blacklist = True
            src.apply_domain_blacklist = True
            src.generated = True

            if mgr.add_source(src):
                added += 1
                existing_paths.add(url)
                existing_names.add(name)

        self.core.sources_updated.emit()

        if added == 0:
            info_box(
                self,
                f"Новых источников не добавлено "
                f"(пропущено: {skipped}).")
            return

        auto_load = bool(self.core.config.get(
            'sources_finder_auto_load_after_add', True))
        if not auto_load:
            if not confirm(
                    self,
                    f"Добавлено источников: {added}.\n"
                    f"Пропущено (уже есть): {skipped}.\n\n"
                    f"Открыть менеджер источников и "
                    f"загрузить их в кэш?"):
                info_box(
                    self,
                    f"Источники добавлены. Обновите их "
                    f"в менеджере источников.", "Готово")
                return

        self._manage_sources()

    def _manage_replacement_settings(self):
        dlg = self._track_dialog(
            LinkReplacementSettingsDialog(
                self.core.config, self))
        dlg.exec()
        self._apply_config()

    def _manage_general_settings(self):
        dlg = GeneralSettingsDialog(self.core, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.apply()
            self._apply_config()

    def _manage_ua_rules(self):
        dlg = self._track_dialog(DomainUserAgentDialog(
            self.core.domain_user_agent_manager,
            playlist_tab=self.current_tab, parent=self))
        dlg.exec()

    def _manage_cache(self):
        dlg = CacheManagerDialog(self.core, self)
        dlg.exec()
        self._apply_config()

    def _show_log(self):
        log_path = Paths.get_log_path()
        if not os.path.exists(log_path):
            info_box(self,
                     f"Файл лога ещё не создан:\n{log_path}",
                     "Лог")
            return
        try:
            open_external(log_path)
        except Exception as e:
            error_box(self, str(e))

    def _show_help(self):
        index_path = os.path.join(
            Paths.get_help_dir(), "index.html")
        if not os.path.isfile(index_path):
            readme = os.path.join(
                Paths.get_app_dir(), "README.md")
            if os.path.isfile(readme):
                open_external(readme)
                return
            error_box(
                self,
                "Файл справки не найден.\n\n"
                f"Ожидался: {index_path}\n\n"
                "Проверьте, что папка 'help' находится рядом "
                "с исполняемым файлом.",
                "Справка")
            return
        dlg = HelpBrowser(index_path, parent=self)
        dlg.exec()

    def _show_about(self):
        HelpDialog(self).exec()

    def _show_support(self):
        SupportDialog(self).exec()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _change_font(self, delta: int):
        if not self.current_tab:
            return
        f = self.current_tab.table.font()
        size = f.pointSize()
        if size <= 0:
            size = int(self.core.config.get(
                'cell_font_size', 10))
        if delta == 0:
            new_size = int(self.core.config.get(
                'cell_font_size', 10))
        else:
            new_size = max(6, min(24, size + delta))
        if new_size == size and delta != 0:
            return
        f.setPointSize(new_size)
        self.current_tab.table.setFont(f)
        self.core.config.set('cell_font_size', new_size)
        self.core.config.save()
        self._apply_config()

    def _disconnect_app_signals(self):
        app = QApplication.instance()
        if app is not None:
            with suppress(TypeError, RuntimeError):
                app.paletteChanged.disconnect(
                    self._on_palette_changed)
        for sig, slot in (
            (self.core.settings_changed,
             self._on_settings_changed),
            (self.core.blacklist_updated,
             self._on_blacklist_updated),
            (self.core.domain_blacklist_updated,
             self._on_domain_blacklist_updated),
            (self.core.playlist_from_sources_ready,
             self._finish_create_playlist_from_sources),
            (self.core.playlist_from_sources_failed,
             self._handle_playlist_creation_error),
        ):
            with suppress(TypeError, RuntimeError):
                sig.disconnect(slot)

    def closeEvent(self, event):
        if self._playlist_creation_stop is not None:
            with suppress(Exception):
                self._playlist_creation_stop.set()

        for ref in list(self._open_dialogs):
            dlg = ref()
            if dlg is None:
                continue
            try:
                if isinstance(dlg, LinkSourceManagerDialog):
                    if (dlg._refresh_worker
                            and dlg._refresh_worker.isRunning()):
                        with suppress(Exception):
                            dlg._refresh_worker.stop()
                        dlg._refresh_worker.wait(10000)
                if isinstance(dlg, SourcesFinderDialog):
                    if (dlg._worker
                            and dlg._worker.isRunning()):
                        with suppress(Exception):
                            dlg._worker.stop()
                        dlg._worker.wait(3000)
                dlg.reject()
            except Exception:
                pass
        self._open_dialogs.clear()

        modified = [t for t in self.tabs.values() if t.modified]
        if modified:
            r = confirm_three(
                self,
                f"Есть {len(modified)} несохранённых. Сохранить?")
            if r == 'yes':
                for tab in modified:
                    if not tab.filepath:
                        fp = save_file_dialog(
                            self, "Сохранить как",
                            "playlist.m3u", M3U_FILTER, ".m3u")
                        if not fp:
                            event.ignore()
                            return
                        tab.filepath = fp
                    tab.flush_sync()
                    if not tab.save_to_file():
                        event.ignore()
                        return
            elif r == 'cancel':
                event.ignore()
                return

        for tab in self.tabs.values():
            tab.disconnect_signals()
        for tab in self.tabs.values():
            with suppress(Exception):
                tab.flush_sync()

        self._disconnect_app_signals()
        self._save_settings()

        super().closeEvent(event)
        if event.isAccepted():
            with suppress(Exception):
                app = QApplication.instance()
                if app is not None:
                    app.quit()


from cli import (  # noqa: E402,F401
    parse_cli_args, run_cli, _install_signal_handlers,
)