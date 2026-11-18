# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""PlaylistTab — виджет одной вкладки плейлиста."""
from __future__ import annotations

import os
import queue
import weakref
from contextlib import contextmanager, suppress
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import (
    QItemSelectionModel, QSettings, Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QMessageBox, QPushButton,
    QTableView, QVBoxLayout, QWidget)

from app_core import ApplicationCore
from channel_table_model import ChannelTableModel
from constants import (
    DEFAULT_GROUP, DEFAULT_EPG_URL, EPG_ALLOWED_META_FIELDS,
    FFPROBE_TIMEOUT_SEC, GROUP_FILTER_ALL, M3U_FILTER,
    REPLACEMENT_MAX_WORKERS_DEFAULT,
    SEARCH_DEBOUNCE_MS, STABLE_CHECK_TIMEOUT_SEC,
    STABLE_LATENCY_THRESHOLD_MS, STATE_SAVE_DEBOUNCE_MS,
    StatusText, SYNC_DEBOUNCE_MS, SAVE_ENCODINGS)
from dialogs import (
    BlockDomainDialog, DuplicateFinderDialog, LinkSelectionDialog,
    PlaylistHeaderDialog, _is_qobject_valid,
    make_action)
from models import ChannelData, LinkQuality
from parsers import M3UParser, PlaylistHeaderManager
from paths import (confirm, confirm_three, error_box, info_box,
                   logger, save_file_dialog, warn_box)
from player import (EmbeddedPlayerDialog, get_mpv_error,
                    is_mpv_available)
from player_controller import PlayerController
from playlist_actions import (
    build_empty_menu, build_multi_row_menu, build_single_row_menu,
    clear_urls, copy_channel, copy_selected_channels,
    cut_channel, cut_selected_channels,
    delete_by_predicate, delete_channel, delete_channels_without_groups,
    delete_channels_without_metadata, delete_channels_without_urls,
    delete_selected_channels, has_any_metadata,
    move_channel_down, move_channel_up,
    move_selected_down, move_selected_up, new_channel,
    on_model_request_move, paste_channel, paste_selected_channels,
    remove_all_broken_urls, remove_all_urls, remove_broken_url,
    remove_selected_broken_urls, remove_selected_urls,
    rename_groups, show_context_menu)
from playlist_edit import (
    add_link_from_current as _add_link_from_current,
    add_link_from_sources as _add_link_from_sources,
    add_selected_to_blacklist as _add_selected_to_blacklist,
    add_to_blacklist as _add_to_blacklist,
    apply_alternative_url as _apply_alternative_url,
    apply_blacklist as _apply_blacklist,
    block_domain_from_channel as _block_domain_from_channel,
    clean_blocked_by_domain as _clean_blocked_by_domain,
    do_apply_link as _do_apply_link,
    edit_user_agent as _edit_user_agent,
    has_meta_to_remove as _has_meta_to_remove,
    remove_metadata as _remove_metadata,
    replace_all_links as _replace_all_links,
    replace_broken_links as _replace_broken_links,
    replace_link_from_sources as _replace_link_from_sources,
    replace_links_async as _replace_links_async,
    replace_selected_links as _replace_selected_links,
    replace_single_link as _replace_single_link)
from playlist_io import (
    export_channels_csv, export_channels_json,
    export_stable_to_file, find_stable_url,
    render_playlist_text, resolve_save_encoding,
    save_playlist_file)
from playlist_workers import (
    apply_epg_metadata_to_all as _apply_epg_metadata_to_all,
    apply_resolution_from_player as _apply_resolution_from_player,
    check_all_urls as _check_all_urls,
    check_selected_urls as _check_selected_urls,
    check_single_url as _check_single_url,
    check_urls_async as _check_urls_async,
    drain_resolution_queue as _drain_resolution_queue,
    load_epg_async as _load_epg_async,
    mark_channel_status as _mark_channel_status,
    start_background_resolution as _start_background_resolution)
from undo import (
    UndoRedoManager, Command,
    AddChannel, RemoveChannel, ReorderChannels,
)
from utils import (URLUtils, decode_m3u_bytes, sanitize_user_agent)
from plural_ru import (
    channels_word, links_word, rules_word,
    updated_verb, deleted_verb, replaced_verb, added_verb,
    working_word, unchecked_word, sources_word,
)
from workers import (
    BackgroundResolutionWorker,
    EPGLoaderWorker, EPGMetadataApplyWorker,
    LinkReplacementWorker, ResolutionWorker)


OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)

_DEFAULT_COLUMN_WIDTHS: Dict[str, int] = {
    "index": 50,
    "name": 320,
    "group": 140,
    "tvg_id": 120,
    "logo": 120,
    "catchup": 100,
    "resolution": 90,
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
        self._resolution_worker: Optional[ResolutionWorker] = None

        self._bg_resolution_worker: Optional[
            BackgroundResolutionWorker] = None
        self._resolution_queue: "queue.Queue" = queue.Queue()
        self._resolution_timer = QTimer(self)
        self._resolution_timer.setInterval(200)
        self._resolution_timer.timeout.connect(
            self._drain_resolution_queue)

        self._restoring_widths = False
        self._initial_widths_applied = False
        self._last_column_keys: List[str] = []

        self.core = ApplicationCore.instance()
        self.header_manager = PlaylistHeaderManager()
        self.undo_manager = UndoRedoManager()
        self.undo_manager.bind_apply_callback(
            self._apply_commands)

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

        self._inside_edit_channels = False

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

        self.table.dropEvent = self._table_drop_event

        h.sectionResized.connect(self._on_section_resized)
        self._apply_resize_modes()

    def _table_drop_event(self, event):
        if event.source() is not self.table:
            return
        idx = self.table.indexAt(event.position().toPoint())
        insert_after = 0
        if idx.isValid():
            rect = self.table.visualRect(idx)
            if (event.position().y()
                    > rect.y() + rect.height() / 2):
                insert_after = 1
        self.model._pending_insert_after = insert_after
        try:
            QTableView.dropEvent(self.table, event)
        finally:
            self.model._pending_insert_after = 0

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
        prev_channels = self._snapshot_channels_for_rollback()
        prev_pending_desc = self._pending_state_desc
        self._pending_state_desc = ""

        ok = False
        self._inside_edit_channels = True
        try:
            with self._suppress_save():
                yield self.all_channels
            ok = True
        finally:
            self._inside_edit_channels = False
            if ok:
                self._commit(description,
                             reset_view=(refresh == 'reset'),
                             save_state=True)
            else:
                try:
                    self.all_channels = prev_channels
                    self._normalize_order()
                except Exception:
                    logger.exception("rollback after failed edit")
                self._pending_state_desc = prev_pending_desc
                with self._suppress_save():
                    self._rebuild_model_safely(preserve_selection=True)

    def _snapshot_channels_for_rollback(self) -> List[ChannelData]:
        return [ch.copy() for ch in self.all_channels]

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
                     '_replacement_worker', '_src_check_worker',
                     '_resolution_worker', '_bg_resolution_worker'):
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
        with suppress(Exception):
            self._resolution_timer.stop()

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
                    f"• удалено по чёрному списку каналов: "
                    f"{removed_by_bl}")
                msg_parts.append(
                    f"• очищено ссылок по чёрному списку "
                    f"доменов/IP: {cleaned_by_domain_bl}")
                msg_parts.append(
                    "Каналы без ссылок сохранены и помечены.")
            if parsed_without_url:
                saved_word = "сохранён" if parsed_without_url == 1 \
                    else "сохранены"
                msg_parts.append(
                    f"\nКаналов без ссылок в исходном файле: "
                    f"{parsed_without_url} — {saved_word}.")
            if msg_parts:
                info_box(
                    self,
                    "\n".join(msg_parts),
                    "Фильтры и каналы без ссылок")

            self.undo_manager.reset(self.all_channels)
            self.modified = False
            self._needs_resort = True

            self.sync_to_core()
            self.flush_sync()

            self.refresh_view(full_reset=True)
            self.update_modified_status()

            if bool(self.core.config.get(
                    'auto_check_on_open', False)):
                with_urls = [ch for ch in self.all_channels
                             if ch.has_valid_url]
                if with_urls:
                    QTimer.singleShot(
                        200,
                        lambda chs=with_urls:
                        self.check_urls_async(chs))
        except Exception as e:
            logger.exception("Load file error")
            self.all_channels = []
            self.modified = False
            self.refresh_view(full_reset=True)
            error_box(self, f"Не удалось загрузить:\n{e}")
        finally:
            self._suppress_state_save = False
            self._loading = False

    def update_info(self):
        st = self.core.get_stats(self.tab_id)
        parts = [
            f"Всего: {st['total']}",
            f"Со ссылками: {st['with_url']}",
            f"Без ссылок: {st['without_url']}",
            f"✓ {working_word(st['working'])}: {st['working']}",
            f"✗ Не работают: {st['not_working']}",
            f"? {unchecked_word(st['unknown'])}: {st['unknown']}",
        ]
        if st.get('unsupported'):
            parts.append(f"🟦 N/A: {st['unsupported']}")
        parts.append(f"Групп: {st['groups']}")
        info = " | ".join(parts)
        if self.model.is_duplicate_mode():
            only_name, only_url, both = self.model.duplicate_counts()
            info += (f" | 🔁 дубликаты: имя {only_name}, "
                     f"URL {only_url}, оба {both}")
        res_counts: Dict[str, int] = {}
        for ch in self.all_channels:
            r = ch.status.resolution
            if r:
                res_counts[r] = res_counts.get(r, 0) + 1
        if res_counts:
            order = ["4K", "1080p", "720p", "576p", "480p"]
            shown = []
            for k in order:
                if k in res_counts:
                    shown.append(f"{k}:{res_counts[k]}")
            for k in sorted(res_counts.keys()):
                if k not in order:
                    shown.append(f"{k}:{res_counts[k]}")
            if shown:
                info += " | 🎬 " + " ".join(shown)
        self.info_changed.emit(info)

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
            if not self._state_save_timer.isActive():
                self._pending_state_desc = "Правка ячейки"
                self._state_save_timer.start()
            self.sync_to_core()
            self.update_info()
            if self.model.is_duplicate_mode():
                QTimer.singleShot(
                    0, self._reapply_duplicate_mode_async)
            self.groups_changed.emit()
        except RuntimeError:
            logger.warning(
                "dataChanged handler hit deleted item, ignoring")

    def _reapply_duplicate_mode_async(self):
        if not _is_qobject_valid(self):
            return
        try:
            self.model.set_duplicate_mode(True)
        except Exception:
            logger.exception("reapply duplicate mode")

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
                self._apply_commands(
                    diff.get('commands', []), reverse=True)
        self.undo_state_changed.emit(self.undo_manager.can_undo(),
                                      self.undo_manager.can_redo())

    def redo(self):
        diff = self.undo_manager.redo()
        if diff:
            with self._suppress_save():
                self._apply_commands(
                    diff.get('commands', []), reverse=False)
        self.undo_state_changed.emit(self.undo_manager.can_undo(),
                                      self.undo_manager.can_redo())

    def _apply_commands(self, commands: List[Command],
                        reverse: bool) -> bool:
        try:
            by_uid: Dict[int, ChannelData] = {
                ch.uid: ch for ch in self.all_channels}
            added: List = []
            removed: List = []
            reorder: Optional[ReorderChannels] = None

            for cmd in commands:
                if isinstance(cmd, AddChannel):
                    added.append(cmd.snapshot)
                elif isinstance(cmd, RemoveChannel):
                    removed.append(cmd.snapshot)
                elif isinstance(cmd, ReorderChannels):
                    reorder = cmd
                else:
                    if reverse:
                        cmd.undo(by_uid)
                    else:
                        cmd.apply(by_uid)

            if reverse:
                add_uids = {s.uid for s in added}
                remove_uids = {s.uid for s in removed}
                if add_uids:
                    self.all_channels = [
                        ch for ch in self.all_channels
                        if ch.uid not in add_uids]
                for snap in removed:
                    existing = by_uid.get(snap.uid)
                    if existing is None:
                        ch = ChannelData(uid=snap.uid)
                        ch.restore_from_snapshot(snap)
                        self.all_channels.append(ch)
                if reorder is not None:
                    old_order = reorder.old_order
                    by = {ch.uid: ch for ch in self.all_channels}
                    self.all_channels = [
                        by[u] for u in old_order if u in by]
                    seen = set(old_order)
                    for ch in by.values():
                        if ch.uid not in seen:
                            self.all_channels.append(ch)
            else:
                remove_uids = {s.uid for s in removed}
                add_snaps = {s.uid: s for s in added}
                if remove_uids:
                    self.all_channels = [
                        ch for ch in self.all_channels
                        if ch.uid not in remove_uids]
                for uid, snap in add_snaps.items():
                    if not any(ch.uid == uid
                               for ch in self.all_channels):
                        ch = ChannelData(uid=uid)
                        ch.restore_from_snapshot(snap)
                        self.all_channels.append(ch)
                if reorder is not None:
                    new_order = reorder.new_order
                    by = {ch.uid: ch for ch in self.all_channels}
                    self.all_channels = [
                        by[u] for u in new_order if u in by]
                    seen = set(new_order)
                    for ch in by.values():
                        if ch.uid not in seen:
                            self.all_channels.append(ch)

            self._normalize_order()
            self.modified = True
            self.sync_to_core()
            self.refresh_view(full_reset=True)
            self.update_modified_status()
            return True
        except Exception:
            logger.exception("_apply_commands failed")
            return False

    def update_modified_status(self):
        self.core.update_tab_metadata(self.tab_id,
                                       modified=self.modified)
        w = self.parent_window
        if w is not None:
            w._update_window_title()

    def _play_in_player(self, row: int):
        self.player_controller.play_row(row)

    # ── обёртки на playlist_workers ──

    def load_epg_async(self):
        _load_epg_async(self)

    def apply_epg_metadata_to_all(self, silent: bool = False) -> bool:
        return _apply_epg_metadata_to_all(self, silent)

    def mark_channel_status(self, uid: int, ok: bool) -> None:
        _mark_channel_status(self, uid, ok)

    def apply_resolution_from_player(self, uid: int,
                                      w: int, h: int) -> None:
        _apply_resolution_from_player(self, uid, w, h)

    def _start_background_resolution(self, channels):
        _start_background_resolution(self, channels)

    def _drain_resolution_queue(self) -> None:
        _drain_resolution_queue(self)

    def check_urls_async(self, channels: List[ChannelData]):
        _check_urls_async(self, channels)

    def _check_selected_urls(self):
        _check_selected_urls(self)

    def check_all_urls(self):
        _check_all_urls(self)

    def _check_single_url(self, row: int):
        _check_single_url(self, row)

    # ── обёртки на playlist_actions ──

    def _clear_urls(self, channels, description, confirm_msg="",
                    only_broken=False):
        return clear_urls(self, channels, description,
                          confirm_msg, only_broken)

    def _remove_broken_url(self, row):
        remove_broken_url(self, row)

    def _remove_selected_broken_urls(self):
        remove_selected_broken_urls(self)

    def _remove_all_broken_urls(self):
        remove_all_broken_urls(self)

    def _remove_selected_urls(self):
        remove_selected_urls(self)

    def remove_all_urls(self):
        remove_all_urls(self)

    def _delete_channels_by_predicate(self, predicate, description,
                                       confirm_msg):
        return delete_by_predicate(self, predicate, description,
                                   confirm_msg)

    def _new_channel(self):
        new_channel(self)

    def _delete_channel(self, row=-1):
        delete_channel(self, row)

    def _delete_selected_channels(self):
        delete_selected_channels(self)

    def delete_channels_without_urls(self):
        delete_channels_without_urls(self)

    def delete_channels_without_metadata(self):
        delete_channels_without_metadata(self)

    def delete_channels_without_groups(self):
        delete_channels_without_groups(self)

    @staticmethod
    def _has_any_metadata(ch):
        return has_any_metadata(ch)

    def _copy_channel(self):
        copy_channel(self)

    def _cut_channel(self):
        cut_channel(self)

    def _cut_selected_channels(self):
        cut_selected_channels(self)

    def _copy_selected_channels(self):
        copy_selected_channels(self)

    def _paste_channel(self, row=-1):
        paste_channel(self, row)

    def _paste_selected_channels(self, row=-1):
        paste_selected_channels(self, row)

    def _on_model_request_move(self, uids, ref_uid, insert_after=0):
        on_model_request_move(self, uids, ref_uid, insert_after)

    def _rename_groups(self):
        rename_groups(self)

    def _move_channel_up(self, row=-1):
        move_channel_up(self, row)

    def _move_channel_down(self, row=-1):
        move_channel_down(self, row)

    def _move_selected_up(self):
        move_selected_up(self)

    def _move_selected_down(self):
        move_selected_down(self)

    def _show_context_menu(self, position):
        show_context_menu(self, position)

    # ── обёртки на playlist_edit ──

    def block_domain_from_channel(self, channel: ChannelData):
        _block_domain_from_channel(self, channel)

    def _clean_blocked_by_domain(self) -> int:
        return _clean_blocked_by_domain(self)

    def _add_to_blacklist(self, row: int = -1):
        _add_to_blacklist(self, row)

    def _add_selected_to_blacklist(self):
        _add_selected_to_blacklist(self)

    def _edit_user_agent(self, row: int):
        _edit_user_agent(self, row)

    def apply_blacklist(self) -> int:
        return _apply_blacklist(self)

    def remove_metadata(self, opts: Dict[str, bool],
                        scope: str = "all"):
        _remove_metadata(self, opts, scope)

    @staticmethod
    def _has_meta_to_remove(ch: ChannelData,
                             opts: Dict[str, bool]) -> bool:
        return _has_meta_to_remove(ch, opts)

    def _apply_alternative_url(self, channel: ChannelData, url: str):
        _apply_alternative_url(self, channel, url)

    def _add_link_from_current(self):
        _add_link_from_current(self)

    def _add_link_from_sources(self, row: int):
        _add_link_from_sources(self, row)

    def _do_apply_link(self, channel: ChannelData,
                        source_ch: ChannelData):
        _do_apply_link(self, channel, source_ch)

    def _replace_link_from_sources(self, row: int):
        _replace_link_from_sources(self, row)

    def replace_broken_links(self):
        _replace_broken_links(self)

    def replace_selected_links(self):
        _replace_selected_links(self)

    def replace_single_link(self, row: int):
        _replace_single_link(self, row)

    def replace_all_links(self):
        _replace_all_links(self)

    def replace_links_async(self, channels: List[ChannelData]):
        _replace_links_async(self, channels)

    # ── файл / экспорт / дубликаты / сравнение ──

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
        explicit_save_as = bool(filepath)
        if filepath:
            self.filepath = filepath
        if not self.filepath:
            return False
        self.flush_sync()

        def _ask_encoding(title: str, msg: str) -> bool:
            reply = QMessageBox.question(
                self, title, msg,
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No)
            return reply == QMessageBox.StandardButton.Yes

        ok = save_playlist_file(
            self.filepath, self.all_channels,
            self.header_manager, self.core.config,
            explicit_save_as, self, _ask_encoding)
        if not ok:
            return False
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

    def _stable_key(self, ch: ChannelData) -> str:
        settings = self.core.get_replacement_settings()
        return ch.stable_key(settings)

    def export_stable(self, filepath: str,
                      progress_cb: Optional[Callable[
                          [int, int, str], None]] = None
                      ) -> Tuple[int, int]:
        ssm = self.core.stable_state_manager
        settings = self.core.get_replacement_settings()
        try:
            return export_stable_to_file(
                filepath, self.all_channels,
                self.header_manager, ssm, settings,
                progress_cb)
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
                export_channels_csv(self.all_channels, fp)
            else:
                if not fp.lower().endswith('.json'):
                    fp += '.json'
                export_channels_json(self.all_channels, fp)
            info_box(self,
                     f"Экспортировано {len(self.all_channels)} "
                     f"{channels_word(len(self.all_channels))}",
                     "Готово")
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
            if removed <= 0 or not _is_qobject_valid(self):
                return
            new_list = dlg.get_result()
            if not new_list:
                return
            self.save_state("Удаление дубликатов")
            self.all_channels[:] = new_list
            self._normalize_order()
            self.sync_to_core()
            with self._suppress_save():
                self._rebuild_model_safely(preserve_selection=True)
            self.update_info()
            self.groups_changed.emit()

        dlg.duplicates_removed.connect(on_duplicates_removed)
        dlg.exec()

    def show_compare(self):
        if not self.all_channels:
            info_box(self, "Нет каналов")
            return
        from dialogs import ComparePlaylistsDialog
        text = render_playlist_text(
            self.all_channels, self.header_manager,
            self.core.config.get('save_extvlcopt', True))
        label = (os.path.basename(self.filepath)
                 if self.filepath else "Текущий")
        dlg = ComparePlaylistsDialog(text, current_label=label,
                                      parent=self)
        dlg.exec()

    def _detect_resolution_current(self):
        ch = self.current_channel
        if ch is None:
            info_box(self, "Не выбран канал.")
            return
        if not ch.has_valid_url:
            info_box(self, "У канала нет ссылки.")
            return
        self._detect_resolution_for([ch])

    def _detect_resolution_for(self,
                                channels: List[ChannelData]):
        if not _is_qobject_valid(self):
            return
        targets = [ch for ch in channels if ch.has_valid_url]
        if not targets:
            info_box(self, "Нет каналов со ссылками "
                            "для определения разрешения.")
            return
        if (self._resolution_worker is not None
                and self._resolution_worker.isRunning()):
            info_box(self, "Определение разрешения уже идёт.")
            return

        from ffprobe_utils import is_ffprobe_available
        if not is_ffprobe_available():
            info_box(
                self,
                "ffprobe не найден.\n\n"
                "Укажите путь в «Настройки → Общие → "
                "Внешние приложения → Путь к ffprobe» "
                "или установите FFmpeg:\n"
                "• Debian/Ubuntu: sudo apt install ffmpeg\n"
                "• Fedora: sudo dnf install ffmpeg\n"
                "• Arch: sudo pacman -S ffmpeg\n"
                "• Windows: https://ffmpeg.org/download.html\n\n"
                "Разрешение недоступно, остальные функции "
                "работают как обычно.",
                "ffprobe недоступен")
            return

        w = self.parent_window
        if w is not None:
            w.set_action(
                f"⏳ ffprobe: {len(targets)} каналов")
            w.show_progress(0, len(targets),
                            "ffprobe: определение…")

        worker = ResolutionWorker(targets)
        self._resolution_worker = worker

        uid_map: Dict[int, ChannelData] = {
            ch.uid: ch for ch in targets}

        def on_resolution(uid: int, res: str):
            ch = uid_map.get(uid)
            if ch is None or not _is_qobject_valid(self):
                return
            ch.status.resolution = res
            try:
                row = self.model.row_of_uid(uid)
                if row >= 0:
                    self.model.refresh_row(row)
            except Exception:
                logger.exception("refresh_row после ffprobe")

        def on_progress(value: int, total: int, text: str):
            w2 = self.parent_window
            if w2 is not None:
                w2.show_progress(value, max(1, total), text)

        def on_done():
            if self._resolution_worker is worker:
                self._resolution_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                with self._suppress_save():
                    self._rebuild_model_safely(
                        preserve_selection=True)
                self.update_info()

        def on_error(msg: str):
            if self._resolution_worker is worker:
                self._resolution_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                error_box(self, msg, "ffprobe")

        worker.resolution_ready.connect(
            on_resolution, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(
            on_progress, Qt.ConnectionType.QueuedConnection)
        worker.worker_done.connect(
            on_done, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(
            on_error, Qt.ConnectionType.QueuedConnection)
        worker.start()

    def _detect_resolution_for_row(self, row: int):
        ch = self.channel_for_row(row)
        if ch is None or not ch.has_valid_url:
            info_box(self, "У канала нет ссылки.")
            return
        self._detect_resolution_for([ch])

    def _detect_resolution_selected(self):
        if not self.selected_channels:
            info_box(self, "Не выбрано ни одного канала.")
            return
        with_urls = [ch for ch in self.selected_channels
                     if ch.has_valid_url]
        if not with_urls:
            info_box(self, "У выбранных каналов нет ссылок.")
            return
        self._detect_resolution_for(with_urls)

    def _detect_resolution_all(self):
        with_urls = [ch for ch in self.all_channels
                     if ch.has_valid_url]
        if not with_urls:
            info_box(self, "Нет каналов со ссылками.")
            return
        if not confirm(
                self,
                f"Определить разрешение у {len(with_urls)} "
                f"{channels_word(len(with_urls))}?\n\n"
                f"Это может занять время: ffprobe делает "
                f"отдельный сетевой запрос на канал."):
            return
        self._detect_resolution_for(with_urls)

    def disconnect_signals(self):
        try:
            self.player_controller.close_dialog()
        except Exception:
            logger.exception("player_controller.close_dialog")

        for attr in ('_replacement_worker', '_epg_worker',
                     '_epg_meta_worker', '_src_check_worker',
                     '_resolution_worker', '_bg_resolution_worker'):
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

        with suppress(Exception):
            self._resolution_timer.stop()

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