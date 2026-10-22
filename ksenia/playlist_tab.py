# -*- coding: utf-8 -*-
"""PlaylistTab — виджет одной вкладки плейлиста.

Версия 0.2.1:
  • Ширины столбцов хранятся в Config (а не в QSettings).
  • allow_duplicate_urls реально проверяется при вставке
    ссылки и при массовой вставке каналов.
  • limit_check_enabled / max_channels_to_check реально
    применяются при проверке.
  • Умное копирование: Ctrl+A → Ctrl+C → Ctrl+V работает
    для любого количества каналов.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import weakref
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import (
    QItemSelectionModel, QSettings, Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog,
    QDialogButtonBox, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMenu, QMessageBox, QPushButton, QTableView, QVBoxLayout,
    QWidget)

from app_core import ApplicationCore
from channel_table_model import ChannelTableModel
from constants import (
    DEFAULT_GROUP, DEFAULT_EPG_URL, EPG_ALLOWED_META_FIELDS,
    GROUP_FILTER_ALL, M3U_FILTER,
    REPLACEMENT_MAX_WORKERS_DEFAULT,
    SEARCH_DEBOUNCE_MS, STABLE_CHECK_TIMEOUT_SEC,
    STABLE_LATENCY_THRESHOLD_MS, STATE_SAVE_DEBOUNCE_MS,
    StatusText, SYNC_DEBOUNCE_MS)
from dialogs import (
    BlockDomainDialog, DuplicateFinderDialog, LinkSelectionDialog,
    MassEditDialog, PlaylistHeaderDialog, _is_qobject_valid,
    make_action)
from models import ChannelData, LinkQuality
from parsers import M3UParser, PlaylistHeaderManager
from paths import (confirm, confirm_three, error_box, info_box,
                   logger, save_file_dialog, warn_box)
from player import (EmbeddedPlayerDialog, get_mpv_error,
                    is_mpv_available)
from player_controller import PlayerController
from undo import UndoRedoManager
from utils import (URLUtils, decode_m3u_bytes, sanitize_user_agent)
from workers import (
    EPGLoaderWorker, EPGMetadataApplyWorker, LinkReplacementWorker)


OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)

_DEFAULT_COLUMN_WIDTHS: Dict[str, int] = {
    "index": 50,
    "name": 320,
    "group": 140,
    "tvg_id": 120,
    "logo": 120,
    "catchup": 100,
    "url": 420,
}


def _settings() -> QSettings:
    org = QApplication.organizationName() or "Ksenia"
    app = QApplication.applicationName() or "KseniaM3UEditor"
    return QSettings(org, app)


class PlaylistTab(QWidget):
    undo_state_changed = pyqtSignal(bool, bool)
    info_changed = pyqtSignal(str)
    groups_changed = pyqtSignal()

    def __init__(self, filepath: Optional[str] = None, parent=None,
                 parent_window=None):
        super().__init__(parent)
        self.filepath = filepath
        self.all_channels: List[ChannelData] = []
        self.current_channel: Optional[ChannelData] = None
        self.selected_channels: List[ChannelData] = []
        self.modified = False
        self._parent_window_ref = (weakref.ref(parent_window)
                                    if parent_window else None)
        self._shortcuts: List[QShortcut] = []
        self._epg_worker: Optional[EPGLoaderWorker] = None
        self._epg_meta_worker: Optional[EPGMetadataApplyWorker] = None
        self._epg_meta_generation = 0
        self._src_check_worker = None

        self._restoring_widths = False
        self._initial_widths_applied = False
        self._last_column_keys: List[str] = []

        self.core = ApplicationCore.instance()
        self.header_manager = PlaylistHeaderManager()
        self.undo_manager = UndoRedoManager()
        self.undo_manager.bind_apply_callback(self._apply_diff)

        self._replacement_worker: Optional[LinkReplacementWorker] = None
        self._loading = False
        self._suppress_state_save = False
        self._pending_sync = False
        self._needs_resort = False

        self.player_controller = PlayerController(self)

        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(SYNC_DEBOUNCE_MS)
        self._sync_timer.timeout.connect(self._do_sync)

        self._state_save_timer = QTimer(self)
        self._state_save_timer.setSingleShot(True)
        self._state_save_timer.setInterval(STATE_SAVE_DEBOUNCE_MS)
        self._state_save_timer.timeout.connect(self._do_save_state)
        self._pending_state_desc = ""

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._do_search)
        self._pending_search = ""

        self._setup_ui()
        self._setup_shortcuts()

        self.core.channels_updated.connect(
            self._on_core_channels_updated)

        self.tab_id = self.core.register_tab(self)

        if filepath and os.path.exists(filepath):
            _fp = filepath
            QTimer.singleShot(0, lambda: self._load_file(_fp))
        else:
            self.refresh_view(full_reset=True)
            QTimer.singleShot(0, self.groups_changed.emit)

    @property
    def parent_window(self):
        if self._parent_window_ref is None:
            return None
        w = self._parent_window_ref()
        if w is None or not _is_qobject_valid(w):
            return None
        return w

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        self.table = QTableView()
        self.model = ChannelTableModel(self)
        self.table.setModel(self.model)
        self._setup_table()
        layout.addWidget(self.table)
        self.model.dataChanged.connect(self._on_model_data_changed)
        self.model.request_move.connect(self._on_model_request_move)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(
            self._show_context_menu)
        self.table.selectionModel().selectionChanged.connect(
            self._on_selection_changed)

    def _setup_table(self):
        self.table.setSortingEnabled(True)
        h = self.table.horizontalHeader()
        h.setSectionsClickable(True)
        h.setSortIndicatorShown(True)

        h.blockSignals(True)
        try:
            h.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        finally:
            h.blockSignals(False)
        h.sortIndicatorChanged.connect(
            self._on_sort_indicator_changed)
        self.model.sort(0, Qt.SortOrder.AscendingOrder)

        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.EditKeyPressed)

        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setDragDropOverwriteMode(False)

        h.sectionResized.connect(self._on_section_resized)
        self._apply_resize_modes()

    # ── Ширины столбцов (в Config) ──
    @staticmethod
    def _default_column_width(key: str) -> int:
        return _DEFAULT_COLUMN_WIDTHS.get(key, 120)

    def _read_saved_widths(self) -> Dict[str, int]:
        raw = self.core.config.get('column_widths', {})
        if not isinstance(raw, dict):
            return {}
        result: Dict[str, int] = {}
        for k, v in raw.items():
            try:
                result[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return result

    def _write_saved_widths(self, widths: Dict[str, int]) -> None:
        self.core.config.set('column_widths', dict(widths))
        self.core.config.save()

    def _on_section_resized(self, logical_index: int,
                            old_size: int, new_size: int) -> None:
        if self._restoring_widths:
            return
        key = self.model.column_key(logical_index)
        if not key:
            return
        widths = self._read_saved_widths()
        if widths.get(key) == int(new_size):
            return
        widths[key] = int(new_size)
        self._write_saved_widths(widths)

    def _apply_resize_modes(self) -> None:
        h = self.table.horizontalHeader()
        h.blockSignals(True)
        try:
            n = self.model.columnCount()
            for i in range(n):
                h.setSectionResizeMode(
                    i, QHeaderView.ResizeMode.Interactive)
        finally:
            h.blockSignals(False)
        self._restore_column_widths()

    def _restore_column_widths(self) -> None:
        if self._restoring_widths:
            return
        h = self.table.horizontalHeader()
        saved = self._read_saved_widths()
        current_keys = [self.model.column_key(i)
                        for i in range(self.model.columnCount())]
        keys_changed = current_keys != self._last_column_keys
        self._last_column_keys = list(current_keys)

        self._restoring_widths = True
        h.blockSignals(True)
        try:
            for i, key in enumerate(current_keys):
                if not key:
                    continue
                width = saved.get(key)
                if width is None:
                    width = self._default_column_width(key)
                    saved[key] = width
                if width < 10:
                    width = 10
                h.resizeSection(i, int(width))
            if keys_changed or not self._initial_widths_applied:
                self._initial_widths_applied = True
        finally:
            h.blockSignals(False)
            self._restoring_widths = False
        self._write_saved_widths(saved)

    def _on_sort_indicator_changed(self, column: int,
                                    order: Qt.SortOrder):
        self.model.sort(column, order)

    def _setup_shortcuts(self):
        extra = {
            QKeySequence("Delete"): self._delete_channel,
            QKeySequence("Ctrl+Shift+Delete"):
                self._delete_selected_channels,
            QKeySequence("Ctrl+Up"): self._move_channel_up,
            QKeySequence("Ctrl+Down"): self._move_channel_down,
            QKeySequence("Ctrl+Shift+Up"): self._move_selected_up,
            QKeySequence("Ctrl+Shift+Down"): self._move_selected_down,
            QKeySequence("Ctrl+Shift+L"): self._add_link_from_current,
        }
        for k, slot in extra.items():
            sh = QShortcut(k, self)
            sh.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sh.activated.connect(slot)
            self._shortcuts.append(sh)

    def retranslate_ui(self):
        self.update_info()

    def set_search_text(self, text: str):
        self._pending_search = text or ""
        if not self._search_timer.isActive():
            self._search_timer.start()

    def _do_search(self):
        self.model.set_search_text(self._pending_search)
        self.update_info()

    def set_group_filter(self, group: str):
        self.model.set_group_filter(group)
        self.update_info()

    def set_status_filter(self, status: str):
        self.model.set_status_filter(status)
        self.update_info()

    def set_duplicate_filter(self, enabled: bool):
        self.model.set_duplicate_mode(bool(enabled))
        self.update_info()

    def _on_core_channels_updated(self, tab_id: str):
        if not _is_qobject_valid(self):
            return
        if tab_id == self.tab_id and not self._loading:
            self.update_info()

    def sync_to_core(self):
        self._pending_sync = True
        self.modified = True
        self.update_modified_status()
        self._sync_timer.start()

    def _do_sync(self):
        if not _is_qobject_valid(self) or not self._pending_sync:
            return
        self._pending_sync = False
        self.core.update_channels(self.tab_id, list(self.all_channels))

    def flush_sync(self):
        if self._sync_timer.isActive():
            self._sync_timer.stop()
        self._do_sync()

    def _normalize_order(self):
        for i, ch in enumerate(self.all_channels):
            ch.original_index = i
        self._needs_resort = False

    def _snapshot_selection(self
                             ) -> Tuple[Set[int], Optional[int], int]:
        uids = {ch.uid for ch in self.selected_channels}
        cur = (self.current_channel.uid
               if self.current_channel is not None else None)
        scroll = self.table.verticalScrollBar().value()
        return uids, cur, scroll

    def _restore_selection(self, uids: Set[int],
                           current_uid: Optional[int],
                           scroll: int) -> None:
        if not uids and current_uid is None:
            return
        sel_model = self.table.selectionModel()
        if sel_model is None:
            return
        try:
            self.table.clearSelection()
        except RuntimeError:
            return
        first_current_idx = None
        for r in range(self.model.rowCount()):
            idx = self.model.index(r, 0)
            if not idx.isValid():
                continue
            ch = self.model.channel_at(r)
            if ch is None or ch.uid not in uids:
                continue
            sel_model.select(
                idx,
                QItemSelectionModel.SelectionFlag.Select |
                QItemSelectionModel.SelectionFlag.Rows)
            if (current_uid is not None and ch.uid == current_uid
                    and first_current_idx is None):
                first_current_idx = idx
        if first_current_idx is not None:
            self.table.setCurrentIndex(first_current_idx)
        else:
            self.current_channel = None
        QTimer.singleShot(0, lambda v=scroll:
                          self.table.verticalScrollBar().setValue(v))

    def _rebuild_model_safely(self, preserve_selection: bool = True):
        saved_uids: Set[int] = set()
        saved_current: Optional[int] = None
        saved_scroll = 0
        if preserve_selection:
            saved_uids, saved_current, saved_scroll = \
                self._snapshot_selection()

        self._suppress_state_save = True
        try:
            if (self.core.config.get('preserve_original_order', True)
                    and self._needs_resort):
                self.all_channels.sort(
                    key=lambda ch: ch.original_index
                    if ch.original_index >= 0 else float('inf'))
                self._needs_resort = False
            self.model.set_channels(self.all_channels)
            self._apply_resize_modes()
        finally:
            self._suppress_state_save = False

        if preserve_selection and (saved_uids
                                    or saved_current is not None):
            self._restore_selection(saved_uids, saved_current,
                                     saved_scroll)

    def refresh_view(self, full_reset: bool = False):
        self._rebuild_model_safely(preserve_selection=not full_reset)
        if full_reset:
            self.selected_channels = []
            self.current_channel = None
        self.update_info()
        self.groups_changed.emit()

    def find_channel_by_ref(self, channel: ChannelData
                            ) -> Optional[ChannelData]:
        if channel is None:
            return None
        target = next(
            (c for c in self.all_channels if c.uid == channel.uid),
            None)
        if target is not None:
            return target
        return next(
            (c for c in self.all_channels
             if c.meta.name == channel.meta.name
             and c.original_index == channel.original_index),
            None)

    @contextmanager
    def _suppress_save(self):
        self._suppress_state_save = True
        try:
            yield
        finally:
            self._suppress_state_save = False

    @contextmanager
    def _edit_channels(self, description: str,
                       refresh: str = 'reset',
                       force_save: bool = False):
        if self._state_save_timer.isActive():
            self._state_save_timer.stop()
        self.undo_manager.save_state(self.all_channels, description)
        self._pending_state_desc = ""
        ok = False
        try:
            with self._suppress_save():
                yield self.all_channels
            ok = True
        finally:
            if ok:
                self._commit(description,
                             reset_view=(refresh == 'reset'),
                             save_state=False)
            else:
                self.refresh_view()

    def _commit(self, description: str = "",
                *, reset_view: bool = False, save_state: bool = True):
        if save_state and description:
            self.save_state(description)
        self.sync_to_core()
        with self._suppress_save():
            if reset_view:
                self._rebuild_model_safely(preserve_selection=True)
            else:
                self.model.refresh_all()
        self.update_info()
        self.groups_changed.emit()

    def _stop_workers(self):
        for attr in ('_epg_meta_worker', '_epg_worker',
                     '_replacement_worker', '_src_check_worker'):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if hasattr(w, 'isRunning') and w.isRunning():
                    with suppress(Exception):
                        w.stop()
                    with suppress(Exception):
                        w.wait(2000)
            except Exception:
                logger.exception(f"stop worker {attr}")
            setattr(self, attr, None)

    def _load_file(self, filepath: str):
        self._pending_state_desc = ""
        self._epg_meta_generation += 1
        self._stop_workers()
        self._loading = True
        self._suppress_state_save = True
        try:
            try:
                with open(filepath, 'rb') as f:
                    raw = f.read()
            except FileNotFoundError:
                error_box(self, f"Файл не найден:\n{filepath}")
                self.all_channels = []
                self.refresh_view(full_reset=True)
                return
            except PermissionError:
                error_box(self, f"Нет доступа к файлу:\n{filepath}")
                self.all_channels = []
                self.refresh_view(full_reset=True)
                return

            content, detected_enc = decode_m3u_bytes(raw)

            self.header_manager.parse_header(content)
            self.header_manager.original_encoding = detected_enc
            source_name = os.path.basename(filepath)

            settings = self.core.get_replacement_settings()
            parsed = M3UParser.parse(
                content, source_name,
                settings=settings)

            if not parsed and self.header_manager.has_extm3u:
                self.info_changed.emit(
                    "Файл содержит только заголовок, каналов нет")
            if not parsed and not self.header_manager.has_extm3u:
                warn_box(self, "Файл не похож на M3U/M3U8 плейлист.\n"
                               "Загружено 0 каналов.")

            apply_filters = bool(self.core.config.get(
                'apply_filters_on_file_open', True))
            removed_by_bl = 0
            cleaned_by_domain_bl = 0

            if apply_filters:
                try:
                    if self.core.blacklist_manager.get_all():
                        self.all_channels, removed_by_bl = \
                            self.core.apply_blacklist_to_channels(
                                parsed)
                    else:
                        self.all_channels = parsed
                except Exception:
                    logger.exception("Blacklist error")
                    self.all_channels = parsed
                try:
                    if self.core.domain_blacklist_manager.get_all():
                        self.all_channels, cleaned_by_domain_bl = \
                            self.core.apply_domain_blacklist_clean(
                                self.all_channels)
                except Exception:
                    logger.exception("Domain blacklist error")
            else:
                self.all_channels = parsed

            try:
                if self.core.domain_user_agent_manager.get_all_rules():
                    self.core.apply_domain_user_agent(
                        self.all_channels)
            except Exception:
                logger.exception("UA rules error")

            parsed_without_url = sum(
                1 for ch in parsed if not ch.has_valid_url)
            msg_parts: List[str] = []
            if apply_filters and (removed_by_bl
                                   or cleaned_by_domain_bl):
                msg_parts.append(
                    "При открытии применены фильтры:")
                msg_parts.append(
                    f"• удалено по ЧС каналов: {removed_by_bl}")
                msg_parts.append(
                    f"• очищено ссылок по ЧС домен/IP: "
                    f"{cleaned_by_domain_bl}")
                msg_parts.append(
                    "Каналы без ссылок сохранены и помечены.")
            if parsed_without_url:
                msg_parts.append(
                    f"\nКаналов без URL в исходном файле: "
                    f"{parsed_without_url} — сохранены.")
            if msg_parts:
                info_box(
                    self,
                    "\n".join(msg_parts),
                    "Фильтры и каналы без URL")

            self.undo_manager.reset(self.all_channels)
            self.modified = False
            self._needs_resort = True

            self.sync_to_core()
            self.flush_sync()

            self.refresh_view(full_reset=True)
            self.update_modified_status()
        except Exception as e:
            logger.exception("Load file error")
            self.all_channels = []
            self.modified = False
            self.refresh_view(full_reset=True)
            error_box(self, f"Не удалось загрузить:\n{e}")
        finally:
            self._suppress_state_save = False
            self._loading = False

    # ── EPG ──
    def _resolve_epg_urls_for_load(self) -> Optional[List[str]]:
        urls = [u for u in self.header_manager.epg_sources
                if u and u.strip()]
        if urls:
            return urls

        default_url = self.core.get_default_epg_url()
        if default_url:
            if confirm(
                    self,
                    "В шапке плейлиста нет EPG (url-tvg).\n\n"
                    f"Загрузить EPG по умолчанию?\n{default_url}",
                    "Загрузка EPG"):
                return [default_url]
            url, ok = QInputDialog.getText(
                self, "Загрузка EPG", "URL EPG (XMLTV):",
                QLineEdit.EchoMode.Normal, default_url)
        else:
            url, ok = QInputDialog.getText(
                self, "Загрузка EPG",
                "URL EPG (XMLTV):\n\n"
                "Встроенного EPG нет — укажите свой источник "
                "(или задайте в «Настройки → Общие»).",
                QLineEdit.EchoMode.Normal, "")
        if not ok or not url or not url.strip():
            return None
        return [url.strip()]

    def load_epg_async(self):
        if not _is_qobject_valid(self):
            return
        if self._epg_worker and self._epg_worker.isRunning():
            info_box(self, "Загрузка EPG уже идёт")
            return
        urls = self._resolve_epg_urls_for_load()
        if not urls:
            return
        w = self.parent_window
        if w is not None:
            w.set_action("⏳ Загрузка EPG...")
        from constants import EPG_LOAD_TIMEOUT_SEC
        worker = EPGLoaderWorker(
            urls, self.core.epg_db, timeout=EPG_LOAD_TIMEOUT_SEC)
        self._epg_worker = worker

        def on_progress(value, total, text):
            w2 = self.parent_window
            if w2 is not None:
                w2.show_progress(value, total, text)

        def on_loaded(count, errors):
            if self._epg_worker is worker:
                self._epg_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if not _is_qobject_valid(self):
                return
            msg = f"Загружено {count} программ"
            if errors:
                shown = errors[:5]
                msg += "\n\nОшибки:\n" + "\n".join(shown)
                if len(errors) > 5:
                    msg += f"\n... и ещё {len(errors) - 5}"
            info_box(self, msg, "EPG")
            self.core.epg_updated.emit()

        def on_error(msg):
            if self._epg_worker is worker:
                self._epg_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                error_box(self, msg, "Ошибка EPG")

        worker.progress.connect(
            on_progress, Qt.ConnectionType.QueuedConnection)
        worker.epg_loaded.connect(
            on_loaded, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(
            on_error, Qt.ConnectionType.QueuedConnection)
        worker.start()

    def apply_epg_metadata_to_all(self, silent: bool = False) -> bool:
        if not _is_qobject_valid(self):
            return False
        if not self.core.epg_db.has_channel_info:
            if not silent:
                info_box(self, "EPG не загружен (или не содержит "
                               "<channel>-записей).\n"
                               "Загрузите EPG через "
                               "«Инструменты → Загрузить EPG».",
                          "EPG")
            return False
        if not self.all_channels:
            return False
        if (self._epg_meta_worker
                and self._epg_meta_worker.isRunning()):
            with suppress(Exception):
                self._epg_meta_worker.stop()
                self._epg_meta_worker.wait(2000)
            self._epg_meta_worker = None

        self._epg_meta_generation += 1
        generation = self._epg_meta_generation

        if not silent:
            self.save_state("Применение EPG-метаданных")
        w = self.parent_window
        if w is not None and not silent:
            w.set_action("⏳ Применение EPG-метаданных...")

        worker = EPGMetadataApplyWorker(
            self.all_channels, self.core.epg_db,
            self.core.config, self.core.get_replacement_settings())
        self._epg_meta_worker = worker
        uid_map = {ch.uid: ch for ch in self.all_channels}

        def on_progress(value, total, text):
            w2 = self.parent_window
            if w2 is not None and not silent:
                w2.show_progress(value, total, text)

        def on_applied(count, results):
            if self._epg_meta_worker is worker:
                self._epg_meta_worker = None
            if not _is_qobject_valid(self):
                return
            if generation != self._epg_meta_generation:
                return
            w2 = self.parent_window
            if w2 is not None and not silent:
                w2.hide_progress()

            touched_uids: Set[int] = set()
            for uid, field, value in results:
                ch = uid_map.get(uid)
                if ch is None:
                    continue
                if field == 'epg_source':
                    if ch.meta.epg_source != value:
                        ch.meta.epg_source = value
                        touched_uids.add(uid)
                    continue
                if field == 'epg_alternative_names':
                    alts = [x for x in value.split("\x1f") if x]
                    if ch.meta.epg_alternative_names != alts:
                        ch.meta.epg_alternative_names = alts
                        touched_uids.add(uid)
                    continue
                if field not in EPG_ALLOWED_META_FIELDS:
                    continue
                if getattr(ch.meta, field) != value:
                    setattr(ch.meta, field, value)
                    touched_uids.add(uid)
            modified = 0
            for uid in touched_uids:
                ch = uid_map.get(uid)
                if ch is None:
                    continue
                ch.update_extinf()
                ch.touch()
                modified += 1

            if modified:
                if not silent:
                    self.undo_manager.save_state(
                        self.all_channels,
                        "Применение EPG-метаданных")
                self._commit(reset_view=False, save_state=False)
                if not silent:
                    info_box(self,
                             f"Обновлено каналов: {modified}",
                             "EPG")
            else:
                if not silent:
                    info_box(self, "Метаданные не изменились "
                                   "(нет совпадений или "
                                   "всё уже актуально).", "EPG")

        def on_error(msg):
            if self._epg_meta_worker is worker:
                self._epg_meta_worker = None
            w2 = self.parent_window
            if w2 is not None and not silent:
                w2.hide_progress()
            if _is_qobject_valid(self) and not silent:
                error_box(self, msg, "Ошибка EPG")

        worker.progress.connect(
            on_progress, Qt.ConnectionType.QueuedConnection)
        worker.applied.connect(
            on_applied, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(
            on_error, Qt.ConnectionType.QueuedConnection)
        worker.start()
        return True

    def update_info(self):
        st = self.core.get_stats(self.tab_id)
        parts = [
            f"Всего: {st['total']}",
            f"С URL: {st['with_url']}",
            f"Без URL: {st['without_url']}",
            f"✓ Работают: {st['working']}",
            f"✗ Не работают: {st['not_working']}",
            f"? Не проверены: {st['unknown']}",
        ]
        if st.get('unsupported'):
            parts.append(f"🟦 N/A: {st['unsupported']}")
        parts.append(f"Групп: {st['groups']}")
        info = " | ".join(parts)
        if self.model.is_duplicate_mode():
            only_name, only_url, both = self.model.duplicate_counts()
            info += (f" | 🔁 дубли: имя {only_name}, "
                     f"URL {only_url}, оба {both}")
        self.info_changed.emit(info)

    def mark_channel_status(self, uid: int, ok: bool) -> None:
        if not _is_qobject_valid(self):
            return
        ch = next((c for c in self.all_channels if c.uid == uid),
                  None)
        if ch is None:
            return
        ch.status.url_status = bool(ok)
        ch.status.url_check_time = datetime.now()
        ch.status.link_quality = (
            LinkQuality.WORKING if ok else LinkQuality.NOT_WORKING)
        ch.status.status_text = (
            StatusText.WORKING if ok else StatusText.NOT_WORKING)
        ch.touch()

        with self._suppress_save():
            try:
                for r in range(self.model.rowCount()):
                    c = self.model.channel_at(r)
                    if c is not None and c.uid == uid:
                        self.model.refresh_row(r)
                        break
            except Exception:
                logger.exception("refresh_row")

        if ch.link.url and self.core.cache_manager is not None:
            try:
                self.core.cache_manager.save_check_result(
                    ch.meta.name, ch.link.url, bool(ok),
                    ch.status.link_response_time or 0.0,
                    ch.status.status_text,
                    ch.status.status_code)
            except Exception:
                logger.exception("save_check_result")

        self.core.bump_tab_revision(self.tab_id)
        self.update_info()

    def channel_for_row(self, row: int) -> Optional[ChannelData]:
        return self.model.channel_at(row)

    def _on_selection_changed(self, selected, deselected):
        uids: Set[int] = set()
        rows: List[int] = []
        for idx in self.table.selectionModel().selectedRows():
            ch = self.model.channel_at(idx.row())
            if ch:
                uids.add(ch.uid)
                rows.append(idx.row())
        self.selected_channels = [ch for ch in self.all_channels
                                  if ch.uid in uids]
        self.current_channel = (
            self.model.channel_at(max(rows)) if rows else None)
        try:
            self.player_controller.on_table_selection_changed()
        except Exception:
            logger.exception("player_controller sync")

    def _on_model_data_changed(self, top_left, bottom_right,
                                roles=None):
        try:
            if self._suppress_state_save or self._loading:
                return
            if roles is not None and all(
                    r in (Qt.ItemDataRole.DisplayRole,
                          Qt.ItemDataRole.DecorationRole,
                          Qt.ItemDataRole.ForegroundRole,
                          Qt.ItemDataRole.BackgroundRole,
                          Qt.ItemDataRole.ToolTipRole,
                          Qt.ItemDataRole.TextAlignmentRole)
                    for r in roles):
                self.update_info()
                self.core.bump_tab_revision(self.tab_id)
                return
            self.save_state("Правка ячейки")
            self.sync_to_core()
            self.update_info()
            if self.model.is_duplicate_mode():
                self.model.set_duplicate_mode(True)
            self.groups_changed.emit()
        except RuntimeError:
            logger.warning(
                "dataChanged handler hit deleted item, ignoring")

    def save_state(self, description: str = ""):
        if self._suppress_state_save:
            return
        if self._state_save_timer.isActive():
            self._state_save_timer.stop()
            self._do_save_state()
        self._pending_state_desc = description
        self._state_save_timer.start()

    def flush_pending_state(self):
        if self._suppress_state_save:
            return
        if self._state_save_timer.isActive():
            self._state_save_timer.stop()
        self._do_save_state()

    def _do_save_state(self):
        if not _is_qobject_valid(self):
            return
        prev_can_undo = self.undo_manager.can_undo()
        prev_can_redo = self.undo_manager.can_redo()
        self.undo_manager.save_state(self.all_channels,
                                      self._pending_state_desc)
        self._pending_state_desc = ""
        cur_can_undo = self.undo_manager.can_undo()
        cur_can_redo = self.undo_manager.can_redo()
        if (cur_can_undo != prev_can_undo) or \
                (cur_can_redo != prev_can_redo):
            self.undo_state_changed.emit(cur_can_undo, cur_can_redo)

    def undo(self):
        diff = self.undo_manager.undo()
        if diff:
            with self._suppress_save():
                self._apply_diff(diff, reverse=True)
        self.undo_state_changed.emit(self.undo_manager.can_undo(),
                                      self.undo_manager.can_redo())

    def redo(self):
        diff = self.undo_manager.redo()
        if diff:
            with self._suppress_save():
                self._apply_diff(diff, reverse=False)
        self.undo_state_changed.emit(self.undo_manager.can_undo(),
                                      self.undo_manager.can_redo())

    def _apply_diff(self, diff: Dict[str, object],
                    reverse: bool = False) -> bool:
        try:
            old_order = diff.get('old_order', [])
            new_order = diff.get('new_order', [])
            added = diff.get('added', [])
            removed = diff.get('removed', [])
            changed = diff.get('changed', [])

            uid_map: Dict[int, ChannelData] = {
                ch.uid: ch for ch in self.all_channels}

            for entry in changed:
                if len(entry) < 3:
                    continue
                uid, old_d, new_d = entry[0], entry[1], entry[2]
                data = old_d if reverse else new_d
                if uid in uid_map and data:
                    ch = uid_map[uid]
                    ch.restore_from_dict(data)
                    ch.touch()

            target_order = old_order if reverse else new_order
            add_data = removed if reverse else added
            remove_uids = {
                uid for uid, _ in (added if reverse else removed)}

            existing_uids = {ch.uid for ch in self.all_channels}
            for uid, data in add_data:
                if uid not in existing_uids and data:
                    new_ch = ChannelData.from_dict(data)
                    if new_ch.uid in existing_uids:
                        new_ch.uid = ChannelData._next_uid()
                    uid_map[new_ch.uid] = new_ch
                    existing_uids.add(new_ch.uid)

            new_list: List[ChannelData] = []
            seen: Set[int] = set()
            for uid in target_order:
                if uid in remove_uids:
                    continue
                ch = uid_map.get(uid)
                if ch is None:
                    continue
                if uid not in seen:
                    new_list.append(ch)
                    seen.add(uid)

            for ch in self.all_channels:
                if ch.uid not in seen and ch.uid not in remove_uids:
                    new_list.append(ch)
                    seen.add(ch.uid)

            self.all_channels = new_list
            self._normalize_order()
            self.modified = True
            self.sync_to_core()
            self.refresh_view(full_reset=True)
            self.update_modified_status()
            return True
        except Exception:
            logger.exception("_apply_diff failed")
            return False

    def update_modified_status(self):
        self.core.update_tab_metadata(self.tab_id,
                                       modified=self.modified)
        w = self.parent_window
        if w is not None:
            w._update_window_title()

    def _play_in_player(self, row: int):
        self.player_controller.play_row(row)

    def check_urls_async(self, channels: List[ChannelData]):
        if not _is_qobject_valid(self):
            return
        worker_existing = getattr(self, '_src_check_worker', None)
        if (worker_existing is not None
                and worker_existing.isRunning()):
            info_box(self, "Проверка уже запущена")
            return
        to_check = [ch for ch in channels if ch.has_valid_url]
        if not to_check:
            info_box(self, "Нет URL для проверки")
            return

        # Лимит проверок из настроек.
        if self.core.config.get('limit_check_enabled', False):
            try:
                limit = int(self.core.config.get(
                    'max_channels_to_check', 2000))
            except (ValueError, TypeError):
                limit = 2000
            if len(to_check) > limit:
                if not confirm(
                        self,
                        f"Будет проверено только {limit} из "
                        f"{len(to_check)} каналов (лимит в "
                        f"«Настройки → Общие»). Продолжить?"):
                    return
                to_check = to_check[:limit]

        from workers import SourceUrlCheckWorker, SourceCheckConfig
        settings = self.core.get_replacement_settings()
        w = self.parent_window
        if w is not None:
            w.set_action(
                f"⏳ Проверка ссылок: {len(to_check)} каналов")

        timeout = max(int(settings.check_timeout or 3), 3)
        check_cfg = SourceCheckConfig.from_app_config(
            self.core.config)
        check_cfg = SourceCheckConfig(
            max_workers=check_cfg.max_workers,
            timeout=timeout,
            trust_sec=0,
            batch_size=check_cfg.batch_size,
            verify_ssl=check_cfg.verify_ssl,
        )
        settings_snapshot = self.core._build_settings_snapshot()
        try:
            bl_index = self.core.blacklist_manager.build_index()
        except Exception:
            logger.exception("build_index")
            bl_index = None

        worker = SourceUrlCheckWorker(
            source_name="__playlist__",
            channels=to_check,
            cache_manager=self.core.cache_manager,
            check_config=check_cfg,
            stop_token=None,
            settings_snapshot=settings_snapshot,
            blacklist_index=bl_index,
        )
        self._src_check_worker = worker

        def on_progress(name, cur, tot):
            w2 = self.parent_window
            if w2 is not None:
                w2.show_progress(cur, max(1, tot),
                                  f"Проверено: {cur}/{tot}")

        def on_done(name, working, total):
            if getattr(self, '_src_check_worker', None) is worker:
                self._src_check_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if not _is_qobject_valid(self):
                return
            with self._suppress_save():
                self.undo_manager.save_state(
                    self.all_channels, "Проверка ссылок")
            self.sync_to_core()
            with self._suppress_save():
                self.model.refresh_all()
            self.core.bump_tab_revision(self.tab_id)
            self.update_info()
            info_box(
                self,
                f"Проверка завершена.\n"
                f"Работают: {working} | "
                f"Не работают: {total - working} | "
                f"Всего проверено: {total}",
                "Проверка ссылок")

        def on_error(msg):
            if getattr(self, '_src_check_worker', None) is worker:
                self._src_check_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                error_box(self, msg, "Ошибка проверки")

        worker.source_check_progress.connect(on_progress)
        worker.source_check_done.connect(on_done)
        worker.error.connect(on_error)
        worker.start()

    def _check_selected_urls(self):
        if not self.selected_channels:
            info_box(self, "Не выбрано ни одного канала")
            return
        with_urls = [ch for ch in self.selected_channels
                     if ch.has_valid_url]
        if not with_urls:
            info_box(self, "У выбранных каналов нет URL")
            return
        self.check_urls_async(with_urls)

    def check_all_urls(self):
        with_urls = [ch for ch in self.all_channels
                     if ch.has_valid_url]
        if not with_urls:
            info_box(self, "Нет каналов с URL")
            return
        if not confirm(self, f"Проверить {len(with_urls)} ссылок?"):
            return
        self.check_urls_async(with_urls)

    def _check_single_url(self, row: int):
        ch = self.channel_for_row(row)
        if ch and ch.link.url:
            self.check_urls_async([ch])

    def _clear_urls(self, channels: List[ChannelData],
                    description: str, confirm_msg: str = "",
                    only_broken: bool = False) -> int:
        if only_broken:
            targets = [ch for ch in channels
                       if ch.status.url_status is False]
        else:
            targets = [ch for ch in channels if ch.has_valid_url]
        if not targets:
            return 0
        if confirm_msg and not confirm(self, confirm_msg):
            return 0
        with self._edit_channels(description):
            for ch in targets:
                ch.clear_url()
                ch.update_extinf()
        return len(targets)

    def _remove_broken_url(self, row: int):
        ch = self.channel_for_row(row)
        if ch is None or ch.status.url_status is not False:
            return
        self._clear_urls([ch], "Удаление битой ссылки",
                          only_broken=True)

    def _remove_selected_broken_urls(self):
        n = self._clear_urls(
            self.selected_channels, "Удаление битых ссылок",
            only_broken=True)
        if n:
            info_box(self, f"Удалено: {n}", "Успех")

    def _remove_all_broken_urls(self):
        n = self._clear_urls(
            self.all_channels, "Удаление всех битых",
            confirm_msg="Удалить все битые ссылки?",
            only_broken=True)
        if n:
            info_box(self, f"Удалено: {n}", "Успех")

    def _remove_selected_urls(self):
        if not self.selected_channels:
            return
        n = self._clear_urls(
            self.selected_channels, "Удаление ссылок у выбранных",
            confirm_msg=f"Удалить ссылки у "
                        f"{len(self.selected_channels)} каналов?")
        if n:
            info_box(self, f"Удалено: {n}", "Успех")

    def remove_all_urls(self):
        if not self.all_channels:
            return
        n = self._clear_urls(
            self.all_channels, "Удаление всех ссылок",
            confirm_msg=f"Удалить все ссылки из "
                        f"{len(self.all_channels)}?")
        if n:
            info_box(self, f"Удалено: {n}", "Успех")

    def _delete_channels_by_predicate(
            self, predicate: Callable[[ChannelData], bool],
            description: str, confirm_msg: str) -> int:
        targets = [ch for ch in self.all_channels if predicate(ch)]
        if not targets:
            return 0
        if not confirm(self, confirm_msg):
            return 0
        del_uids = {ch.uid for ch in targets}
        with self._edit_channels(description):
            self.all_channels[:] = [ch for ch in self.all_channels
                                     if ch.uid not in del_uids]
            self._normalize_order()
        return len(targets)

    def _new_channel(self):
        self.save_state("Создание нового канала")
        ch = ChannelData()
        ch.meta.name = "Новый канал"
        ch.meta.group = DEFAULT_GROUP
        ch.original_index = -1
        ch.update_extinf()
        self.all_channels.append(ch)
        self._normalize_order()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)
        row = self.model.row_of_uid(ch.uid)
        if row >= 0:
            self.table.setCurrentIndex(self.model.index(row, 1))
            self.table.edit(self.model.index(row, 1))
        self.groups_changed.emit()

    def _delete_channel(self, row: int = -1):
        if row == -1:
            if self.current_channel:
                uids = {self.current_channel.uid}
            else:
                return
        else:
            ch = self.channel_for_row(row)
            if ch is None:
                return
            uids = {ch.uid}

        n = len(uids)
        if n == 1:
            ch = next((c for c in self.all_channels
                       if c.uid in uids), None)
            msg = (f"Удалить канал '{ch.meta.name}'?" if ch
                   else "Удалить канал?")
        else:
            msg = f"Удалить выбранные {n} каналов?"
        if not confirm(self, msg):
            return
        self.save_state("Удаление канала")
        self.all_channels = [ch for ch in self.all_channels
                              if ch.uid not in uids]
        self._normalize_order()
        self.sync_to_core()
        new_uids = {ch.uid for ch in self.all_channels}
        self.selected_channels = [
            ch for ch in self.selected_channels
            if ch.uid in new_uids]
        if (self.current_channel is not None
                and self.current_channel.uid not in new_uids):
            self.current_channel = None
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)
        self.groups_changed.emit()

    def _delete_selected_channels(self):
        if not self.selected_channels:
            return
        uids = {ch.uid for ch in self.selected_channels}
        n = len(uids)
        if n == 1:
            ch = next((c for c in self.all_channels
                       if c.uid in uids), None)
            msg = (f"Удалить канал '{ch.meta.name}'?" if ch
                   else "Удалить канал?")
        else:
            msg = f"Удалить выбранные {n} каналов?"
        if not confirm(self, msg):
            return
        self.save_state("Удаление канала")
        self.all_channels = [ch for ch in self.all_channels
                              if ch.uid not in uids]
        self._normalize_order()
        self.sync_to_core()
        self.selected_channels = []
        self.current_channel = None
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=False)
        self.groups_changed.emit()

    def delete_channels_without_urls(self):
        n = self._delete_channels_by_predicate(
            lambda ch: not ch.has_valid_url,
            "Удаление без ссылок",
            "Удалить каналы без ссылок?")
        if n:
            info_box(self, f"Удалено: {n}", "Успех")

    def delete_channels_without_metadata(self):
        n = self._delete_channels_by_predicate(
            lambda ch: not self._has_any_metadata(ch),
            "Удаление без метаданных",
            "Удалить каналы без метаданных?")
        if n:
            info_box(self, f"Удалено: {n}", "Успех")

    @staticmethod
    def _has_any_metadata(ch: ChannelData) -> bool:
        return bool(ch.meta.tvg_id or ch.meta.tvg_logo
                    or ch.meta.tvg_name
                    or (ch.meta.group
                        and ch.meta.group != DEFAULT_GROUP)
                    or ch.link.user_agent)

    def block_domain_from_channel(self, channel: ChannelData):
        if not channel:
            info_box(self, "Канал не выбран")
            return
        target = self.find_channel_by_ref(channel)
        if target is None:
            info_box(self, f"Канал «{channel.meta.name}» "
                            f"не найден в плейлисте.")
            return
        if not target.has_valid_url:
            info_box(self, f"У канала «{target.meta.name}» нет URL.")
            return
        host = URLUtils.extract_host(target.link.url)
        if not host:
            info_box(self, f"Не удалось определить домен/IP "
                            f"из URL:\n{target.link.url[:120]}")
            return

        dlg = BlockDomainDialog(host, self.all_channels, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value, include, note, selected = dlg.get_result()
        if not value:
            return
        added = self.core.add_domain_to_blacklist(
            value, include_subdomains=include, note=note)
        if not selected and not added:
            info_box(self, f"Правило для «{value}» уже существует, "
                            f"совпадений нет.")
        elif not selected:
            info_box(self, f"Правило «{value}» добавлено. "
                            f"Совпадений в плейлисте не найдено.")
        else:
            self._clean_blocked_by_domain()
            info_box(self, f"Правило «{value}» добавлено.\n"
                            f"Очищено ссылок: {len(selected)}.\n"
                            f"Каналы сохранены с метаданными.")

    def _clean_blocked_by_domain(self) -> int:
        if not self.all_channels:
            return 0
        mgr = self.core.domain_blacklist_manager
        if not mgr.get_all():
            return 0
        blocked_count = sum(
            1 for ch in self.all_channels
            if ch.link.url and mgr.matches_url(ch.link.url))
        if blocked_count == 0:
            return 0
        self.flush_pending_state()
        self.save_state("Очистка ссылок по ЧС домен/IP")
        _, cleaned = self.core.apply_domain_blacklist_clean(
            self.all_channels)
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()
        self.core.bump_tab_revision(self.tab_id)
        self.update_info()
        return cleaned

    def _add_to_blacklist(self, row: int = -1):
        if row == -1:
            if not self.current_channel:
                return
            ch = self.current_channel
        else:
            ch = self.channel_for_row(row)
            if ch is None:
                return
        dlg = QDialog(self)
        dlg.setWindowTitle("Добавить в чёрный список каналов")
        dlg.resize(400, 200)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(f"Канал: {ch.meta.name}"))
        l.addWidget(QLabel(f"TVG-ID: {ch.meta.tvg_id}"))
        g = QGroupBox("Параметры")
        gl = QVBoxLayout(g)
        nc = QCheckBox("По названию")
        nc.setChecked(True)
        gl.addWidget(nc)
        tc = QCheckBox("По TVG-ID")
        tc.setChecked(bool(ch.meta.tvg_id))
        gl.addWidget(tc)
        l.addWidget(g)
        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = ch.meta.name if nc.isChecked() else ""
        tid = ch.meta.tvg_id if tc.isChecked() else ""
        if not name and not tid:
            return
        self.core.add_to_blacklist(name, tid)

    def _add_selected_to_blacklist(self):
        if not self.selected_channels:
            return
        chs = self.selected_channels.copy()
        dlg = QDialog(self)
        dlg.setWindowTitle("Добавить в чёрный список каналов")
        dlg.resize(450, 300)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(f"Добавить {len(chs)} каналов:"))
        lw = QListWidget()
        for c in chs[:100]:
            lw.addItem(f"{c.meta.name} [{c.meta.tvg_id}]"
                       if c.meta.tvg_id else c.meta.name)
        if len(chs) > 100:
            lw.addItem(f"... и ещё {len(chs) - 100}")
        l.addWidget(lw)
        g = QGroupBox("Параметры")
        gl = QVBoxLayout(g)
        nc = QCheckBox("По названию")
        nc.setChecked(True)
        gl.addWidget(nc)
        tc = QCheckBox("По TVG-ID")
        tc.setChecked(True)
        gl.addWidget(tc)
        l.addWidget(g)
        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not nc.isChecked() and not tc.isChecked():
            return
        added = 0
        for c in chs:
            name = c.meta.name if nc.isChecked() else ""
            tid = (c.meta.tvg_id
                   if tc.isChecked() and c.meta.tvg_id else "")
            if not name and not tid:
                continue
            if self.core.add_to_blacklist(name, tid):
                added += 1
        if added > 0:
            info_box(self, f"Добавлено: {added}", "Успех")

    def _edit_user_agent(self, row: int):
        ch = self.channel_for_row(row)
        if not ch:
            return
        new_ua, ok = QInputDialog.getText(
            self, "Правка User Agent", "User Agent:",
            QLineEdit.EchoMode.Normal, ch.link.user_agent or "")
        if not ok:
            return
        self.save_state("Изменение User Agent")
        ua = sanitize_user_agent(new_ua)
        ch.link.user_agent = ua
        if ua:
            ch.link.extra_headers['User-Agent'] = ua
        else:
            for k in list(ch.link.extra_headers.keys()):
                if k.lower() == 'user-agent':
                    del ch.link.extra_headers[k]
        ch.update_extvlcopt_from_headers()
        ch.touch()
        self.sync_to_core()
        for r in range(self.model.rowCount()):
            c = self.model.channel_at(r)
            if c is not None and c.uid == ch.uid:
                self.model.refresh_row(r)
                break

    def _mass_edit_selected(self):
        if not self.selected_channels:
            info_box(self, "Не выбрано ни одного канала")
            return
        dlg = MassEditDialog(len(self.selected_channels), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        changes = dlg.get_changes()
        if not changes:
            return
        self.save_state("Массовая правка")
        for ch in self.selected_channels:
            if 'user_agent' in changes:
                ua = sanitize_user_agent(changes['user_agent'])
                ch.link.user_agent = ua
                if ua:
                    ch.link.extra_headers['User-Agent'] = ua
                else:
                    for k in list(ch.link.extra_headers.keys()):
                        if k.lower() == 'user-agent':
                            del ch.link.extra_headers[k]
                ch.update_extvlcopt_from_headers()
            if 'tvg_id' in changes:
                ch.meta.tvg_id = changes['tvg_id']
            if 'tvg_logo' in changes:
                ch.meta.tvg_logo = changes['tvg_logo']
            if 'group' in changes:
                ch.meta.group = changes['group'] or DEFAULT_GROUP
            ch.update_extinf()
            ch.touch()
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()
        self.groups_changed.emit()
        info_box(self, f"Обновлено {len(self.selected_channels)} "
                        f"каналов", "Готово")

    # ── Copy/Cut/Paste ──
    # FIX 0.2.1: умное копирование — Ctrl+A → Ctrl+C → Ctrl+V
    # работает для любого количества каналов.
    def _copy_channel(self):
        """Умное копирование: если выделено >1 канала —
        копируем всё выделение, иначе — текущий канал.

        Ctrl+C вызывает именно этот метод.
        """
        w = self.parent_window
        if w is None:
            return
        if len(self.selected_channels) > 1:
            self._copy_selected_channels()
            w.set_action(
                f"✓ Скопировано каналов: "
                f"{len(self.selected_channels)}")
        elif self.current_channel is not None:
            w.copied_channel = self.current_channel.copy()
            w.copied_channels = None
            w.set_action("✓ Скопирован 1 канал")
        else:
            w.copied_channel = None
            w.copied_channels = None

    def _cut_channel(self):
        if not self.current_channel and not self.selected_channels:
            return
        if len(self.selected_channels) > 1:
            self._copy_selected_channels()
            self._delete_selected_channels()
            return
        if not self.current_channel:
            return
        target = self.current_channel
        self._copy_channel()
        uids = {target.uid}
        if not confirm(self, f"Вырезать канал '{target.meta.name}'?"):
            return
        self.save_state("Вырезание канала")
        self.all_channels = [ch for ch in self.all_channels
                              if ch.uid not in uids]
        self._normalize_order()
        self.sync_to_core()
        self.selected_channels = []
        self.current_channel = None
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=False)
        self.groups_changed.emit()

    def _cut_selected_channels(self):
        if self.selected_channels:
            self._copy_selected_channels()
            self._delete_selected_channels()

    def _copy_selected_channels(self):
        w = self.parent_window
        if self.selected_channels and w is not None:
            w.copied_channels = [ch.copy()
                                  for ch in self.selected_channels]
            w.copied_channel = None

    def _paste_channel(self, row: int = -1):
        """Умная вставка: если в буфере список — вставляем
        весь список, иначе — один канал.

        Ctrl+V вызывает именно этот метод.
        """
        w = self.parent_window
        if w is None:
            return

        if w.copied_channels:
            self._paste_selected_channels(row)
            return

        if not w.copied_channel:
            return

        self.save_state("Вставка канала")
        ch = w.copied_channel.copy()
        ch.original_index = -1

        if (ch.link.url
                and not self.core.config.get(
                    'allow_duplicate_urls', False)):
            for existing in self.all_channels:
                if existing.link.url == ch.link.url:
                    warn_box(
                        self,
                        f"URL уже используется каналом "
                        f"«{existing.meta.name}».\n\n"
                        f"Включите «Разрешать одинаковые URL "
                        f"у разных каналов» в общих настройках, "
                        f"если это нужно.")
                    return

        ref_channel = (self.channel_for_row(row)
                       if row >= 0 else None)
        if ref_channel is None:
            ref_channel = self.current_channel

        if (ref_channel is not None
                and ref_channel in self.all_channels):
            idx = self.all_channels.index(ref_channel) + 1
            self.all_channels.insert(idx, ch)
        else:
            self.all_channels.append(ch)

        self._normalize_order()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)
        self.groups_changed.emit()

    def _paste_selected_channels(self, row: int = -1):
        w = self.parent_window
        if w is None or not w.copied_channels:
            return
        self.save_state("Вставка каналов")

        ref_channel = (self.channel_for_row(row)
                       if row >= 0 else None)
        if ref_channel is None:
            ref_channel = self.current_channel

        if (ref_channel is not None
                and ref_channel in self.all_channels):
            base_idx = self.all_channels.index(ref_channel) + 1
        else:
            base_idx = len(self.all_channels)

        # FIX 0.2.1: проверка allow_duplicate_urls для
        # массовой вставки.
        allow_dup = bool(self.core.config.get(
            'allow_duplicate_urls', False))
        if not allow_dup:
            existing_urls = {
                ch.link.url for ch in self.all_channels
                if ch.link.url
            }
            conflicts: List[str] = []
            for ch in w.copied_channels:
                url = ch.link.url
                if url and url in existing_urls:
                    conflicts.append(
                        f"«{ch.meta.name}» → {url[:60]}")
            if conflicts:
                warn_box(
                    self,
                    "В буфере есть каналы с URL, которые уже "
                    "используются в этом плейлисте:\n\n"
                    + "\n".join(conflicts[:10])
                    + (f"\n… и ещё {len(conflicts) - 10}"
                       if len(conflicts) > 10 else "")
                    + "\n\nВключите «Разрешать одинаковые URL "
                      "у разных каналов» в общих настройках, "
                      "если это нужно.")
                return

        for offset, ch in enumerate(w.copied_channels):
            new = ch.copy()
            new.original_index = -1
            self.all_channels.insert(base_idx + offset, new)

        self._normalize_order()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)
        self.groups_changed.emit()

        w.set_action(
            f"✓ Вставлено каналов: {len(w.copied_channels)}")

    def _on_model_request_move(self, uids: List[int],
                                ref_uid: Optional[int]):
        if not uids:
            return
        moving_set = set(uids)
        moving = [ch for ch in self.all_channels
                  if ch.uid in moving_set]
        if not moving:
            return
        if ref_uid is not None and ref_uid in moving_set:
            return

        ref_channel = None
        if ref_uid is not None:
            ref_channel = next(
                (ch for ch in self.all_channels
                 if ch.uid == ref_uid), None)

        if self._state_save_timer.isActive():
            self._state_save_timer.stop()
            self._do_save_state()

        remaining = [ch for ch in self.all_channels
                     if ch.uid not in moving_set]

        if ref_channel is not None:
            if ref_channel not in remaining:
                return
            insert_idx = remaining.index(ref_channel)
        else:
            insert_idx = len(remaining)

        self.all_channels = (remaining[:insert_idx] + moving
                              + remaining[insert_idx:])

        h = self.table.horizontalHeader()
        if h.sortIndicatorSection() != 0:
            h.blockSignals(True)
            h.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
            h.blockSignals(False)

        self._normalize_order()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)

        self._pending_state_desc = \
            "Перемещение каналов (drag & drop)"
        self._do_save_state()

        self._restore_selection(
            moving_set, None,
            self.table.verticalScrollBar().value())

    def _rename_groups(self):
        if not self.selected_channels:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Пакетное переименование")
        dlg.resize(400, 200)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(
            f"Выбрано: {len(self.selected_channels)}"))
        groups = {ch.meta.group for ch in self.selected_channels}
        cur = (next(iter(groups)) if len(groups) == 1
               else "РАЗНЫЕ")
        l.addWidget(QLabel(f"<b>{cur}</b>"))
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        l.addWidget(sep)
        l.addWidget(QLabel("Новая группа:"))
        edit = QLineEdit()
        if len(groups) == 1:
            edit.setText(cur)
        edit.setPlaceholderText("Введите название")
        l.addWidget(edit)
        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ng = edit.text().strip()
        if not ng or ng == cur:
            return
        with self._edit_channels("Переименование групп"):
            for ch in self.selected_channels:
                ch.meta.group = ng
                ch.update_extinf()

    def _move_channel_up(self, row: int = -1):
        ch = (self.current_channel if row == -1
              else self.channel_for_row(row))
        if ch is None:
            return
        try:
            idx = self.all_channels.index(ch)
        except ValueError:
            return
        if idx > 0:
            with self._edit_channels("Перемещение вверх"):
                (self.all_channels[idx],
                 self.all_channels[idx - 1]) = \
                    (self.all_channels[idx - 1],
                     self.all_channels[idx])
                self._normalize_order()

    def _move_channel_down(self, row: int = -1):
        ch = (self.current_channel if row == -1
              else self.channel_for_row(row))
        if ch is None:
            return
        try:
            idx = self.all_channels.index(ch)
        except ValueError:
            return
        if idx < len(self.all_channels) - 1:
            with self._edit_channels("Перемещение вниз"):
                (self.all_channels[idx],
                 self.all_channels[idx + 1]) = \
                    (self.all_channels[idx + 1],
                     self.all_channels[idx])
                self._normalize_order()

    def _move_selected_up(self):
        if not self.selected_channels:
            return
        indices = []
        for ch in self.selected_channels:
            try:
                indices.append(self.all_channels.index(ch))
            except ValueError:
                continue
        if not indices:
            return
        indices.sort()
        self.save_state("Перемещение вверх")
        for i, idx in enumerate(indices):
            real_idx = idx - i
            if real_idx > 0:
                (self.all_channels[real_idx],
                 self.all_channels[real_idx - 1]) = \
                    (self.all_channels[real_idx - 1],
                     self.all_channels[real_idx])
        self._normalize_order()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)

    def _move_selected_down(self):
        if not self.selected_channels:
            return
        indices = []
        for ch in self.selected_channels:
            try:
                indices.append(self.all_channels.index(ch))
            except ValueError:
                continue
        if not indices:
            return
        indices.sort(reverse=True)
        self.save_state("Перемещение вниз")
        last = len(self.all_channels) - 1
        for i, idx in enumerate(indices):
            real_idx = idx + i
            if real_idx < last:
                (self.all_channels[real_idx],
                 self.all_channels[real_idx + 1]) = \
                    (self.all_channels[real_idx + 1],
                     self.all_channels[real_idx])
        self._normalize_order()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)

    def _apply_alternative_url(self, channel: ChannelData, url: str):
        if not url or not url.strip():
            return
        self.save_state("Применение альтернативной ссылки")
        if (channel.link.url and channel.link.url != url
                and channel.link.url
                not in channel.link.alternative_urls):
            channel.link.alternative_urls.append(channel.link.url)
        if url in channel.link.alternative_urls:
            channel.link.alternative_urls.remove(url)
        channel.link.url = url
        channel.link.has_url = True
        channel.status.reset()
        try:
            self.core.domain_user_agent_manager\
                .apply_rules_to_channel(channel)
        except Exception:
            logger.exception("apply_rules_to_channel")
        channel.touch()
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()

    def _add_link_from_current(self):
        if not self.current_channel:
            return
        for r in range(self.model.rowCount()):
            ch = self.channel_for_row(r)
            if ch is self.current_channel:
                self._add_link_from_sources(r)
                return
        for r in range(self.model.rowCount()):
            ch = self.channel_for_row(r)
            if ch and ch.uid == self.current_channel.uid:
                self._add_link_from_sources(r)
                return

    def _add_link_from_sources(self, row: int):
        ch = self.channel_for_row(row)
        if ch is None:
            return
        if ch.has_valid_url:
            info_box(self, f"'{ch.meta.name}' уже имеет ссылку.")
            return
        settings = self.core.get_replacement_settings()
        alts = self.core.link_source_manager.find_matches_for_channel(
            ch, settings,
            config=self.core.config,
            only_working=False,
            epg_db=self.core.epg_db)
        if not alts:
            info_box(self, f"Не найдено для '{ch.meta.name}'.")
            return
        dlg = LinkSelectionDialog(ch.meta.name, alts, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.get_selected()
        if selected:
            self._do_apply_link(ch, selected)

    def _do_apply_link(self, channel: ChannelData,
                        source_ch: ChannelData):
        if not source_ch.has_valid_url:
            return
        if not self.core.config.get('allow_duplicate_urls', False):
            target_url = source_ch.link.url
            for ch in self.all_channels:
                if ch.uid != channel.uid and ch.link.url == target_url:
                    warn_box(
                        self,
                        f"URL уже используется каналом "
                        f"«{ch.meta.name}».\n\n"
                        f"Включите «Разрешать одинаковые URL "
                        f"у разных каналов» в общих настройках, "
                        f"если это нужно.")
                    return
        self.save_state("Добавление ссылки из источников")
        if (channel.link.url
                and channel.link.url != source_ch.link.url):
            if channel.link.url not in channel.link.alternative_urls:
                channel.link.alternative_urls.append(
                    channel.link.url)
        channel.link.url = source_ch.link.url
        channel.link.has_url = True
        channel.status.reset()
        channel.touch()
        channel.link.link_source = source_ch.link.link_source
        if source_ch.link.user_agent:
            if self.core.should_remove_user_agent(channel.link.url):
                channel.link.user_agent = ""
                for k in list(channel.link.extra_headers.keys()):
                    if k.lower() == 'user-agent':
                        del channel.link.extra_headers[k]
            else:
                channel.link.user_agent = source_ch.link.user_agent
                channel.link.extra_headers['User-Agent'] = \
                    source_ch.link.user_agent
        else:
            ua = self.core.get_user_agent_for_url(channel.link.url)
            if ua:
                channel.link.user_agent = ua
                channel.link.extra_headers['User-Agent'] = ua
        channel.update_extinf()
        channel.update_extvlcopt_from_headers()
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()

    def _replace_link_from_sources(self, row: int):
        ch = self.channel_for_row(row)
        if ch is None:
            return
        settings = self.core.get_replacement_settings()
        alts = self.core.link_source_manager.find_matches_for_channel(
            ch, settings,
            config=self.core.config,
            only_working=False,
            epg_db=self.core.epg_db)
        if not alts:
            info_box(self, f"Не найдено для '{ch.meta.name}'.")
            return
        dlg = LinkSelectionDialog(ch.meta.name, alts, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.get_selected()
        if selected:
            self._do_apply_link(ch, selected)

    def replace_selected_links(self):
        if not self.selected_channels:
            return
        settings = self.core.get_replacement_settings()
        candidates: List[Tuple[ChannelData,
                                List[ChannelData]]] = []
        for ch in self.selected_channels:
            alts = self.core.link_source_manager \
                .find_matches_for_channel(
                    ch, settings,
                    config=self.core.config,
                    only_working=False,
                    epg_db=self.core.epg_db)
            if alts:
                candidates.append((ch, alts))
        if not candidates:
            info_box(self, "Не найдено альтернатив")
            return
        all_alts: List[ChannelData] = []
        seen_urls: Set[str] = set()
        for _, alts in candidates:
            for a in alts:
                if a.link.url and a.link.url not in seen_urls:
                    seen_urls.add(a.link.url)
                    all_alts.append(a)
        if not all_alts:
            return
        dlg = LinkSelectionDialog(
            f"{len(candidates)} каналов", all_alts, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.get_selected()
        if not selected:
            return
        self.save_state("Замена выбранных ссылок")
        applied = 0
        for ch, _ in candidates:
            if (not ch.has_valid_url
                    or ch.link.url == selected.link.url):
                continue
            if ch.link.url not in ch.link.alternative_urls:
                ch.link.alternative_urls.append(ch.link.url)
            ch.link.url = selected.link.url
            ch.link.has_url = True
            ch.status.reset()
            ch.touch()
            if selected.link.user_agent:
                ch.link.user_agent = selected.link.user_agent
                ch.link.extra_headers['User-Agent'] = \
                    selected.link.user_agent
            ch.update_extinf()
            ch.update_extvlcopt_from_headers()
            applied += 1
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()
        info_box(self, f"Заменено у {applied} каналов", "Готово")

    def replace_single_link(self, row: int):
        self._replace_link_from_sources(row)

    def replace_all_links(self):
        if not self.all_channels:
            return

        sources = self.core.link_source_manager.get_enabled_sources()
        if not sources:
            info_box(
                self,
                "Нет включённых источников.\n\n"
                "Откройте «Инструменты → Источники ссылок» "
                "и добавьте\n"
                "хотя бы один источник. Затем нажмите "
                "«🔄 Обновить всё»,\n"
                "чтобы загрузить каналы и наполнить кэш "
                "живых URL.",
                "Замена ссылок")
            return

        settings = self.core.get_replacement_settings()
        to_process = [ch for ch in self.all_channels
                      if ch.needs_replacement(settings)]
        if not to_process:
            info_box(self, "Нет каналов для замены.")
            return

        if not self.core.link_source_manager.has_alive_index():
            reply = confirm_three(
                self,
                "Кэш живых URL источников пуст.\n\n"
                "Автозамена работает ИСКЛЮЧИТЕЛЬНО из кэша "
                "(без сетевых проверок).\n\n"
                "Нажмите «Да», чтобы открыть менеджер "
                "источников и запустить «🔄 Обновить всё».\n"
                "«Нет» — продолжить замену (вероятно, "
                "0 результатов).\n"
                "«Отмена» — прервать.",
                "Кэш пуст")
            if reply == 'yes':
                w = self.parent_window
                if w is not None:
                    w._manage_sources()
                return
            if reply == 'cancel':
                return

        self.replace_links_async(to_process)

    def replace_links_async(self, channels: List[ChannelData]):
        if not _is_qobject_valid(self):
            return
        if (self._replacement_worker
                and self._replacement_worker.isRunning()):
            info_box(self, "Замена уже запущена")
            return
        settings = self.core.get_replacement_settings()
        to_process = [ch for ch in channels
                      if ch.needs_replacement(settings)]
        if not to_process:
            info_box(self, "Нет каналов для замены.")
            return
        w = self.parent_window
        if w is not None:
            w.set_action(f"⏳ Замена ссылок (из кэша): "
                          f"{len(to_process)} каналов")
        max_workers = int(self.core.config.get(
            'replacement_max_workers',
            REPLACEMENT_MAX_WORKERS_DEFAULT))
        self._replacement_worker = LinkReplacementWorker(
            to_process, self.core.link_source_manager, settings,
            config=self.core.config, max_workers=max_workers,
            epg_db=self.core.epg_db)

        uid_map: Dict[int, ChannelData] = {
            ch.uid: ch for ch in self.all_channels}

        def on_channel_updated(uid: int, old_url: str,
                                new_url: str, name: str,
                                from_alive: bool):
            if not _is_qobject_valid(self):
                return
            ch = uid_map.get(uid)
            if ch is None:
                return
            s = settings
            if s.keep_backup_links and old_url and old_url.strip():
                if old_url not in ch.link.alternative_urls:
                    ch.link.alternative_urls.insert(0, old_url)
                    ch.link.alternative_urls = \
                        ch.link.alternative_urls[
                            :s.max_alternative_urls]
            ch.link.url = new_url
            ch.link.has_url = True
            ch.status.reset()
            try:
                self.core.domain_user_agent_manager\
                    .apply_rules_to_channel(ch)
                ch.update_extvlcopt_from_headers()
            except Exception:
                logger.exception("apply UA after replacement")
            ch.touch()

        def on_progress(value, maximum, text):
            w2 = self.parent_window
            if w2 is not None:
                w2.show_progress(value, maximum, text)

        def on_done(replaced, total):
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if not _is_qobject_valid(self):
                return
            with self._suppress_save():
                self.undo_manager.save_state(
                    self.all_channels, "Замена ссылок")
            self.sync_to_core()
            with self._suppress_save():
                self.model.refresh_all()
            self.undo_state_changed.emit(
                self.undo_manager.can_undo(),
                self.undo_manager.can_redo())
            if replaced == 0:
                info_box(
                    self,
                    "Заменено 0 из " + str(total) + ".\n\n"
                    "Автозамена работает только из кэша "
                    "url_status_cache.\n"
                    "Возможные причины:\n"
                    "• источники не обновлялись с проверкой "
                    "URL;\n"
                    "• живых URL нет в кэше;\n"
                    "• имена каналов не совпадают.\n\n"
                    "Откройте «Инструменты → Источники ссылок» "
                    "и нажмите\n"
                    "«🔄 Обновить всё», чтобы наполнить кэш.",
                    "Замена завершена")
            else:
                info_box(self, f"Заменено {replaced} из {total}",
                          "Замена завершена")
            self._replacement_worker = None

        def on_error(msg):
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                error_box(self, msg, "Ошибка замены")
            self._replacement_worker = None

        self._replacement_worker.channel_updated.connect(
            on_channel_updated, Qt.ConnectionType.QueuedConnection)
        self._replacement_worker.progress.connect(
            on_progress, Qt.ConnectionType.QueuedConnection)
        self._replacement_worker.replacement_done.connect(
            on_done, Qt.ConnectionType.QueuedConnection)
        self._replacement_worker.error.connect(
            on_error, Qt.ConnectionType.QueuedConnection)
        self._replacement_worker.start()

    def edit_playlist_header(self):
        old_snapshot = self.header_manager.copy()
        dlg = PlaylistHeaderDialog(self.header_manager, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        hm = dlg.header_manager
        changed = (
            old_snapshot.header_lines != hm.header_lines
            or old_snapshot.epg_sources != hm.epg_sources
            or old_snapshot.custom_attributes != hm.custom_attributes
            or old_snapshot.playlist_name != hm.playlist_name
        )
        if not changed:
            return
        self.save_state('Изменение заголовка плейлиста')
        self.header_manager.header_lines = list(hm.header_lines)
        self.header_manager.epg_sources = list(hm.epg_sources)
        self.header_manager.custom_attributes = \
            dict(hm.custom_attributes)
        self.header_manager.playlist_name = hm.playlist_name
        self.header_manager.has_extm3u = hm.has_extm3u
        self.header_manager._original_attrs = \
            dict(hm._original_attrs)
        self.modified = True
        self.update_modified_status()

    def apply_blacklist(self) -> int:
        filtered, removed = self.core.apply_blacklist_to_channels(
            self.all_channels)
        if removed > 0:
            with self._edit_channels("Применение ч.с. каналов"):
                self.all_channels[:] = filtered
                self._normalize_order()
        return removed

    def remove_metadata(self, opts: Dict[str, bool],
                        scope: str = "all"):
        if scope == "current":
            if not self.current_channel:
                return
            chs = [self.current_channel]
        elif scope == "selected":
            if not self.selected_channels:
                return
            chs = self.selected_channels.copy()
        else:
            chs = self.all_channels.copy()
        if not chs:
            return
        chs = [ch for ch in chs
               if self._has_meta_to_remove(ch, opts)]
        if not chs:
            info_box(self,
                     "Нет каналов с указанными метаданными")
            return
        if not confirm(self, f"Удалить из {len(chs)} каналов?"):
            return
        self.save_state("Удаление метаданных")
        for ch in chs:
            if opts.get('tvg_id'):
                ch.meta.tvg_id = ""
            if opts.get('tvg_logo'):
                ch.meta.tvg_logo = ""
            if opts.get('tvg_name'):
                ch.meta.tvg_name = ""
            if opts.get('group_title'):
                ch.meta.group = DEFAULT_GROUP
            if opts.get('user_agent'):
                ch.link.user_agent = ""
                for k in list(ch.link.extra_headers.keys()):
                    if k.lower() == 'user-agent':
                        del ch.link.extra_headers[k]
                ch.update_extvlcopt_from_headers()
            ch.update_extinf()
            ch.touch()
        self.sync_to_core()
        with self._suppress_save():
            self._rebuild_model_safely(preserve_selection=True)
        self.groups_changed.emit()

    @staticmethod
    def _has_meta_to_remove(ch: ChannelData,
                             opts: Dict[str, bool]) -> bool:
        checks = (
            ('tvg_id', bool(ch.meta.tvg_id)),
            ('tvg_logo', bool(ch.meta.tvg_logo)),
            ('tvg_name', bool(ch.meta.tvg_name)),
            ('group_title', bool(
                ch.meta.group
                and ch.meta.group != DEFAULT_GROUP)),
            ('user_agent', bool(ch.link.user_agent)),
        )
        return any(opts.get(k) and has for k, has in checks)

    def save_changes(self):
        w = self.parent_window
        if w is not None:
            w._save_file()
        else:
            if not self.filepath:
                fp = save_file_dialog(
                    self, "Сохранить как", "playlist.m3u",
                    M3U_FILTER, ".m3u")
                if not fp:
                    return
                self.filepath = fp
            self.save_to_file()

    def save_to_file(self, filepath: Optional[str] = None) -> bool:
        if filepath:
            self.filepath = filepath
        if not self.filepath:
            return False
        try:
            self.flush_sync()
            if (os.path.exists(self.filepath)
                    and self.core.config.get(
                        'auto_backup_before_save', True)):
                with suppress(OSError):
                    shutil.copy2(self.filepath,
                                  self.filepath + '.bak')
            enc = self.header_manager.original_encoding or 'utf-8'
            if enc not in ('utf-8', 'utf-8-sig'):
                reply = QMessageBox.question(
                    self, "Кодировка",
                    f"Файл был загружен в кодировке {enc}.\n"
                    f"Сохранить в UTF-8 (рекомендуется)?",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No)
                if reply == QMessageBox.StandardButton.Yes:
                    enc = 'utf-8'
            with open(self.filepath, 'w', encoding=enc) as f:
                f.write(self._render_playlist_text())
            self.modified = False
            self.core.update_tab_metadata(self.tab_id,
                                            filepath=self.filepath)
            with suppress(Exception):
                ssm = self.core.stable_state_manager
                settings = self.core.get_replacement_settings()
                for ch in self.all_channels:
                    key = ch.stable_key(settings)
                    if not ssm.get(key) and ch.link.url:
                        ssm.set(key, ch.link.url)
                ssm.save()
            self.update_modified_status()
            return True
        except Exception as e:
            error_box(self, f"Не удалось сохранить:\n{e}")
            return False

    def _render_playlist_text(self) -> str:
        parts: List[str] = []
        ht = self.header_manager.get_header_text()
        if ht:
            parts.append(ht.rstrip('\n'))
            parts.append('')
        save_extvlcopt = self.core.config.get(
            'save_extvlcopt', True)
        for ch in self.all_channels:
            parts.append(ch.link.extinf)
            if save_extvlcopt:
                parts.extend(ch.link.extvlcopt_lines)
            else:
                if ch.link.user_agent:
                    parts.append(
                        f'#EXTVLCOPT:http-user-agent='
                        f'"{ch._escape(ch.link.user_agent)}"')
                for k, v in ch.link.extra_headers.items():
                    if k.lower() == 'user-agent':
                        continue
                    if k.lower() == 'referer':
                        parts.append(
                            f'#EXTVLCOPT:http-referrer='
                            f'"{ch._escape(v)}"')
                    else:
                        parts.append(
                            f'#EXTVLCOPT:http-header='
                            f'"{k}: {ch._escape(v)}"')
            parts.append(ch.link.url or '')
        return '\n'.join(parts)

    def _stable_key(self, ch: ChannelData) -> str:
        settings = self.core.get_replacement_settings()
        return ch.stable_key(settings)

    def _find_stable_url(self, ch: ChannelData) -> Optional[str]:
        ssm = self.core.stable_state_manager
        settings = self.core.get_replacement_settings()
        candidates: List[str] = []
        ref = ssm.get(ch.stable_key(settings))
        if ref:
            candidates.append(ref)
        if ch.link.url and ch.link.url not in candidates:
            candidates.append(ch.link.url)
        for u in ch.link.alternative_urls:
            if u and u not in candidates:
                candidates.append(u)

        for u in candidates:
            if not u or not u.strip():
                continue
            if (settings.is_blacklisted(u)
                    or settings.is_filtered_domain(u)):
                continue
            ok, rt, _, _ = URLUtils.check_url(
                u, STABLE_CHECK_TIMEOUT_SEC, verify_ssl=False)
            if ok is True and (rt is None
                               or rt * 1000
                               <= STABLE_LATENCY_THRESHOLD_MS):
                return u
        return None

    def export_stable(self, filepath: str,
                      progress_cb: Optional[Callable[
                          [int, int, str], None]] = None
                      ) -> Tuple[int, int]:
        ssm = self.core.stable_state_manager
        settings = self.core.get_replacement_settings()
        written = 0
        skipped = 0
        total = len(self.all_channels)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self.header_manager.get_header_text())
                for i, ch in enumerate(self.all_channels):
                    if progress_cb:
                        progress_cb(i, total,
                                     f"Stable: {i + 1}/{total}")
                    url = self._find_stable_url(ch)
                    if not url:
                        skipped += 1
                        continue
                    key = ch.stable_key(settings)
                    if not ssm.get(key):
                        ssm.set(key, url)
                    tmp = ch.copy()
                    tmp.link.url = url
                    tmp.link.has_url = True
                    tmp.update_extinf()
                    f.write(tmp.link.extinf + '\n')
                    for line in tmp.link.extvlcopt_lines:
                        f.write(line + '\n')
                    f.write(url + '\n')
                    written += 1
            ssm.save()
            return written, skipped
        except Exception:
            logger.exception("export_stable")
            raise

    def export_as(self):
        fp = save_file_dialog(
            self, "Экспорт", "playlist_export",
            "CSV (*.csv);;JSON (*.json);;Все (*.*)")
        if not fp:
            return
        try:
            if fp.lower().endswith('.csv'):
                with open(fp, 'w', encoding='utf-8',
                          newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        'name', 'original_name', 'group', 'tvg_id',
                        'tvg_name', 'tvg_logo', 'url',
                        'user_agent', 'region', 'quality',
                        'timeshift', 'catchup', 'catchup_source',
                        'catchup_days', 'tvg_shift', 'tvg_chno',
                        'audio_track', 'alternative_urls',
                        'link_source', 'url_status', 'status_code',
                        'epg_source', 'preferred_audio_lang',
                        'preferred_video_lang'])
                    for ch in self.all_channels:
                        writer.writerow([
                            ch.meta.name, ch.meta.original_name,
                            ch.meta.group, ch.meta.tvg_id,
                            ch.meta.tvg_name, ch.meta.tvg_logo,
                            ch.link.url, ch.link.user_agent,
                            ch.meta.region, ch.meta.quality,
                            ch.meta.timeshift, ch.meta.catchup,
                            ch.meta.catchup_source,
                            ch.meta.catchup_days, ch.meta.tvg_shift,
                            ch.meta.tvg_chno, ch.meta.audio_track,
                            '|'.join(ch.link.alternative_urls),
                            ch.link.link_source,
                            ('working'
                             if ch.status.url_status is True
                             else 'broken'
                             if ch.status.url_status is False
                             else 'unknown'),
                            (ch.status.status_code
                             if ch.status.status_code is not None
                             else ''),
                            ch.meta.epg_source,
                            ch.meta.preferred_audio_lang,
                            ch.meta.preferred_video_lang,
                        ])
            else:
                if not fp.lower().endswith('.json'):
                    fp += '.json'
                with open(fp, 'w', encoding='utf-8') as f:
                    json.dump(
                        [ch.to_dict() for ch in self.all_channels],
                        f, ensure_ascii=False, indent=2)
            info_box(self,
                     f"Экспортировано {len(self.all_channels)} "
                     f"каналов", "Готово")
        except Exception as e:
            logger.exception("export_as")
            error_box(self, str(e))

    def show_duplicate_finder(self):
        if not self.all_channels:
            info_box(self, "Нет каналов")
            return
        use_tvg = bool(self.core.config.get(
            'dedup_by_name_use_tvg', False))
        keep_dup = bool(self.core.config.get(
            'keep_duplicates', False))
        dlg = DuplicateFinderDialog(
            self.all_channels, self,
            use_tvg_id=use_tvg,
            keep_duplicates=keep_dup,
            settings=self.core.get_replacement_settings())

        def on_duplicates_removed(removed: int):
            if removed > 0 and _is_qobject_valid(self):
                self.save_state("Удаление дубликатов")
                self._normalize_order()
                self.sync_to_core()
                with self._suppress_save():
                    self._rebuild_model_safely(
                        preserve_selection=True)
                self.update_info()
                self.groups_changed.emit()

        dlg.duplicates_removed.connect(on_duplicates_removed)
        dlg.exec()

    def show_compare(self):
        if not self.all_channels:
            info_box(self, "Нет каналов")
            return
        from dialogs import ComparePlaylistsDialog
        text = self._render_playlist_text()
        label = (os.path.basename(self.filepath)
                 if self.filepath else "Текущий")
        dlg = ComparePlaylistsDialog(text, current_label=label,
                                      parent=self)
        dlg.exec()

    def _show_context_menu(self, position):
        menu = QMenu(self)
        rows = sorted({idx.row() for idx in
                       self.table.selectionModel().selectedRows()})
        if rows:
            if len(rows) == 1:
                self._build_single_row_menu(menu, rows[0])
            else:
                self._build_multi_row_menu(menu, set(rows))
        else:
            self._build_empty_menu(menu)
        menu.exec(self.table.mapToGlobal(position))

    def _build_single_row_menu(self, menu: QMenu, row: int):
        ch = self.channel_for_row(row)
        if ch:
            if ch.has_valid_url:
                menu.addAction(make_action(
                    menu, "▶ Смотреть в плеере",
                    lambda r=row: self._play_in_player(r)))
                menu.addSeparator()
            menu.addAction(make_action(
                menu, "Редактировать User Agent...",
                lambda r=row: self._edit_user_agent(r)))
            if ch.link.alternative_urls:
                alt = menu.addMenu(
                    f"Альтернативные ссылки "
                    f"({len(ch.link.alternative_urls)})")
                for i, u in enumerate(
                        ch.link.alternative_urls[:10]):
                    a = make_action(
                        alt, f"{i + 1}. {u[:60]}...",
                        lambda checked=False, uu=u, cc=ch:
                        self._apply_alternative_url(cc, uu))
                    alt.addAction(a)
            menu.addSeparator()
            if ch.status.url_status is False:
                menu.addAction(make_action(
                    menu, "Удалить битую ссылку",
                    lambda r=row: self._remove_broken_url(r)))
                menu.addSeparator()
            src_ch = self.find_channel_by_ref(ch)
            if src_ch is not None and src_ch.has_valid_url:
                host = URLUtils.extract_host(src_ch.link.url)
                if host:
                    is_ip = URLUtils.is_ip_address(host)
                    label = (
                        f"🚫 Заблокировать IP «{host}»"
                        if is_ip
                        else f"🚫 Заблокировать домен «{host}»")
                    menu.addAction(make_action(
                        menu, label,
                        lambda c=src_ch:
                        self.block_domain_from_channel(c)))
                menu.addSeparator()
        menu.addAction(make_action(menu, "Новый канал",
                                    self._new_channel))
        menu.addAction(make_action(menu, "Копировать канал",
                                    self._copy_channel))
        menu.addAction(make_action(menu, "Вырезать канал",
                                    self._cut_channel))
        menu.addAction(make_action(
            menu, "Вставить канал",
            lambda r=row: self._paste_channel(r)))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, "Пакетное переименование групп",
            self._rename_groups))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, "Добавить в чёрный список каналов",
            lambda r=row: self._add_to_blacklist(r)))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, "Проверить ссылку",
            lambda r=row: self._check_single_url(r)))
        menu.addAction(make_action(
            menu, "Заменить ссылку из источников...",
            lambda r=row: self.replace_single_link(r)))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, "Удалить канал",
            lambda r=row: self._delete_channel(r)))

    def _build_multi_row_menu(self, menu: QMenu, rows: Set[int]):
        cnt = len(rows)
        a = QAction(f"Выбрано каналов: {cnt}", menu)
        a.setEnabled(False)
        menu.addAction(a)
        menu.addSeparator()
        menu.addAction(make_action(menu, "Новый канал",
                                    self._new_channel))
        menu.addAction(make_action(
            menu, f"Копировать каналы ({cnt})",
            self._copy_selected_channels))
        menu.addAction(make_action(
            menu, f"Вырезать каналы ({cnt})",
            self._cut_selected_channels))
        menu.addAction(make_action(
            menu, f"Вставить каналы ({cnt})",
            lambda: self._paste_selected_channels(min(rows))))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, f"Массовая правка ({cnt})...",
            self._mass_edit_selected))
        menu.addAction(make_action(
            menu, "Пакетное переименование групп",
            self._rename_groups))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, f"Удалить выбранные ({cnt})",
            self._delete_selected_channels))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, f"Проверить ссылки ({cnt})",
            self._check_selected_urls))
        menu.addAction(make_action(
            menu, f"Заменить ссылки из источников ({cnt})...",
            self.replace_selected_links))
        menu.addAction(make_action(
            menu, f"Добавить в чёрный список каналов ({cnt})",
            self._add_selected_to_blacklist))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, f"Удалить битые ссылки ({cnt})",
            self._remove_selected_broken_urls))
        menu.addAction(make_action(
            menu, f"Удалить все ссылки ({cnt})",
            self._remove_selected_urls))

    def _build_empty_menu(self, menu: QMenu):
        menu.addAction(make_action(menu, "Новый канал",
                                    self._new_channel))
        menu.addAction(make_action(menu, "Вставить канал",
                                    self._paste_channel))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, "Пакетное переименование групп",
            self._rename_groups))
        menu.addSeparator()
        menu.addAction(make_action(
            menu, "Удалить дубликаты...",
            self.show_duplicate_finder))
        menu.addAction(make_action(
            menu, "Сравнить плейлисты...",
            self.show_compare))
        menu.addSeparator()
        menu.addAction(make_action(
            menu,
            "Применить метаданные из EPG ко всем каналам",
            lambda: self.apply_epg_metadata_to_all(silent=False)))

    def disconnect_signals(self):
        try:
            self.player_controller.close_dialog()
        except Exception:
            logger.exception("player_controller.close_dialog")

        for attr in ('_replacement_worker', '_epg_worker',
                     '_epg_meta_worker', '_src_check_worker'):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if w.isRunning():
                    with suppress(Exception):
                        w.stop()
                    with suppress(Exception):
                        w.wait(2000)
            except Exception:
                logger.exception(f"worker stop {attr}")
            setattr(self, attr, None)

        for sh in self._shortcuts:
            try:
                sh.activated.disconnect()
            except (TypeError, RuntimeError):
                pass
            sh.setParent(None)
            sh.deleteLater()
        self._shortcuts.clear()
        try:
            self.core.channels_updated.disconnect(
                self._on_core_channels_updated)
        except (TypeError, RuntimeError):
            pass
        try:
            self.model.dataChanged.disconnect(
                self._on_model_data_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            self.model.request_move.disconnect(
                self._on_model_request_move)
        except (TypeError, RuntimeError):
            pass