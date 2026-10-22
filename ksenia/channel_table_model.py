# -*- coding: utf-8 -*-
"""ChannelTableModel — QAbstractTableModel с ColumnSpec."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import (QAbstractTableModel, QMimeData,
                            QModelIndex, Qt, pyqtSignal)
from PyQt6.QtGui import QColor

from channel_columns import ColumnSpec, columns_for_channels
from constants import (
    GROUP_FILTER_ALL, DEFAULT_GROUP,
    STATUS_FILTER_ALL, STATUS_FILTER_WITH_URL,
    STATUS_FILTER_WITHOUT_URL, STATUS_FILTER_WORKING,
    STATUS_FILTER_NOT_WORKING, STATUS_FILTER_UNCHECKED,
)
from dup_colors import (dup_color_both, dup_color_name,
                         dup_color_url, dup_text_color_for_bg)
from logging_setup import logger
from models import ChannelData, LinkQuality
from ranked_search import parse_special_prefix
from theme import semantic_colors
from undo import SimpleDuplicateFinder


def _get_core_if_gui():
    try:
        from app_core import ApplicationCore
        return ApplicationCore.try_get_if_gui_thread()
    except Exception:
        return None


class ChannelTableModel(QAbstractTableModel):
    MIME_TYPE = "application/x-ksenia-channel-uids"

    request_move = pyqtSignal(list, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._channels: List[ChannelData] = []
        self._filtered: List[ChannelData] = []
        self._columns: List[ColumnSpec] = []
        self._column_index_by_key: Dict[str, int] = {}

        self._search_text = ""
        self._group_filter = GROUP_FILTER_ALL
        self._status_filter = STATUS_FILTER_ALL
        self._special: Optional[str] = None
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder

        self._duplicate_mode = False
        self._dup_uids_name: Set[int] = set()
        self._dup_uids_url: Set[int] = set()
        self._dup_name_key: Dict[int, str] = {}
        self._dup_url_key: Dict[int, str] = {}

        self._last_visible_keys: List[str] = []
        self._rebuild_columns(
            columns_for_channels([]))

    def _rebuild_columns(self, columns: List[ColumnSpec]):
        # QAbstractTableModel не имеет setColumnCount/setHorizontalHeaderLabels.
        # columnCount() и headerData() уже переопределены и читают
        # self._columns, поэтому достаточно обновить список.
        self._columns = list(columns)
        self._column_index_by_key = {
            spec.key: i for i, spec in enumerate(self._columns)}

    def columns(self) -> List[ColumnSpec]:
        return list(self._columns)

    def column_index(self, key: str) -> int:
        return self._column_index_by_key.get(key, -1)

    def column_key(self, index: int) -> Optional[str]:
        if 0 <= index < len(self._columns):
            return self._columns[index].key
        return None

    # ── Настройки ──
    @staticmethod
    def _get_settings():
        core = _get_core_if_gui()
        if core is None:
            return None
        try:
            return core.get_replacement_settings()
        except Exception:
            return None

    @staticmethod
    def _get_dedup_tvg_setting() -> bool:
        core = _get_core_if_gui()
        if core is None:
            return False
        try:
            return bool(core.config.get(
                'dedup_by_name_use_tvg', False))
        except Exception:
            return False

    @staticmethod
    def _has_any_metadata(ch: ChannelData) -> bool:
        return bool(
            ch.meta.tvg_id or ch.meta.tvg_logo or ch.meta.tvg_name
            or (ch.meta.group and ch.meta.group != DEFAULT_GROUP)
            or ch.link.user_agent)

    # ── Публичный API ──
    def set_channels(self, channels: List[ChannelData],
                     visible_optional_keys: Optional[List[str]] = None):
        self._channels = channels
        if visible_optional_keys is not None:
            self._last_visible_keys = list(visible_optional_keys)
        needed = columns_for_channels(channels,
                                       self._last_visible_keys)
        if ([c.key for c in needed]
                != [c.key for c in self._columns]):
            self._rebuild_columns(needed)
        if self._duplicate_mode:
            self._recompute_duplicates()
        self._reset_with_filter()

    def set_search_text(self, text: str):
        raw = (text or "").strip()
        self._special, remaining = parse_special_prefix(raw)
        self._search_text = remaining.lower()
        self._reset_with_filter()

    def set_group_filter(self, group: str):
        self._group_filter = group
        self._reset_with_filter()

    def set_status_filter(self, status: str):
        """Фильтр по статусу URL: Всего / С URL / Без URL /
        Работают / Не работают / Не проверены.

        Складывается по AND с группой, дубликатами, спец-префиксами
        и текстовым поиском.
        """
        if status not in (
                STATUS_FILTER_ALL, STATUS_FILTER_WITH_URL,
                STATUS_FILTER_WITHOUT_URL, STATUS_FILTER_WORKING,
                STATUS_FILTER_NOT_WORKING, STATUS_FILTER_UNCHECKED):
            status = STATUS_FILTER_ALL
        if self._status_filter == status:
            return
        self._status_filter = status
        self._reset_with_filter()

    def status_filter(self) -> str:
        return self._status_filter

    def set_duplicate_mode(self, enabled: bool):
        self._duplicate_mode = bool(enabled)
        if self._duplicate_mode:
            self._recompute_duplicates()
        else:
            self._dup_uids_name = set()
            self._dup_uids_url = set()
            self._dup_name_key = {}
            self._dup_url_key = {}
        self._reset_with_filter()

    def is_duplicate_mode(self) -> bool:
        return self._duplicate_mode

    def duplicate_counts(self) -> Tuple[int, int, int]:
        only_name = len(self._dup_uids_name - self._dup_uids_url)
        only_url = len(self._dup_uids_url - self._dup_uids_name)
        both = len(self._dup_uids_name & self._dup_uids_url)
        return only_name, only_url, both

    def _recompute_duplicates(self):
        settings = self._get_settings()
        (self._dup_uids_name, self._dup_uids_url,
         self._dup_name_key, self._dup_url_key) = \
            SimpleDuplicateFinder.find_duplicate_uids_with_keys(
                self._channels,
                use_tvg_id=self._get_dedup_tvg_setting(),
                settings=settings)

    # ── Фильтр ──
    def _reset_with_filter(self):
        self.beginResetModel()
        self._apply_filter_internal()
        self._sort_internal()
        self.endResetModel()

    def _apply_filter_internal(self):
        filtered = list(self._channels)

        if self._duplicate_mode:
            dup_uids = self._dup_uids_name | self._dup_uids_url
            filtered = [ch for ch in filtered
                        if ch.uid in dup_uids]

        if self._group_filter != GROUP_FILTER_ALL:
            filtered = [ch for ch in filtered
                        if ch.meta.group == self._group_filter]

        # ── Фильтр по статусу URL ──
        if self._status_filter == STATUS_FILTER_WITH_URL:
            filtered = [ch for ch in filtered
                        if ch.has_valid_url]
        elif self._status_filter == STATUS_FILTER_WITHOUT_URL:
            filtered = [ch for ch in filtered
                        if not ch.has_valid_url]
        elif self._status_filter == STATUS_FILTER_WORKING:
            filtered = [ch for ch in filtered
                        if ch.status.url_status is True]
        elif self._status_filter == STATUS_FILTER_NOT_WORKING:
            filtered = [ch for ch in filtered
                        if ch.status.url_status is False]
        elif self._status_filter == STATUS_FILTER_UNCHECKED:
            filtered = [ch for ch in filtered
                        if ch.has_valid_url
                        and ch.status.url_status is None]
        # STATUS_FILTER_ALL — без фильтрации

        s = self._search_text
        if self._special == "orphan":
            filtered = [ch for ch in filtered
                        if not ch.has_valid_url
                        and self._has_any_metadata(ch)]
        elif self._special == "broken":
            filtered = [ch for ch in filtered
                        if ch.status.url_status is False]
        elif self._special == "working":
            filtered = [ch for ch in filtered
                        if ch.status.url_status is True]

        if s:
            filtered = [
                ch for ch in filtered
                if (s in (ch.meta.name or "").lower() or
                    s in (ch.meta.group or "").lower() or
                    s in (ch.meta.tvg_id or "").lower() or
                    s in (ch.link.url or "").lower())
            ]
        self._filtered = filtered

    def _sort_key(self, ch: ChannelData, column: int):
        if 0 <= column < len(self._columns):
            return self._columns[column].sort_key(ch)
        return ""

    def _sort_internal(self):
        if self._sort_column < 0:
            return
        reverse = (self._sort_order == Qt.SortOrder.DescendingOrder)
        try:
            self._filtered.sort(
                key=lambda ch: self._sort_key(ch,
                                               self._sort_column),
                reverse=reverse)
        except TypeError:
            pass

    def sort(self, column: int,
             order: Qt.SortOrder = Qt.SortOrder.AscendingOrder):
        self._sort_column = column
        self._sort_order = order
        self.beginResetModel()
        self._sort_internal()
        self.endResetModel()

    # ── Доступ ──
    def channel_at(self, row: int) -> Optional[ChannelData]:
        if 0 <= row < len(self._filtered):
            return self._filtered[row]
        return None

    def channels_filtered(self) -> List[ChannelData]:
        return list(self._filtered)

    def row_of_uid(self, uid: int) -> int:
        for i, ch in enumerate(self._filtered):
            if ch.uid == uid:
                return i
        return -1

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered)

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns)

    def headerData(self, section, orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole):
            if 0 <= section < len(self._columns):
                return self._columns[section].title
        return None

    # ── Цвета ──
    def _dup_color_for_uid(self, uid: int) -> Optional[QColor]:
        in_name = uid in self._dup_uids_name
        in_url = uid in self._dup_uids_url
        if in_name and in_url:
            key = (self._dup_name_key.get(uid)
                   or self._dup_url_key.get(uid) or "")
            return dup_color_both(key)
        if in_url:
            return dup_color_url(self._dup_url_key.get(uid) or "")
        if in_name:
            return dup_color_name(self._dup_name_key.get(uid) or "")
        return None

    def _dup_text_color_for_uid(self, uid: int
                                  ) -> Optional[QColor]:
        bg = self._dup_color_for_uid(uid)
        if bg is None:
            return None
        return dup_text_color_for_bg(bg)

    # ── data ──
    def data(self, index: QModelIndex,
             role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ch = self._filtered[index.row()]
        col = index.column()
        spec = (self._columns[col]
                if 0 <= col < len(self._columns) else None)
        if spec is None:
            return None

        if role in (Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.EditRole):
            return spec.get_value(ch)

        if role == Qt.ItemDataRole.UserRole:
            return ch.uid

        if spec.key == "name" and role == Qt.ItemDataRole.BackgroundRole:
            if self._duplicate_mode:
                c = self._dup_color_for_uid(ch.uid)
                if c is not None:
                    return c
            if (not ch.has_valid_url
                    and self._has_any_metadata(ch)):
                return semantic_colors()['orphan_bg']
            return None

        if spec.key == "name" and role == Qt.ItemDataRole.ForegroundRole:
            if self._duplicate_mode:
                return self._dup_text_color_for_uid(ch.uid)
            return None

        if spec.key == "url":
            if role == Qt.ItemDataRole.BackgroundRole:
                if self._duplicate_mode and ch.uid in self._dup_uids_url:
                    if ch.uid in self._dup_uids_name:
                        key = (self._dup_name_key.get(ch.uid)
                               or self._dup_url_key.get(ch.uid)
                               or "")
                        return dup_color_both(key)
                    return dup_color_url(
                        self._dup_url_key.get(ch.uid) or "")
                if ch.link.user_agent:
                    base = semantic_colors()['working']
                    c = QColor(base)
                    c.setAlpha(60)
                    return c
                return None
            if role == Qt.ItemDataRole.ForegroundRole:
                if self._duplicate_mode and ch.uid in self._dup_uids_url:
                    tc = self._dup_text_color_for_uid(ch.uid)
                    if tc is not None:
                        return tc
                return ch.get_url_foreground()

        if role == Qt.ItemDataRole.ToolTipRole:
            return ch.get_status_tooltip()
        if (role == Qt.ItemDataRole.TextAlignmentRole
                and spec.alignment == "center"):
            return Qt.AlignmentFlag.AlignCenter
        return None

    # ── flags/mime ──
    def flags(self, index: QModelIndex):
        if not index.isValid():
            return (Qt.ItemFlag.ItemIsDropEnabled
                    | Qt.ItemFlag.ItemIsEnabled)
        f = (Qt.ItemFlag.ItemIsEnabled
             | Qt.ItemFlag.ItemIsSelectable
             | Qt.ItemFlag.ItemIsDragEnabled
             | Qt.ItemFlag.ItemIsDropEnabled)
        spec = (self._columns[index.column()]
                if 0 <= index.column() < len(self._columns)
                else None)
        if spec is not None and spec.editable:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def supportedDragActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [self.MIME_TYPE]

    def mimeData(self, indexes):
        uids = sorted({self._filtered[i.row()].uid
                       for i in indexes
                       if i.isValid()
                       and 0 <= i.row() < len(self._filtered)})
        if not uids:
            return None
        mime = QMimeData()
        mime.setData(self.MIME_TYPE,
                     ",".join(map(str, uids)).encode('ascii'))
        return mime

    def dropMimeData(self, data, action, row, column, parent):
        if not data.hasFormat(self.MIME_TYPE):
            return False
        if action == Qt.DropAction.IgnoreAction:
            return True
        try:
            raw = bytes(data.data(self.MIME_TYPE)).decode('ascii')
            uids = [int(x) for x in raw.split(',') if x.strip()]
        except ValueError:
            return False
        if not uids:
            return False
        target_row = parent.row() if parent.isValid() else row
        ref_uid: Optional[int] = None
        if 0 <= target_row < len(self._filtered):
            ref_uid = self._filtered[target_row].uid
        self.request_move.emit(uids, ref_uid)
        return True

    # ── setData ──
    def setData(self, index: QModelIndex, value,
                role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        ch = self._filtered[index.row()]
        spec = (self._columns[index.column()]
                if 0 <= index.column() < len(self._columns)
                else None)
        if spec is None or not spec.editable:
            return False
        new_value = str(value).strip()
        key = spec.key
        changed = False
        if key == "name":
            if ch.meta.name == new_value:
                return False
            ch.meta.name = new_value
            changed = True
        elif key == "group":
            if ch.meta.group == new_value:
                return False
            ch.meta.group = new_value or DEFAULT_GROUP
            changed = True
        elif key == "tvg_id":
            if ch.meta.tvg_id == new_value:
                return False
            ch.meta.tvg_id = new_value
            changed = True
        elif key == "logo":
            if ch.meta.tvg_logo == new_value:
                return False
            ch.meta.tvg_logo = new_value
            changed = True
        elif key == "catchup":
            if ch.meta.catchup == new_value:
                return False
            ch.meta.catchup = new_value
            changed = True
        elif key == "url":
            if ch.link.url == new_value:
                return False
            core = _get_core_if_gui()
            if core is not None and ch.has_valid_url and new_value:
                max_alts = core.get_replacement_settings()\
                    .max_alternative_urls
                if ch.link.url not in ch.link.alternative_urls:
                    ch.link.alternative_urls.insert(0, ch.link.url)
                    ch.link.alternative_urls = \
                        ch.link.alternative_urls[:max_alts]
            ch.link.url = new_value
            ch.link.has_url = bool(ch.link.url)
            ch.link.link_source = ""
            ch.link.user_agent = ""
            ch.link.extra_headers.clear()
            ch.status.reset()
            if core is not None:
                core.domain_user_agent_manager\
                    .apply_rules_to_channel(ch)
            changed = True
        else:
            return False
        if changed:
            if key != "url":
                ch.update_extinf()
            ch.touch()
            if self._duplicate_mode:
                self._recompute_duplicates()
            self.dataChanged.emit(index, index)
            return True
        return False

    # ── refresh ──
    def refresh_row(self, row: int):
        if 0 <= row < len(self._filtered):
            self.dataChanged.emit(self.index(row, 0),
                                   self.index(row,
                                               self.columnCount() - 1))

    def refresh_all(self):
        if self._filtered:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._filtered) - 1,
                           self.columnCount() - 1))