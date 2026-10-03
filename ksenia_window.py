# -*- coding: utf-8 -*-
"""ApplicationCore, ChannelTableModel, PlaylistTab, MainWindow, CLI."""

from __future__ import annotations
import os
import sys
import csv
import json
import shutil
import weakref
import uuid
import argparse
import signal
import threading
import concurrent.futures
from contextlib import suppress, contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Callable
from collections import defaultdict
from PyQt6.QtCore import (Qt, QTimer, QSettings, QPoint, pyqtSignal,
    QObject, QThread, QAbstractTableModel, QModelIndex, QUrl,
    QCoreApplication)
from PyQt6.QtGui import QAction, QKeySequence, QColor, QFont, QShortcut, QIcon
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QGroupBox, QFormLayout, QLineEdit, QPushButton,
    QComboBox, QLabel, QMenu, QStatusBar, QToolBar, QFileDialog,
    QMessageBox, QDialog, QDialogButtonBox, QListWidget,
    QHeaderView, QAbstractItemView, QInputDialog, QTextEdit,
    QCheckBox, QRadioButton, QProgressBar, QFrame, QPlainTextEdit,
    QStyle, QSpinBox, QDoubleSpinBox, QTableView, QButtonGroup,
    QSlider)
from constants import *
from models import *
from paths import *
from utils import *
from config import Config, LinkReplacementSettings
from storage import CacheManager, StableStateManager
from blacklists import (BlacklistManager, DomainBlacklistManager,
    DomainUserAgentManager, DomainUserAgentRule)
from sources import LinkSource, LinkSourceManager
from epg import EPGDatabase
from parsers import M3UParser, PlaylistHeaderManager
from undo import UndoRedoManager, SimpleDuplicateFinder
from workers import (LinkReplacementWorker,
    SourcesRefreshWorker, SourceUrlCheckWorker, EPGLoaderWorker,
    EPGMetadataApplyWorker)
from dialogs import (
    BaseDialog, IconProvider, _is_qobject_valid, _is_gui_thread,
    make_table, make_action, fill_channels_table, make_form,
    json_import_dialog, json_export_dialog,
    SupportDialog, HelpDialog, M3USyntaxHighlighter, EnhancedTextEdit,
    PlaylistHeaderDialog, RemoveMetadataDialog, MassEditDialog,
    LinkReplacementSettingsDialog, DuplicateFinderDialog,
    ComparePlaylistsDialog, BlockDomainDialog, DomainBlacklistDialog,
    LinkSourceEditDialog, DomainUserAgentEditDialog,
    DomainUserAgentDialog, LinkSourceManagerDialog,
    LinkSelectionDialog, BlacklistDialog,
    PlaylistFromSourcesDialog, GeneralSettingsDialog,
    CacheManagerDialog)
from player import (EmbeddedVlcPlayer, EmbeddedPlayerDialog,
    is_vlc_available, get_vlc_error)

# Флаги VLC (могут отсутствовать на системе)
try:
    import vlc
    _HAS_VLC_MODULE = True
    _VLC_IMPORT_ERROR = ""
except ImportError as _e:
    vlc = None
    _HAS_VLC_MODULE = False
    _VLC_IMPORT_ERROR = str(_e)
except Exception as _e:
    vlc = None
    _HAS_VLC_MODULE = False
    _VLC_IMPORT_ERROR = str(_e)


class ApplicationCore(QObject):
    channels_updated = pyqtSignal(str)
    sources_updated = pyqtSignal()
    settings_changed = pyqtSignal()
    blacklist_updated = pyqtSignal()
    domain_blacklist_updated = pyqtSignal()
    epg_updated = pyqtSignal()
    playlist_from_sources_ready = pyqtSignal(list)
    playlist_from_sources_failed = pyqtSignal(str)

    _instance: Optional['ApplicationCore'] = None
    _instance_lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                cls._instance = obj
            return cls._instance

    _initialized: bool = False

    def __init__(self):
        if ApplicationCore._initialized:
            return
        super().__init__()
        ApplicationCore._initialized = True
        self._initialize()

    @classmethod
    def instance(cls) -> 'ApplicationCore':
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _initialize(self):
        config_dir = Paths.get_config_dir()
        os.makedirs(config_dir, exist_ok=True)
        self.config = Config(os.path.join(config_dir, "editor_config.json"))
        self.blacklist_manager = BlacklistManager(config_dir)
        self.domain_blacklist_manager = DomainBlacklistManager(config_dir)
        self.cache_manager = CacheManager() if self.config.get(
            'use_cache_manager', True) else None
        self.link_source_manager = LinkSourceManager(config_dir,
                                                      self.cache_manager)
        self.domain_user_agent_manager = DomainUserAgentManager(config_dir)
        self.link_replacement_settings = LinkReplacementSettings(self.config)
        self.stable_state_manager = StableStateManager(config_dir)

        if self.cache_manager:
            with suppress(Exception):
                self.cache_manager.configure_link_cache(
                    max_files=int(self.config.get('link_cache_max_files', 10000)),
                    max_mb=int(self.config.get('link_cache_max_mb', 64)))

        self.epg_db = EPGDatabase(self.cache_manager)

        self._tabs: Dict[str, weakref.ref] = {}
        self._tab_metadata: Dict[str, Dict] = {}
        self._cache_lock = threading.RLock()
        self._auto_update_workers: List[SourcesRefreshWorker] = []
        self._stats_cache: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._tab_revision: Dict[str, int] = {}

        if self.cache_manager:
            with suppress(Exception):
                cleanup_days = self.config.get('cache_cleanup_days', 30)
                removed = self.cache_manager.cleanup_old(cleanup_days)
                if removed:
                    logger.info(f"Очищено {removed} старых записей кэша")

        self.sources_updated.connect(self._on_sources_updated)

        self._auto_update_timer = QTimer(self)
        self._auto_update_timer.timeout.connect(self._check_auto_update)
        self.apply_auto_update_setting()

        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._on_app_quit)

        logger.info(
            f"ApplicationCore {APP_VERSION} инициализирован")

    def apply_auto_update_setting(self):
        with suppress(Exception):
            enabled = bool(self.config.get('auto_update_sources', True))
            if enabled and not self._auto_update_timer.isActive():
                self._auto_update_timer.start(60 * 60 * 1000)
            elif not enabled and self._auto_update_timer.isActive():
                self._auto_update_timer.stop()

    def save_all_settings(self):
        self.config.save()
        self.apply_auto_update_setting()
        self.settings_changed.emit()

    def set_cache_manager_enabled(self, enabled: bool):
        if enabled and self.cache_manager is None:
            try:
                self.cache_manager = CacheManager()
                self.link_source_manager.cache_manager = self.cache_manager
                self.epg_db.cache_manager = self.cache_manager
            except Exception:
                logger.exception("Не удалось создать CacheManager")
                self.cache_manager = None
        elif not enabled and self.cache_manager is not None:
            with suppress(Exception):
                self.cache_manager.clear_thread_connection()
            self.cache_manager = None
            self.link_source_manager.cache_manager = None
            self.epg_db.cache_manager = None

    def load_epg_from_cache_async(self):
        if self.cache_manager is None:
            return

        def _worker():
            try:
                ttl = int(self.config.get('epg_cache_ttl_hours',
                                           EPG_CACHE_TTL_HOURS))
                cnt = self.epg_db.load_from_cache(ttl)
                if cnt:
                    logger.info(f"EPG загружен из кэша: {cnt} программ")
            except Exception as e:
                logger.debug(f"EPG cache load: {e}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _on_sources_updated(self):
        with suppress(Exception):
            self.link_source_manager.invalidate_cache()

    def _on_app_quit(self):
        with suppress(Exception):
            self._auto_update_timer.stop()
        for w in list(self._auto_update_workers):
            with suppress(Exception):
                if w.isRunning():
                    w.stop()
                    w.wait(2000)
        self._auto_update_workers.clear()
        with suppress(Exception):
            self.link_source_manager.shutdown()
        if self.cache_manager:
            with suppress(Exception):
                self.cache_manager.cleanup_old(
                    self.config.get('cache_cleanup_days', 30))
                self.cache_manager.vacuum()

    def _check_auto_update(self):
        sources = self.link_source_manager.get_all_sources()
        to_update = [s for s in sources if s.enabled and s.auto_update
                     and s.should_refresh()]
        if not to_update:
            return

        worker = SourcesRefreshWorker(
            to_update, self.link_source_manager, self.config,
            check_urls=True)
        core_ref = weakref.ref(self)

        def _on_all_done(s, t):
            core = core_ref()
            if core is None:
                return
            with suppress(Exception):
                if s:
                    core.sources_updated.emit()

        worker.all_done.connect(_on_all_done)
        self._auto_update_workers.append(worker)
        worker.worker_done.connect(
            lambda w=worker: self._remove_auto_worker(w))
        worker.start()

    def _remove_auto_worker(self, w: SourcesRefreshWorker):
        if w in self._auto_update_workers:
            self._auto_update_workers.remove(w)

    def register_tab(self, tab: 'PlaylistTab') -> str:
        tab_id = uuid.uuid4().hex
        with self._cache_lock:
            self._tabs[tab_id] = weakref.ref(tab)
            self._tab_metadata[tab_id] = {
                'filepath': getattr(tab, 'filepath', None),
                'created': datetime.now(),
                'modified': False,
                'channels_count': 0,
            }
            self._tab_revision[tab_id] = 0
        return tab_id

    def unregister_tab(self, tab_id: str) -> bool:
        with self._cache_lock:
            if tab_id in self._tabs:
                del self._tabs[tab_id]
                self._tab_metadata.pop(tab_id, None)
                self._stats_cache.pop(tab_id, None)
                self._tab_revision.pop(tab_id, None)
                return True
        return False

    def get_tab(self, tab_id: str) -> Optional['PlaylistTab']:
        with self._cache_lock:
            ref = self._tabs.get(tab_id)
        return ref() if ref else None

    def get_all_tabs(self) -> List['PlaylistTab']:
        with self._cache_lock:
            refs = list(self._tabs.values())
        result = []
        for r in refs:
            t = r()
            if t is not None:
                result.append(t)
        return result

    def update_tab_metadata(self, tab_id: str, **kwargs):
        with self._cache_lock:
            if tab_id in self._tab_metadata:
                self._tab_metadata[tab_id].update(kwargs)

    def update_channels(self, tab_id: str,
                        channels: List[ChannelData]) -> None:
        with self._cache_lock:
            tab = self.get_tab(tab_id)
            if tab is None:
                return
            tab.all_channels = channels
            self._tab_metadata[tab_id]['modified'] = True
            self._tab_metadata[tab_id]['channels_count'] = len(channels)
            self._tab_revision[tab_id] = \
                self._tab_revision.get(tab_id, 0) + 1
        self.channels_updated.emit(tab_id)

    def get_stats(self, tab_id: str) -> Dict[str, Any]:
        with self._cache_lock:
            tab = self.get_tab(tab_id)
            if tab is None:
                return {'total': 0, 'with_url': 0, 'without_url': 0,
                        'working': 0, 'not_working': 0, 'unknown': 0,
                        'groups': 0, 'unsupported': 0}
            channels = tab.all_channels
            revision = self._tab_revision.get(tab_id, 0)
            cached = self._stats_cache.get(tab_id)
            if cached and cached[0] == revision:
                return cached[1]

        total = len(channels)
        with_url = working = not_working = unknown = unsupported = 0
        groups_set: Set[str] = set()
        for ch in channels:
            has = ch.has_valid_url
            if has:
                with_url += 1
            if ch.status.url_status is True:
                working += 1
            elif ch.status.url_status is False:
                not_working += 1
            elif has:
                unknown += 1
            if ch.status.link_quality == LinkQuality.UNSUPPORTED:
                unsupported += 1
            if ch.meta.group:
                groups_set.add(ch.meta.group)
        stats = {
            'total': total, 'with_url': with_url,
            'without_url': total - with_url,
            'working': working, 'not_working': not_working,
            'unknown': unknown, 'unsupported': unsupported,
            'groups': len(groups_set),
        }
        with self._cache_lock:
            self._stats_cache[tab_id] = (revision, stats)
        return stats

    def get_all_groups(self, tab_id: str) -> List[str]:
        tab = self.get_tab(tab_id)
        channels = tab.all_channels if tab else []
        return sorted({ch.meta.group for ch in channels if ch.meta.group})

    def get_sources(self) -> List[LinkSource]:
        return self.link_source_manager.get_all_sources()

    def search_in_sources(self, channel_name: str,
                          settings: Optional[LinkReplacementSettings] = None
                          ) -> List[ChannelData]:
        s = settings or self.link_replacement_settings
        return self.link_source_manager.search_channel(
            channel_name, s, config=self.config)

    def add_to_blacklist(self, name: str, tvg_id: str = "") -> bool:
        r = self.blacklist_manager.add_channel(name, tvg_id)
        if r:
            self.blacklist_updated.emit()
            with suppress(Exception):
                self.link_source_manager.rebuild_alive_index()
        return r

    def remove_from_blacklist(self, name: str, tvg_id: str = "") -> bool:
        r = self.blacklist_manager.remove_channel(name, tvg_id)
        if r:
            self.blacklist_updated.emit()
            with suppress(Exception):
                self.link_source_manager.rebuild_alive_index()
        return r

    def get_blacklist(self) -> List[Dict[str, str]]:
        return self.blacklist_manager.get_all()

    def clear_blacklist(self) -> bool:
        self.blacklist_manager.clear()
        self.blacklist_updated.emit()
        with suppress(Exception):
            self.link_source_manager.rebuild_alive_index()
        return True

    def apply_blacklist_to_channels(self, channels: List[ChannelData]
                                    ) -> Tuple[List[ChannelData], int]:
        return self.blacklist_manager.filter_channels(channels)

    def add_domain_to_blacklist(self, value: str,
                                include_subdomains: bool = True,
                                note: str = "") -> bool:
        r = self.domain_blacklist_manager.add_domain(
            value, include_subdomains=include_subdomains, note=note)
        if r:
            self.domain_blacklist_updated.emit()
            with suppress(Exception):
                self.link_source_manager.rebuild_alive_index()
        return r

    def remove_domain_from_blacklist(self, value: str) -> bool:
        r = self.domain_blacklist_manager.remove_domain(value)
        if r:
            self.domain_blacklist_updated.emit()
            with suppress(Exception):
                self.link_source_manager.rebuild_alive_index()
        return r

    def clear_domain_blacklist(self) -> bool:
        self.domain_blacklist_manager.clear()
        self.domain_blacklist_updated.emit()
        with suppress(Exception):
            self.link_source_manager.rebuild_alive_index()
        return True

    def get_domain_blacklist(self) -> List[Dict[str, Any]]:
        return self.domain_blacklist_manager.get_all_dicts()

    def apply_domain_blacklist(self, channels: List[ChannelData]
                               ) -> Tuple[List[ChannelData], int]:
        """LEGACY: удаляет каналы (для CLI)."""
        return self.domain_blacklist_manager.filter_channels(channels)

    def apply_domain_blacklist_clean(self, channels: List[ChannelData]
                                     ) -> Tuple[List[ChannelData], int]:
        """UI: очищает ссылки, каналы сохраняются."""
        return self.domain_blacklist_manager.clean_channels(channels)

    def apply_domain_user_agent(self, channels: List[ChannelData]) -> int:
        return self.domain_user_agent_manager.apply_rules_to_channels(channels)

    def get_user_agent_for_url(self, url: str) -> Optional[str]:
        return self.domain_user_agent_manager.get_user_agent_for_url(url)

    def should_remove_user_agent(self, url: str) -> bool:
        return self.domain_user_agent_manager.should_remove_user_agent(url)

    def get_replacement_settings(self) -> LinkReplacementSettings:
        return self.link_replacement_settings

    def get_cached_check_result(self, name: str, url: str
                                ) -> Optional[Dict[str, Any]]:
        if self.cache_manager is None:
            return None
        if not self.config.get('use_check_result_cache', True):
            return None
        ttl = int(self.config.get('check_result_cache_ttl_hours',
                                    CHECK_RESULT_CACHE_TTL_HOURS))
        return self.cache_manager.get_check_result(name, url, ttl)


class ChannelTableModel(QAbstractTableModel):
    HEADERS = ["№", "Название", "Группа", "TVG-ID", "Логотип",
               "Catchup", "URL"]
    URL_COLUMN = 6
    COL_NAME = 1
    COL_GROUP = 2
    COL_TVG_ID = 3
    COL_TVG_LOGO = 4
    COL_CATCHUP = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._channels: List[ChannelData] = []
        self._filtered: List[ChannelData] = []
        self._search_text = ""
        self._group_filter = GROUP_FILTER_ALL
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder

    def _reset_with_filter(self):
        self.beginResetModel()
        self._apply_filter_internal()
        if self._sort_column >= 0:
            self._sort_internal()
        self.endResetModel()

    def set_channels(self, channels: List[ChannelData]):
        self._channels = channels
        self._reset_with_filter()

    def set_search_text(self, text: str):
        self._search_text = (text or "").lower()
        self._reset_with_filter()

    def set_group_filter(self, group: str):
        self._group_filter = group
        self._reset_with_filter()

    @staticmethod
    def _has_any_metadata(ch: ChannelData) -> bool:
        return bool(
            ch.meta.tvg_id or ch.meta.tvg_logo or ch.meta.tvg_name
            or (ch.meta.group and ch.meta.group != DEFAULT_GROUP)
            or ch.link.user_agent)

    def _apply_filter_internal(self):
        filtered = list(self._channels)
        if self._group_filter != GROUP_FILTER_ALL:
            filtered = [ch for ch in filtered
                        if ch.meta.group == self._group_filter]
        s = (self._search_text or "").strip()
        special = None
        if s.startswith("is:"):
            special = s[3:].strip()
            s = ""
        if special == "orphan":
            filtered = [ch for ch in filtered
                        if not ch.has_valid_url
                        and self._has_any_metadata(ch)]
        elif special == "broken":
            filtered = [ch for ch in filtered
                        if ch.status.url_status is False]
        elif special == "working":
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
        if column == 0:
            if ch.original_index >= 0:
                return (0, ch.original_index)
            return (1, 0)
        if column == self.COL_NAME:
            return (ch.meta.name or "").lower()
        if column == self.COL_GROUP:
            return (ch.meta.group or "").lower()
        if column == self.COL_TVG_ID:
            return (ch.meta.tvg_id or "").lower()
        if column == self.COL_TVG_LOGO:
            return (ch.meta.tvg_logo or "").lower()
        if column == self.COL_CATCHUP:
            return (ch.meta.catchup or "").lower()
        if column == self.URL_COLUMN:
            return (ch.link.url or "").lower()
        return ""

    def sort(self, column: int,
             order: Qt.SortOrder = Qt.SortOrder.AscendingOrder):
        self._sort_column = column
        self._sort_order = order
        self.beginResetModel()
        self._sort_internal()
        self.endResetModel()

    def _sort_internal(self):
        if self._sort_column < 0:
            return
        reverse = (self._sort_order == Qt.SortOrder.DescendingOrder)
        with suppress(TypeError):
            self._filtered.sort(
                key=lambda ch: self._sort_key(ch, self._sort_column),
                reverse=reverse)

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
        return len(self.HEADERS)

    def headerData(self, section, orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and \
                role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ch = self._filtered[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == 0:
                return str(index.row() + 1)
            if col == self.COL_NAME:
                return ch.meta.name
            if col == self.COL_GROUP:
                return ch.meta.group
            if col == self.COL_TVG_ID:
                return ch.meta.tvg_id
            if col == self.COL_TVG_LOGO:
                return ch.meta.tvg_logo
            if col == self.COL_CATCHUP:
                return ch.meta.catchup
            if col == self.URL_COLUMN:
                return ch.link.url
        if role == Qt.ItemDataRole.UserRole:
            return ch.uid

        if col == self.COL_NAME and role == Qt.ItemDataRole.BackgroundRole:
            if not ch.has_valid_url and self._has_any_metadata(ch):
                return URL_FG_COLORS['orphan_bg']
            return None

        if col == self.URL_COLUMN:
            if role == Qt.ItemDataRole.ForegroundRole:
                return ch.get_url_foreground()
            if role == Qt.ItemDataRole.BackgroundRole:
                if ch.link.user_agent:
                    return QColor(220, 255, 220)
                return None
        if role == Qt.ItemDataRole.ToolTipRole:
            return ch.get_status_tooltip()
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 0:
            return Qt.AlignmentFlag.AlignCenter
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() != 0:
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def setData(self, index: QModelIndex, value,
                role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        ch = self._filtered[index.row()]
        col = index.column()
        new_value = str(value).strip()
        if col == self.COL_NAME:
            if ch.meta.name == new_value:
                return False
            ch.meta.name = new_value
        elif col == self.COL_GROUP:
            if ch.meta.group == new_value:
                return False
            ch.meta.group = new_value or DEFAULT_GROUP
        elif col == self.COL_TVG_ID:
            if ch.meta.tvg_id == new_value:
                return False
            ch.meta.tvg_id = new_value
        elif col == self.COL_TVG_LOGO:
            if ch.meta.tvg_logo == new_value:
                return False
            ch.meta.tvg_logo = new_value
        elif col == self.COL_CATCHUP:
            if ch.meta.catchup == new_value:
                return False
            ch.meta.catchup = new_value
        elif col == self.URL_COLUMN:
            if ch.link.url == new_value:
                return False
            core = ApplicationCore.instance()
            if ch.has_valid_url and new_value:
                max_alts = core.get_replacement_settings().max_alternative_urls
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
            core.domain_user_agent_manager.apply_rules_to_channel(ch)
        else:
            return False
        if col != self.URL_COLUMN:
            ch.update_extinf()
        object.__setattr__(ch, 'modified_date', datetime.now())
        self.dataChanged.emit(index, index)
        return True

    def refresh_row(self, row: int):
        if 0 <= row < len(self._filtered):
            idx = self.index(row, 0)
            idx2 = self.index(row, self.columnCount() - 1)
            self.dataChanged.emit(idx, idx2)

    def refresh_all(self):
        if self._filtered:
            top = self.index(0, 0)
            bot = self.index(len(self._filtered) - 1,
                             self.columnCount() - 1)
            self.dataChanged.emit(top, bot)


class _NumericItem(QTableWidgetItem):
    def __init__(self, value: int):
        super().__init__(str(value))
        self._value = int(value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        try:
            other_val = int(other.text())
            return self._value < other_val
        except (ValueError, AttributeError):
            return super().__lt__(other)


_META_CHECKS = (
    ('tvg_id',      lambda ch: bool(ch.meta.tvg_id)),
    ('tvg_logo',    lambda ch: bool(ch.meta.tvg_logo)),
    ('tvg_name',    lambda ch: bool(ch.meta.tvg_name)),
    ('group_title', lambda ch: bool(ch.meta.group and
                                    ch.meta.group != DEFAULT_GROUP)),
    ('user_agent',  lambda ch: bool(ch.link.user_agent)),
)


class PlaylistTab(QWidget):
    """Вкладка плейлиста."""

    undo_state_changed = pyqtSignal(bool, bool)
    info_changed = pyqtSignal(str)

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

        self.core = ApplicationCore.instance()
        self.tab_id = self.core.register_tab(self)
        self.header_manager = PlaylistHeaderManager()
        self.undo_manager = UndoRedoManager()

        self._src_check_worker: Optional[SourceUrlCheckWorker] = None
        self._replacement_worker: Optional[LinkReplacementWorker] = None
        self._loading = False
        self._suppress_state_save = False
        self._pending_sync = False
        self._needs_resort = False
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

        self.core.channels_updated.connect(self._on_core_channels_updated)

        if filepath and os.path.exists(filepath):
            _fp = filepath
            QTimer.singleShot(0, lambda: self._load_file(_fp))
        else:
            self.refresh_view()

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

        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in (2, 3, 4, 5):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 150)
        self.table.setColumnWidth(5, 100)

        h.blockSignals(True)
        h.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        h.blockSignals(False)
        h.sortIndicatorChanged.connect(self._on_sort_indicator_changed)
        self.model.sort(0, Qt.SortOrder.AscendingOrder)

        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.EditKeyPressed)

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
            sh.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sh.activated.connect(slot)
            self._shortcuts.append(sh)

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
        if not _is_qobject_valid(self):
            return
        if not self._pending_sync:
            return
        self._pending_sync = False
        self.core.update_channels(self.tab_id, self.all_channels)

    def flush_sync(self):
        if self._sync_timer.isActive():
            self._sync_timer.stop()
        self._do_sync()

    def refresh_view(self):
        self._suppress_state_save = True
        try:
            if (self.core.config.get('preserve_original_order', True)
                    and self._needs_resort):
                self.all_channels.sort(
                    key=lambda ch: ch.original_index
                    if ch.original_index >= 0 else float('inf'))
                self._needs_resort = False
            self.model.set_channels(self.all_channels)
        finally:
            self._suppress_state_save = False
        self.selected_channels = []
        self.current_channel = None
        self.update_info()

    def find_channel_by_ref(self, channel: ChannelData
                             ) -> Optional[ChannelData]:
        if channel is None:
            return None
        target = next(
            (c for c in self.all_channels if c.uid == channel.uid), None)
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
        if force_save:
            self.save_state(description)
            self._do_save_state()
        else:
            self.save_state(description)
        with self._suppress_save():
            yield self.all_channels
        self.sync_to_core()
        if refresh == 'reset':
            self.model.set_channels(self.all_channels)
        else:
            self.model.refresh_all()
        self.update_info()

    def _load_file(self, filepath: str):
        self._loading = True
        self._suppress_state_save = True
        try:
            try:
                with open(filepath, 'rb') as f:
                    raw = f.read()
                detected_enc = 'utf-8'
                content = None
                for enc in ('utf-8', 'utf-8-sig',
                            'windows-1251', 'cp1251'):
                    try:
                        content = raw.decode(enc)
                        detected_enc = enc
                        break
                    except UnicodeDecodeError:
                        continue
                if content is None:
                    content = raw.decode('utf-8', errors='replace')
                    detected_enc = 'utf-8'
            except FileNotFoundError:
                error_box(self, f"Файл не найден:\n{filepath}")
                self.all_channels = []
                self.refresh_view()
                return
            except PermissionError:
                error_box(self, f"Нет доступа к файлу:\n{filepath}")
                self.all_channels = []
                self.refresh_view()
                return
            self.header_manager.parse_header(content)
            self.header_manager.original_encoding = detected_enc
            source_name = os.path.basename(filepath)
            parsed = M3UParser.parse(content, source_name)
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
                            self.core.apply_blacklist_to_channels(parsed)
                        if removed_by_bl:
                            logger.info(
                                f"Удалено {removed_by_bl} по ч.с. каналов")
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
                        if cleaned_by_domain_bl:
                            logger.info(
                                f"Очищено {cleaned_by_domain_bl} ссылок "
                                f"по ч.с. домен/IP")
                except Exception:
                    logger.exception("Domain blacklist error")
            else:
                self.all_channels = parsed

            try:
                if self.core.domain_user_agent_manager.get_all_rules():
                    modified = self.core.apply_domain_user_agent(
                        self.all_channels)
                    if modified:
                        logger.info(f"UA применён к {modified} каналам")
            except Exception:
                logger.exception("UA rules error")

            if apply_filters and (removed_by_bl or cleaned_by_domain_bl):
                info_box(
                    self,
                    "При открытии применены фильтры:\n"
                    f"• удалено по ЧС каналов: {removed_by_bl}\n"
                    f"• очищено ссылок по ЧС домен/IP: "
                    f"{cleaned_by_domain_bl}\n"
                    "Каналы без ссылок сохранены и помечены.\n"
                    "Отключить фильтры можно в настройках "
                    "или в менеджере ЧС.",
                    "Фильтры применены")

            self.undo_manager.reset(self.all_channels)
            self.modified = False
            self._needs_resort = True
            self.refresh_view()
            self.update_modified_status()
        except Exception as e:
            logger.exception("Load file error")
            self.all_channels = []
            self.modified = False
            self.refresh_view()
            error_box(self, f"Не удалось загрузить:\n{e}")
        finally:
            self._suppress_state_save = False
            self._loading = False

    def apply_epg_metadata_to_all(self, silent: bool = False) -> bool:
        if not _is_qobject_valid(self):
            return False
        if not self.core.epg_db.has_channel_info:
            if not silent:
                info_box(self, "EPG не загружен (или не содержит "
                               "<channel>-записей).\n"
                               "Загрузите EPG через "
                               "«Инструменты → Загрузить EPG».", "EPG")
            return False
        if not self.all_channels:
            return False
        if self._epg_meta_worker and self._epg_meta_worker.isRunning():
            if not silent:
                info_box(self, "Применение EPG уже идёт")
            return False

        self._epg_meta_generation += 1
        generation = self._epg_meta_generation

        if not silent:
            self.save_state("Применение EPG-метаданных")
        w = self.parent_window
        if w is not None and not silent:
            w.set_action("⏳ Применение EPG-метаданных...")

        worker = EPGMetadataApplyWorker(
            self.all_channels, self.core.epg_db, self.core.config)
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
                object.__setattr__(ch, 'modified_date', datetime.now())
                modified += 1

            if modified:
                if not silent:
                    self.undo_manager.save_state(
                        self.all_channels, "Применение EPG-метаданных")
                self.sync_to_core()
                with self._suppress_save():
                    self.model.refresh_all()
                self.update_info()
                logger.info(
                    f"EPG-метаданные применены к {modified} каналам")
                if not silent:
                    info_box(self, f"Обновлено каналов: {modified}", "EPG")
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

        worker.progress.connect(on_progress)
        worker.applied.connect(on_applied)
        worker.error.connect(on_error)
        worker.start()
        return True

    def update_info(self):
        st = self.core.get_stats(self.tab_id)
        info = (f"Каналов: {st['total']} | С URL: {st['with_url']} | "
                f"✓: {st['working']} | ✗: {st['not_working']} | "
                f"?: {st['unknown']} | Групп: {st['groups']}")
        if st.get('unsupported'):
            info += f" | N/A: {st['unsupported']}"
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
        if rows:
            max_row = max(rows)
            self.current_channel = self.model.channel_at(max_row)
        else:
            self.current_channel = None

    def _on_model_data_changed(self, top_left, bottom_right, roles=None):
        if self._suppress_state_save or self._loading:
            return
        if roles is not None and all(
                r in (Qt.ItemDataRole.DisplayRole,
                      Qt.ItemDataRole.ForegroundRole,
                      Qt.ItemDataRole.BackgroundRole,
                      Qt.ItemDataRole.ToolTipRole,
                      Qt.ItemDataRole.TextAlignmentRole)
                for r in roles):
            self.update_info()
            return
        self.save_state("Правка ячейки")
        self.sync_to_core()
        self.update_info()

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

    def _apply_diff(self, diff: Dict[str, Any], reverse: bool = False):
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
                object.__setattr__(ch, 'modified_date', datetime.now())

        target_order = old_order if reverse else new_order
        add_data = removed if reverse else added
        remove_uids = {uid for uid, _ in (added if reverse else removed)}

        existing_uids = {ch.uid for ch in self.all_channels}
        for uid, data in add_data:
            if uid not in existing_uids and data:
                new_ch = ChannelData.from_dict(data)
                if new_ch.uid in existing_uids:
                    object.__setattr__(new_ch, 'uid',
                                       ChannelData._next_uid())
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
        self.modified = True
        self.sync_to_core()
        self.refresh_view()
        self.update_modified_status()

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

    def update_modified_status(self):
        self.core.update_tab_metadata(self.tab_id, modified=self.modified)
        w = self.parent_window
        if w is not None:
            w._update_window_title()

    def _play_in_player(self, row: int):
        ch = self.channel_for_row(row)
        if not ch:
            return
        if not ch.has_valid_url:
            warn_box(self, "Нет URL", "Плеер")
            return
        if not is_vlc_available():
            error_box(self,
                      f"Встроенный плеер недоступен.\n\n"
                      f"{get_vlc_error()}\n\n"
                      f"Установите VLC: "
                      f"https://www.videolan.org/vlc/",
                      "Плеер")
            return
        playlist = self.model.channels_filtered()
        dlg = EmbeddedPlayerDialog(ch, self, playlist=playlist)
        dlg.exec()
        w = self.parent_window
        if w is not None and _is_qobject_valid(w):
            w.set_action(f"▶ Воспроизведение: {ch.meta.name}")

    def _check_selected_urls(self):
        if not self.selected_channels:
            info_box(self, "Не выбрано ни одного канала")
            return
        with_urls = [ch for ch in self.selected_channels if ch.has_valid_url]
        if not with_urls:
            info_box(self, "У выбранных каналов нет URL")
            return
        self.check_urls_async(with_urls, force=True)

    def check_all_urls(self):
        with_urls = [ch for ch in self.all_channels if ch.has_valid_url]
        if not with_urls:
            info_box(self, "Нет каналов с URL")
            return
        if not confirm(self, f"Проверить {len(with_urls)} ссылок?"):
            return
        self.check_urls_async(with_urls, force=True)

    def _check_single_url(self, row: int):
        ch = self.channel_for_row(row)
        if ch and ch.link.url:
            self.check_urls_async([ch], force=True)

    def _apply_cached_check(self, ch: ChannelData) -> bool:
        cached = self.core.get_cached_check_result(
            ch.meta.name, ch.link.url)
        if cached is None:
            return False
        ch.status.url_status = cached['alive']
        ch.status.url_check_time = (
            datetime.fromtimestamp(cached['last_check'])
            if cached['last_check'] else None)
        ch.status.link_response_time = (
            cached['response_ms'] / 1000.0
            if cached['response_ms'] else None)
        ch.status.status_text = (cached['status_text']
                                  or StatusText.UNCHECKED)
        ch.status.status_code = cached['status_code']
        if cached['alive']:
            ch.status.link_quality = LinkQuality.WORKING
        else:
            ch.status.link_quality = LinkQuality.NOT_WORKING
        return True

    def check_urls_async(self, channels: List[ChannelData],
                         force: bool = True):
        """ЕДИНЫЙ механизм проверки — как у источников.

        Использует SourceUrlCheckWorker (тот же, что «Обновить всё»
        в менеджере источников).
        """
        if not _is_qobject_valid(self):
            return
        if self._src_check_worker is not None \
                and self._src_check_worker.isRunning():
            info_box(self, "Проверка уже запущена")
            return

        to_check: List[ChannelData] = list(channels)
        if not to_check:
            return

        limit_enabled = self.core.config.get('limit_check_enabled', False)
        limit = int(self.core.config.get('max_channels_to_check', 2000))
        if limit_enabled and len(to_check) > limit:
            to_check = to_check[:limit]

        settings = self.core.get_replacement_settings()
        w = self.parent_window
        if w is not None:
            w.set_action(
                f"⏳ Проверка ссылок: {len(to_check)} каналов")

        timeout = max(int(settings.check_timeout or 3), 3)
        max_workers = int(self.core.config.get('source_check_workers', 4))
        trust_sec = 0
        batch_size = int(self.core.config.get(
            'source_check_batch_size', 100))

        worker = SourceUrlCheckWorker(
            source_name="__playlist__",
            channels=to_check,
            cache_manager=self.core.cache_manager,
            max_workers=max_workers,
            timeout=timeout,
            trust_sec=trust_sec,
            batch_size=batch_size,
            stop_token=None,
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
            self.update_info()
            with suppress(Exception):
                self.core.link_source_manager.rebuild_alive_index()
            if _is_qobject_valid(self):
                info_box(
                    self,
                    f"Проверка завершена.\n"
                    f"Рабочих: {working} из {total}",
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
        if confirm_msg:
            if not confirm(self, confirm_msg):
                return 0
        self.save_state(description)
        for ch in targets:
            ch.clear_url()
            ch.update_extinf()
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()
        return len(targets)

    def _remove_broken_url(self, row: int):
        ch = self.channel_for_row(row)
        if ch is None or ch.status.url_status is not False:
            return
        self._clear_urls([ch], "Удаление битой ссылки", only_broken=True)

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
            confirm_msg=f"Удалить все ссылки из {len(self.all_channels)}?")
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
        return len(targets)

    def _new_channel(self):
        self.save_state("Создание нового канала")
        ch = ChannelData()
        ch.meta.name = "Новый канал"
        ch.meta.group = DEFAULT_GROUP
        object.__setattr__(ch, 'original_index', -1)
        ch.update_extinf()
        self.all_channels.append(ch)
        self.sync_to_core()
        with self._suppress_save():
            self.model.set_channels(self.all_channels)
        row = self.model.row_of_uid(ch.uid)
        if row >= 0:
            self.table.setCurrentIndex(self.model.index(row, 1))
            self.table.edit(self.model.index(row, 1))

    def _delete_channel(self, row: int = -1):
        if row == -1:
            if self.selected_channels:
                uids = {ch.uid for ch in self.selected_channels}
                n = len(uids)
            elif self.current_channel:
                uids = {self.current_channel.uid}
                n = 1
            else:
                return
        else:
            ch = self.channel_for_row(row)
            if ch is None:
                return
            uids = {ch.uid}
            n = 1
        if n == 1:
            ch = next((c for c in self.all_channels if c.uid in uids), None)
            msg = (f"Удалить канал '{ch.meta.name}'?" if ch
                   else "Удалить канал?")
        else:
            msg = f"Удалить выбранные {n} каналов?"
        if not confirm(self, msg):
            return
        self.save_state("Удаление канала")
        self.all_channels = [ch for ch in self.all_channels
                             if ch.uid not in uids]
        self.sync_to_core()
        self.selected_channels = []
        self.current_channel = None
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

    def _delete_selected_channels(self):
        if self.selected_channels:
            self._delete_channel()

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
        return bool(ch.meta.tvg_id or ch.meta.tvg_logo or ch.meta.tvg_name
                    or (ch.meta.group and ch.meta.group != DEFAULT_GROUP)
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
            info_box(self, f"Не удалось определить домен/IP из URL:\n"
                           f"{target.link.url[:120]}")
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
        blocked_count = 0
        for ch in self.all_channels:
            if ch.link.url and mgr.matches_url(ch.link.url):
                blocked_count += 1
        if blocked_count == 0:
            return 0

        self.flush_pending_state()
        self.save_state("Очистка ссылок по ЧС домен/IP")
        self.flush_pending_state()
        _, cleaned = self.core.apply_domain_blacklist_clean(
            self.all_channels)
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()
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
        if self.core.add_to_blacklist(name, tid):
            if ch in self.all_channels:
                self.save_state("Добавление в ч.с.")
                self.all_channels.remove(ch)
                self.sync_to_core()
                with self._suppress_save():
                    self.model.set_channels(self.all_channels)

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
            self.save_state("Пакетное добавление в ч.с.")
            del_uids = {c.uid for c in chs}
            self.all_channels = [ch for ch in self.all_channels
                                 if ch.uid not in del_uids]
            self.sync_to_core()
            with self._suppress_save():
                self.model.set_channels(self.all_channels)
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
        ua = DomainUserAgentRule._sanitize_ua(new_ua)
        ch.link.user_agent = ua
        if ua:
            ch.link.extra_headers['User-Agent'] = ua
        else:
            for k in list(ch.link.extra_headers.keys()):
                if k.lower() == 'user-agent':
                    del ch.link.extra_headers[k]
        ch.update_extvlcopt_from_headers()
        self.sync_to_core()
        self.model.refresh_row(row)

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
                ua = DomainUserAgentRule._sanitize_ua(
                    changes['user_agent'])
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
            object.__setattr__(ch, 'modified_date', datetime.now())
        self.sync_to_core()
        with self._suppress_save():
            self.model.refresh_all()
        info_box(self, f"Обновлено {len(self.selected_channels)} каналов",
                 "Готово")

    def _copy_channel(self):
        w = self.parent_window
        if self.current_channel and w is not None:
            w.copied_channel = self.current_channel.copy()

    def _cut_channel(self):
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
        self.sync_to_core()
        self.selected_channels = []
        self.current_channel = None
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

    def _cut_selected_channels(self):
        if self.selected_channels:
            self._copy_selected_channels()
            self._delete_selected_channels()

    def _copy_selected_channels(self):
        w = self.parent_window
        if self.selected_channels and w is not None:
            w.copied_channels = [ch.copy()
                                 for ch in self.selected_channels]

    def _paste_channel(self):
        w = self.parent_window
        if w is None or not w.copied_channel:
            return
        self.save_state("Вставка канала")
        ch = w.copied_channel.copy()
        object.__setattr__(ch, 'original_index', -1)
        if self.current_channel and self.current_channel in self.all_channels:
            idx = self.all_channels.index(self.current_channel) + 1
            self.all_channels.insert(idx, ch)
        else:
            self.all_channels.append(ch)
        self.sync_to_core()
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

    def _paste_selected_channels(self):
        w = self.parent_window
        if w is None or not w.copied_channels:
            return
        self.save_state("Вставка каналов")
        base_idx = len(self.all_channels)
        if self.current_channel and self.current_channel in self.all_channels:
            base_idx = self.all_channels.index(self.current_channel) + 1
        for offset, ch in enumerate(w.copied_channels):
            new = ch.copy()
            object.__setattr__(new, 'original_index', -1)
            self.all_channels.insert(base_idx + offset, new)
        self.sync_to_core()
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

    def _rename_groups(self):
        if not self.selected_channels:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Пакетное переименование")
        dlg.resize(400, 200)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(f"Выбрано: {len(self.selected_channels)}"))
        groups = {ch.meta.group for ch in self.selected_channels}
        cur = next(iter(groups)) if len(groups) == 1 else "РАЗНЫЕ"
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
        if row == -1:
            ch = self.current_channel
        else:
            ch = self.channel_for_row(row)
        if ch is None:
            return
        try:
            idx = self.all_channels.index(ch)
        except ValueError:
            return
        if idx > 0:
            with self._edit_channels("Перемещение вверх"):
                self.all_channels[idx], self.all_channels[idx - 1] = \
                    self.all_channels[idx - 1], self.all_channels[idx]

    def _move_channel_down(self, row: int = -1):
        if row == -1:
            ch = self.current_channel
        else:
            ch = self.channel_for_row(row)
        if ch is None:
            return
        try:
            idx = self.all_channels.index(ch)
        except ValueError:
            return
        if idx < len(self.all_channels) - 1:
            with self._edit_channels("Перемещение вниз"):
                self.all_channels[idx], self.all_channels[idx + 1] = \
                    self.all_channels[idx + 1], self.all_channels[idx]

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
        self.sync_to_core()
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

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
        self.sync_to_core()
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

    def _apply_alternative_url(self, channel: ChannelData, url: str):
        if not url or not url.strip():
            return
        self.save_state("Применение альтернативной ссылки")
        if channel.link.url and channel.link.url != url and \
                channel.link.url not in channel.link.alternative_urls:
            channel.link.alternative_urls.append(channel.link.url)
        if url in channel.link.alternative_urls:
            channel.link.alternative_urls.remove(url)
        channel.link.url = url
        channel.link.has_url = True
        channel.status.reset()
        object.__setattr__(channel, 'modified_date', datetime.now())
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

    def _add_link_from_sources(self, row: int):
        ch = self.channel_for_row(row)
        if ch is None:
            return
        if ch.has_valid_url:
            info_box(self, f"'{ch.meta.name}' уже имеет ссылку.")
            return
        alts = self.core.search_in_sources(ch.meta.name)
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
        self.save_state("Добавление ссылки из источников")
        if channel.link.url and channel.link.url != source_ch.link.url:
            if channel.link.url not in channel.link.alternative_urls:
                channel.link.alternative_urls.append(channel.link.url)
        channel.link.url = source_ch.link.url
        channel.link.has_url = True
        channel.status.reset()
        object.__setattr__(channel, 'modified_date', datetime.now())
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
        alts = self.core.search_in_sources(ch.meta.name)
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
        candidates: List[Tuple[ChannelData, List[ChannelData]]] = []
        for ch in self.selected_channels:
            alts = self.core.search_in_sources(ch.meta.name)
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
            if not ch.has_valid_url or ch.link.url == selected.link.url:
                continue
            if ch.link.url not in ch.link.alternative_urls:
                ch.link.alternative_urls.append(ch.link.url)
            ch.link.url = selected.link.url
            ch.link.has_url = True
            ch.status.reset()
            object.__setattr__(ch, 'modified_date', datetime.now())
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
                "Откройте «Инструменты → Источники ссылок» и добавьте\n"
                "хотя бы один источник. Затем нажмите "
                "«🔄 Обновить всё»,\n"
                "чтобы загрузить каналы и наполнить кэш живых URL.",
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
                "Автозамена теперь работает ИСКЛЮЧИТЕЛЬНО из кэша "
                "(без сетевых проверок).\n\n"
                "Нажмите «Да», чтобы открыть менеджер источников и "
                "запустить «🔄 Обновить всё».\n"
                "«Нет» — продолжить замену (вероятно, 0 результатов).\n"
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
        if self._replacement_worker and \
                self._replacement_worker.isRunning():
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
            'replacement_max_workers', REPLACEMENT_MAX_WORKERS_DEFAULT))
        self._replacement_worker = LinkReplacementWorker(
            to_process, self.core.link_source_manager, settings,
            config=self.core.config, max_workers=max_workers)

        uid_map: Dict[int, ChannelData] = {
            ch.uid: ch for ch in self.all_channels}

        def on_channel_updated(uid: int, old_url: str, new_url: str,
                                name: str):
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
                        ch.link.alternative_urls[:s.max_alternative_urls]
            ch.link.url = new_url
            ch.link.has_url = True
            ch.status.reset()
            object.__setattr__(ch, 'modified_date', datetime.now())

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
            self.undo_state_changed.emit(self.undo_manager.can_undo(),
                                         self.undo_manager.can_redo())
            if replaced == 0:
                info_box(
                    self,
                    "Заменено 0 из " + str(total) + ".\n\n"
                    "Автозамена работает только из кэша "
                    "url_status_cache.\n"
                    "Возможные причины:\n"
                    "• источники не обновлялись с проверкой URL;\n"
                    "• живых URL нет в кэше;\n"
                    "• имена каналов не совпадают.\n\n"
                    "Откройте «Инструменты → Источники ссылок» и нажмите\n"
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
            on_channel_updated)
        self._replacement_worker.progress.connect(on_progress)
        self._replacement_worker.replacement_done.connect(on_done)
        self._replacement_worker.error.connect(on_error)
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
        self.header_manager.custom_attributes = dict(hm.custom_attributes)
        self.header_manager.playlist_name = hm.playlist_name
        self.header_manager.has_extm3u = hm.has_extm3u
        self.header_manager._original_attrs = dict(hm._original_attrs)
        self.modified = True
        self.update_modified_status()

    def load_epg_async(self):
        if not _is_qobject_valid(self):
            return
        if self._epg_worker and self._epg_worker.isRunning():
            info_box(self, "Загрузка EPG уже идёт")
            return
        urls = list(self.header_manager.epg_sources)
        if not urls:
            url, ok = QInputDialog.getText(
                self, "Загрузка EPG", "URL EPG (XMLTV):",
                QLineEdit.EchoMode.Normal,
                "http://example.com/epg.xml.gz")
            if not ok or not url:
                return
            urls = [url]
        w = self.parent_window
        if w is not None:
            w.set_action("⏳ Загрузка EPG...")
        self._epg_worker = EPGLoaderWorker(
            urls, self.core.epg_db, timeout=EPG_LOAD_TIMEOUT_SEC)

        def on_progress(value, total, text):
            w2 = self.parent_window
            if w2 is not None:
                w2.show_progress(value, total, text)

        def on_loaded(count, errors):
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
                for err in errors:
                    logger.warning(f"EPG error: {err}")
            info_box(self, msg, "EPG")
            self.core.epg_updated.emit()
            self._epg_worker = None

        def on_error(msg):
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                error_box(self, msg, "Ошибка EPG")
            self._epg_worker = None

        self._epg_worker.progress.connect(on_progress)
        self._epg_worker.epg_loaded.connect(on_loaded)
        self._epg_worker.error.connect(on_error)
        self._epg_worker.start()

    def apply_blacklist(self) -> int:
        filtered, removed = self.core.apply_blacklist_to_channels(
            self.all_channels)
        if removed > 0:
            with self._edit_channels("Применение ч.с. каналов"):
                self.all_channels[:] = filtered
        return removed

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
                    YES_NO)
                if reply == QMessageBox.StandardButton.Yes:
                    enc = 'utf-8'
            with open(self.filepath, 'w', encoding=enc) as f:
                ht = self.header_manager.get_header_text()
                if ht:
                    f.write(ht)
                save_extvlcopt = self.core.config.get(
                    'save_extvlcopt', True)
                for ch in self.all_channels:
                    f.write(ch.link.extinf + '\n')
                    if save_extvlcopt:
                        for line in ch.link.extvlcopt_lines:
                            f.write(line + '\n')
                    else:
                        if ch.link.user_agent:
                            f.write(
                                f'#EXTVLCOPT:http-user-agent='
                                f'"{ch._escape(ch.link.user_agent)}"\n')
                        for k, v in ch.link.extra_headers.items():
                            if k.lower() == 'user-agent':
                                continue
                            if k.lower() == 'referer':
                                f.write(
                                    f'#EXTVLCOPT:http-referrer='
                                    f'"{ch._escape(v)}"\n')
                            else:
                                f.write(
                                    f'#EXTVLCOPT:http-header='
                                    f'"{k}: {ch._escape(v)}"\n')
                    f.write((ch.link.url or '') + '\n')
            self.modified = False
            self.core.update_tab_metadata(self.tab_id,
                                          filepath=self.filepath)
            with suppress(Exception):
                ssm = self.core.stable_state_manager
                for ch in self.all_channels:
                    key = self._stable_key(ch)
                    if not ssm.get(key) and ch.link.url:
                        ssm.set(key, ch.link.url)
                ssm.save()
            self.update_modified_status()
            return True
        except Exception as e:
            error_box(self, f"Не удалось сохранить:\n{e}")
            return False

    @staticmethod
    def _stable_key(ch: ChannelData) -> str:
        if ch.meta.tvg_id:
            return f"tvg:{ch.meta.tvg_id.strip().lower()}"
        norm = ChannelNameNormalizer.normalize(ch.meta.name or "")
        return f"name:{norm}"

    def _find_stable_url(self, ch: ChannelData) -> Optional[str]:
        ssm = self.core.stable_state_manager
        candidates: List[str] = []
        ref = ssm.get(self._stable_key(ch))
        if ref:
            candidates.append(ref)
        if ch.link.url and ch.link.url not in candidates:
            candidates.append(ch.link.url)
        for u in ch.link.alternative_urls:
            if u and u not in candidates:
                candidates.append(u)

        settings = self.core.get_replacement_settings()
        for u in candidates:
            if not u or not u.strip():
                continue
            if settings.is_blacklisted(u) or \
                    settings.is_filtered_domain(u):
                continue
            ok, rt, _, _ = URLUtils.check_url(
                u, STABLE_CHECK_TIMEOUT_SEC, verify_ssl=False,
                max_retries=0, retry_delay=0.0)
            if ok is True and (
                    rt is None
                    or rt * 1000 <= STABLE_LATENCY_THRESHOLD_MS):
                return u
        return None

    def export_stable(self, filepath: str,
                      progress_cb: Optional[Callable[
                          [int, int, str], None]] = None
                      ) -> Tuple[int, int]:
        ssm = self.core.stable_state_manager
        written = 0
        skipped = 0
        total = len(self.all_channels)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self.header_manager.get_header_text())
                for i, ch in enumerate(self.all_channels):
                    if progress_cb:
                        progress_cb(i, total,
                                    f"Stable: {i+1}/{total}")
                    url = self._find_stable_url(ch)
                    if not url:
                        skipped += 1
                        continue
                    key = self._stable_key(ch)
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
                if not fp.lower().endswith('.csv'):
                    fp += '.csv'
                with open(fp, 'w', encoding='utf-8', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        'name', 'group', 'tvg_id', 'tvg_name', 'tvg_logo',
                        'url', 'user_agent', 'region', 'quality',
                        'timeshift', 'catchup', 'catchup_source',
                        'catchup_days', 'tvg_shift', 'tvg_chno',
                        'audio_track', 'alternative_urls', 'link_source',
                        'url_status', 'status_code', 'epg_source'])
                    for ch in self.all_channels:
                        writer.writerow([
                            ch.meta.name, ch.meta.group, ch.meta.tvg_id,
                            ch.meta.tvg_name, ch.meta.tvg_logo,
                            ch.link.url, ch.link.user_agent,
                            ch.meta.region,
                            ch.meta.quality, ch.meta.timeshift,
                            ch.meta.catchup, ch.meta.catchup_source,
                            ch.meta.catchup_days, ch.meta.tvg_shift,
                            ch.meta.tvg_chno, ch.meta.audio_track,
                            '|'.join(ch.link.alternative_urls),
                            ch.link.link_source,
                            'working' if ch.status.url_status is True else
                            'broken' if ch.status.url_status is False
                            else 'unknown',
                            ch.status.status_code
                            if ch.status.status_code is not None else '',
                            ch.meta.epg_source,
                        ])
            else:
                if not fp.lower().endswith('.json'):
                    fp += '.json'
                with open(fp, 'w', encoding='utf-8') as f:
                    json.dump([ch.to_dict() for ch in self.all_channels],
                              f, ensure_ascii=False, indent=2)
            info_box(self, f"Экспортировано {len(self.all_channels)} каналов",
                     "Готово")
        except Exception as e:
            error_box(self, str(e))

    def remove_metadata(self, opts: Dict[str, bool], scope: str = "all"):
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
        chs = [ch for ch in chs if self._has_meta_to_remove(ch, opts)]
        if not chs:
            info_box(self, "Нет каналов с указанными метаданными")
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
        self.sync_to_core()
        with self._suppress_save():
            self.model.set_channels(self.all_channels)

    @staticmethod
    def _has_meta_to_remove(ch: ChannelData,
                             opts: Dict[str, bool]) -> bool:
        return any(opts.get(k) and check(ch)
                   for k, check in _META_CHECKS)

    def show_duplicate_finder(self):
        if not self.all_channels:
            info_box(self, "Нет каналов")
            return
        use_tvg = bool(self.core.config.get('dedup_by_name_use_tvg',
                                             False))
        keep_dup = bool(self.core.config.get('keep_duplicates', False))
        dlg = DuplicateFinderDialog(self.all_channels, self,
                                     use_tvg_id=use_tvg,
                                     keep_duplicates=keep_dup)

        def on_duplicates_removed(removed: int):
            if removed > 0 and _is_qobject_valid(self):
                self.save_state("Удаление дубликатов")
                self.sync_to_core()
                with self._suppress_save():
                    self.model.set_channels(self.all_channels)
                self.update_info()

        dlg.duplicates_removed.connect(on_duplicates_removed)
        dlg.exec()

    def show_compare(self):
        if not self.all_channels:
            info_box(self, "Нет каналов")
            return
        dlg = ComparePlaylistsDialog(self.all_channels, self)
        dlg.exec()

    @staticmethod
    def _act(text: str, slot: Callable, parent: QMenu) -> QAction:
        return make_action(parent, text, slot)

    def _show_context_menu(self, position: QPoint):
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
                menu.addAction(self._act(
                    "▶ Смотреть в плеере",
                    lambda checked=False, r=row:
                        self._play_in_player(r), menu))
                menu.addSeparator()
            menu.addAction(self._act(
                "Редактировать User Agent...",
                lambda checked=False, r=row:
                    self._edit_user_agent(r), menu))
            if ch.link.alternative_urls:
                alt = menu.addMenu(
                    f"Альтернативные ссылки "
                    f"({len(ch.link.alternative_urls)})")
                try:
                    cached_map = {
                        u: self.core.get_cached_check_result(
                            ch.meta.name, u)
                        for u in ch.link.alternative_urls[:10]
                    }
                except Exception:
                    cached_map = {}
                sorted_alts = sorted(
                    ch.link.alternative_urls,
                    key=lambda u: link_score(
                        ch, u, cached_map.get(u)),
                    reverse=True)[:10]
                for i, u in enumerate(sorted_alts):
                    a = QAction(f"{i + 1}. {u[:60]}...", alt)
                    a.triggered.connect(
                        lambda checked=False, uu=u, cc=ch:
                        self._apply_alternative_url(cc, uu))
                    alt.addAction(a)
            menu.addSeparator()
            if ch.status.url_status is False:
                menu.addAction(self._act(
                    "Удалить битую ссылку",
                    lambda checked=False, r=row:
                        self._remove_broken_url(r), menu))
                menu.addSeparator()
            if ch.meta.tvg_id:
                menu.addAction(self._act(
                    "📺 Показать EPG",
                    lambda checked=False, r=row:
                        self._show_epg_for(r), menu))
            src_ch = self.find_channel_by_ref(ch)
            if src_ch is not None and src_ch.has_valid_url:
                host = URLUtils.extract_host(src_ch.link.url)
                if host:
                    is_ip = URLUtils.is_ip_address(host)
                    label = (f"🚫 Заблокировать IP «{host}»"
                             if is_ip else
                             f"🚫 Заблокировать домен «{host}»")
                    menu.addAction(self._act(
                        label,
                        lambda checked=False, c=src_ch:
                        self.block_domain_from_channel(c),
                        menu))
                menu.addSeparator()
        menu.addAction(self._act("Новый канал", self._new_channel, menu))
        menu.addAction(self._act("Копировать канал",
                                 self._copy_channel, menu))
        menu.addAction(self._act("Вырезать канал",
                                 self._cut_channel, menu))
        menu.addAction(self._act("Вставить канал",
                                 self._paste_channel, menu))
        menu.addSeparator()
        menu.addAction(self._act("Пакетное переименование групп",
                                 self._rename_groups, menu))
        menu.addSeparator()
        menu.addAction(self._act(
            "Добавить в чёрный список каналов",
            lambda checked=False, r=row:
                self._add_to_blacklist(r), menu))
        menu.addSeparator()
        menu.addAction(self._act(
            "Проверить ссылку",
            lambda checked=False, r=row:
                self._check_single_url(r), menu))
        menu.addAction(self._act(
            "Заменить ссылку из источников...",
            lambda checked=False, r=row:
                self.replace_single_link(r), menu))
        menu.addSeparator()
        menu.addAction(self._act(
            "Удалить канал",
            lambda checked=False, r=row:
                self._delete_channel(r), menu))

    def _build_multi_row_menu(self, menu: QMenu, rows: Set[int]):
        cnt = len(rows)
        a = QAction(f"Выбрано каналов: {cnt}", menu)
        a.setEnabled(False)
        menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self._act("Новый канал", self._new_channel, menu))
        menu.addAction(self._act(f"Копировать каналы ({cnt})",
                                 self._copy_selected_channels, menu))
        menu.addAction(self._act(f"Вырезать каналы ({cnt})",
                                 self._cut_selected_channels, menu))
        menu.addAction(self._act(f"Вставить каналы ({cnt})",
                                 self._paste_selected_channels, menu))
        menu.addSeparator()
        menu.addAction(self._act(f"Массовая правка ({cnt})...",
                                 self._mass_edit_selected, menu))
        menu.addAction(self._act("Пакетное переименование групп",
                                 self._rename_groups, menu))
        menu.addSeparator()
        menu.addAction(self._act(f"Удалить выбранные ({cnt})",
                                 self._delete_selected_channels, menu))
        menu.addSeparator()
        menu.addAction(self._act(f"Проверить ссылки ({cnt})",
                                 self._check_selected_urls, menu))
        menu.addAction(self._act(
            f"Заменить ссылки из источников ({cnt})...",
            self.replace_selected_links, menu))
        menu.addAction(self._act(
            f"Добавить в чёрный список каналов ({cnt})",
            self._add_selected_to_blacklist, menu))
        menu.addSeparator()
        menu.addAction(self._act(f"Удалить битые ссылки ({cnt})",
                                 self._remove_selected_broken_urls, menu))
        menu.addAction(self._act(f"Удалить все ссылки ({cnt})",
                                 self._remove_selected_urls, menu))

    def _build_empty_menu(self, menu: QMenu):
        menu.addAction(self._act("Новый канал", self._new_channel, menu))
        menu.addAction(self._act("Вставить канал",
                                 self._paste_channel, menu))
        menu.addSeparator()
        menu.addAction(self._act("Пакетное переименование групп",
                                 self._rename_groups, menu))
        menu.addSeparator()
        menu.addAction(self._act("Поиск дубликатов...",
                                 self.show_duplicate_finder, menu))
        menu.addAction(self._act("Сравнить плейлисты...",
                                 self.show_compare, menu))
        menu.addSeparator()
        menu.addAction(self._act(
            "Применить метаданные из EPG ко всем каналам",
            lambda: self.apply_epg_metadata_to_all(silent=False), menu))

    def _show_epg_for(self, row: int):
        ch = self.channel_for_row(row)
        if not ch or not ch.meta.tvg_id:
            return
        cur = self.core.epg_db.get_current(ch.meta.tvg_id)
        if not cur:
            info_box(self, f"Нет EPG для '{ch.meta.tvg_id}'.\n"
                           "Загрузите EPG через меню "
                           "«Инструменты → Загрузить EPG».",
                     "EPG")
            return
        cat = f"\nКатегория: {cur.category}" if cur.category else ""
        src = (f"\nИсточник: {ch.meta.epg_source}"
               if ch.meta.epg_source else "")
        info_box(self,
                 f"Сейчас: {cur.title}\n"
                 f"{cur.start.strftime('%H:%M') if cur.start else '?'} — "
                 f"{cur.stop.strftime('%H:%M') if cur.stop else '?'}"
                 f"{cat}{src}\n\n"
                 f"{cur.desc or ''}",
                 f"EPG: {ch.meta.name}")

    def disconnect_signals(self):
        for w in (self._src_check_worker, self._replacement_worker,
                  self._epg_worker, self._epg_meta_worker):
            if w and w.isRunning():
                with suppress(Exception):
                    w.stop()
                    w.wait(2000)
        for sh in self._shortcuts:
            with suppress(TypeError, RuntimeError):
                sh.activated.disconnect()
            sh.setParent(None)
            sh.deleteLater()
        self._shortcuts.clear()
        with suppress(TypeError, RuntimeError):
            self.core.channels_updated.disconnect(
                self._on_core_channels_updated)
        with suppress(TypeError, RuntimeError):
            self.model.dataChanged.disconnect(
                self._on_model_data_changed)


class MainWindow(QMainWindow):
    _bg_progress = pyqtSignal(int, int, str)
    _bg_export_done = pyqtSignal(int, int)
    _bg_export_failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Ksenia M3U Editor {APP_VERSION}")
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
        self._playlist_creation_stop = None

        IconProvider.initialize(self.style())
        IconProvider.set_enabled(
            bool(self.core.config.get('enable_icons', True)))

        self._setup_ui()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_statusbar()
        self._bg_progress.connect(self.show_progress)
        self._bg_export_done.connect(self._on_stable_export_done)
        self._bg_export_failed.connect(self._on_stable_export_failed)
        self._load_settings()
        self._apply_config()
        self._update_window_title()

        app = QApplication.instance()
        if app:
            app.paletteChanged.connect(self._on_palette_changed)
        self.core.settings_changed.connect(self._on_settings_changed)
        self.core.blacklist_updated.connect(self._on_blacklist_updated)
        self.core.domain_blacklist_updated.connect(
            self._on_domain_blacklist_updated)
        self.core.playlist_from_sources_ready.connect(
            self._finish_create_playlist_from_sources)
        self.core.playlist_from_sources_failed.connect(
            self._handle_playlist_creation_error)

        self.core.load_epg_from_cache_async()

    def _on_settings_changed(self):
        self._apply_config()

    def _on_blacklist_updated(self):
        for tab in list(self.tabs.values()):
            if not _is_qobject_valid(tab):
                continue
            selected_uids = {ch.uid for ch in tab.selected_channels}
            current_uid = (tab.current_channel.uid
                           if tab.current_channel else None)
            filtered, removed = self.core.apply_blacklist_to_channels(
                tab.all_channels)
            if removed > 0:
                tab.flush_pending_state()
                tab.save_state("Применение чёрного списка каналов")
                tab.all_channels = filtered
                tab.sync_to_core()
                tab.refresh_view()
                tab.flush_pending_state()
                tab.selected_channels = [
                    ch for ch in tab.all_channels
                    if ch.uid in selected_uids]
                if current_uid is not None:
                    tab.current_channel = next(
                        (ch for ch in tab.all_channels
                         if ch.uid == current_uid), None)
            tab.update_info()

    def _on_domain_blacklist_updated(self):
        for tab in list(self.tabs.values()):
            if not _is_qobject_valid(tab):
                continue
            cleaned = tab._clean_blocked_by_domain()
            if cleaned > 0:
                logger.info(
                    f"Очищено {cleaned} ссылок по ЧС домен/IP "
                    f"в табе {tab.tab_id[:8]}")
            tab.update_info()

    def _on_palette_changed(self, *args):
        IconProvider.clear_cache()
        self._rebuild_ui_icons()

    def _std_icon(self, sp: Optional[QStyle.StandardPixmap] = None,
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

        icons_enabled = bool(c.get('enable_icons', True))
        IconProvider.set_enabled(icons_enabled)
        if self._toolbar is not None:
            self._toolbar.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly if icons_enabled
                else Qt.ToolButtonStyle.ToolButtonTextOnly)

        font_size = int(c.get('cell_font_size', 10))
        show_tvg_id = bool(c.get('show_tvg_id', True))
        show_tvg_logo = bool(c.get('show_tvg_logo', True))
        show_catchup = bool(c.get('show_catchup', False))
        for tab in self.tabs.values():
            if not _is_qobject_valid(tab):
                continue
            f = tab.table.font()
            f.setPointSize(font_size)
            tab.table.setFont(f)
            tab.table.setColumnHidden(tab.model.COL_TVG_ID,
                                       not show_tvg_id)
            tab.table.setColumnHidden(tab.model.COL_TVG_LOGO,
                                       not show_tvg_logo)
            tab.table.setColumnHidden(tab.model.COL_CATCHUP,
                                       not show_catchup)
        self.status_bar.setVisible(bool(c.get('show_status_bar', True)))

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tab_widget)

        fl = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Поиск... (префиксы: is:orphan, is:broken, is:working)")
        self.search_edit.textChanged.connect(self._on_search_changed)
        fl.addWidget(self.search_edit, 3)

        self.group_combo = QComboBox()
        self.group_combo.addItem(GROUP_FILTER_ALL)
        self.group_combo.setFixedWidth(150)
        self.group_combo.currentTextChanged.connect(self._on_group_changed)
        fl.addWidget(self.group_combo, 0)

        layout.addLayout(fl)

    def _setup_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("Файл")
        self._add_action(fm, "Новый", self._new_file, "Ctrl+N")
        self._add_action(fm, "Открыть", self._open_file, "Ctrl+O")
        self._add_action(fm, "Создать плейлист из источников...",
                         self._create_playlist_from_sources)
        self.recent_menu = QMenu("Открыть недавние", self)
        fm.addMenu(self.recent_menu)
        fm.addSeparator()
        self._add_action(fm, "Сохранить", self._save_file, "Ctrl+S")
        self._add_action(fm, "Сохранить как", self._save_as,
                         "Ctrl+Shift+S")
        self._add_action(fm, "Сохранить всё", self._save_all)
        fm.addSeparator()
        self._add_action(fm, "Экспорт Stable-плейлиста...",
                         self._export_stable)
        fm.addSeparator()
        self._add_action(fm, "Импорт", self._import_file)
        self._add_action(fm, "Экспорт в CSV/JSON...", self._export_file)
        fm.addSeparator()
        self._add_action(fm, "Выход", self.close, "Alt+F4")

        cm = mb.addMenu("Каналы")
        self._add_action(cm, "Новый канал",
                         lambda: self._with_tab('_new_channel'),
                         "Ctrl+Shift+A")
        self._add_action(cm, "Вырезать",
                         lambda: self._with_tab('_cut_channel'),
                         "Ctrl+X")
        self._add_action(cm, "Копировать",
                         lambda: self._with_tab('_copy_channel'),
                         "Ctrl+C")
        self._add_action(cm, "Вставить",
                         lambda: self._with_tab('_paste_channel'),
                         "Ctrl+V")
        cm.addSeparator()
        self._add_action(cm, "Массовая правка выбранных...",
                         lambda: self._with_tab('_mass_edit_selected'))
        cm.addSeparator()
        self._add_action(cm, "Удалить каналы без ссылок",
                         lambda: self._with_tab(
                             'delete_channels_without_urls'))
        self._add_action(cm, "Удалить каналы без метаданных",
                         lambda: self._with_tab(
                             'delete_channels_without_metadata'))
        cm.addSeparator()
        self._add_action(cm, "Переместить вверх",
                         lambda: self._with_tab('_move_channel_up'))
        self._add_action(cm, "Переместить вниз",
                         lambda: self._with_tab('_move_channel_down'))
        cm.addSeparator()
        self._add_action(cm, "Поиск дубликатов...",
                         self._show_duplicates)
        self._add_action(cm, "Сравнить плейлисты...", self._show_compare)
        cm.addSeparator()
        self._add_action(cm, "Удалить метаданные...",
                         self._remove_metadata)

        lm = mb.addMenu("Ссылки")
        self._add_action(lm, "Проверить все ссылки",
                         lambda: self._with_tab('check_all_urls'))
        self._add_action(lm, "Проверить выбранные ссылки",
                         lambda: self._with_tab('_check_selected_urls'))
        lm.addSeparator()
        self._add_action(lm, "Заменить все ссылки (из кэша)",
                         lambda: self._with_tab('replace_all_links'))
        self._add_action(lm, "Заменить выбранные ссылки",
                         lambda: self._with_tab('replace_selected_links'))
        lm.addSeparator()
        self._add_action(lm, "Удалить все ссылки",
                         lambda: self._with_tab('remove_all_urls'))
        self._add_action(lm, "Удалить выбранные ссылки",
                         lambda: self._with_tab('_remove_selected_urls'))
        lm.addSeparator()
        self._add_action(lm, "Удалить все битые ссылки",
                         lambda: self._with_tab('_remove_all_broken_urls'))
        lm.addSeparator()
        self._add_action(lm, "🚫 Заблокировать домен/IP…",
                         self._block_domain_manual)
        lm.addSeparator()

        info_act = QAction(
            "ℹ️ Проверка ссылок — в менеджере источников", self)
        info_act.setEnabled(False)
        lm.addAction(info_act)

        tm = mb.addMenu("Инструменты")
        self._add_action(
            tm,
            "Источники ссылок (обновить + проверить URL)...",
            self._manage_sources)
        self._add_action(tm, "Редактировать заголовок...",
                         self._edit_header)
        tm.addSeparator()
        self._add_action(tm, "Загрузить EPG", self._load_epg)
        self._add_action(
            tm,
            "Применить метаданные из EPG ко всем каналам",
            self._apply_epg_metadata_all)
        tm.addSeparator()
        self._add_action(tm, "Управление кэшем...", self._manage_cache)
        tm.addSeparator()
        self._add_action(tm, "Менеджер чёрного списка каналов",
                         self._manage_blacklist)
        self._add_action(tm, "Применить чёрный список каналов",
                         self._apply_blacklist)
        tm.addSeparator()
        self._add_action(tm, "Менеджер чёрного списка домен/IP",
                         self._manage_domain_blacklist)
        self._add_action(
            tm,
            "Очистить ссылки по чёрному списку домен/IP",
            self._apply_domain_blacklist)
        tm.addSeparator()
        self._add_action(tm, "User-Agent по доменам",
                         self._manage_ua_rules)
        tm.addSeparator()
        self._add_action(tm, "Показать лог", self._show_log)

        sm = mb.addMenu("Настройки")
        self._add_action(sm, "Настройки замены ссылок",
                         self._manage_replacement_settings)
        self._add_action(sm, "Общие настройки",
                         self._manage_general_settings)
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
        self._add_action(hm, "Справка", self._show_help, "F1")
        self._add_action(hm, "Поддержка проекта", self._show_support)
        hm.addSeparator()
        self._add_action(hm, "О Qt",
                         lambda: QMessageBox.aboutQt(self))

    def _add_action(self, container, text: str, slot: Callable,
                    shortcut: str = "",
                    icon: Optional[QIcon] = None,
                    tooltip: str = "") -> QAction:
        a = make_action(self, text, slot, shortcut, icon, tooltip)
        if container is not None:
            container.addAction(a)
        return a

    def _setup_toolbar(self):
        if self._toolbar is None:
            self._toolbar = QToolBar("Main", self)
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
            self._std_icon(SP.SP_DialogOpenButton, "document-open"),
            "Открыть файл")
        add("Сохранить", self._save_file,
            self._std_icon(SP.SP_DialogSaveButton, "document-save"),
            "Сохранить (Ctrl+S)")
        self._toolbar.addSeparator()

        self.toolbar_undo_action = add(
            "Отменить", lambda: self._with_tab('undo'),
            self._std_icon(SP.SP_ArrowBack, "edit-undo"),
            "Отменить (Ctrl+Z)")
        self.toolbar_redo_action = add(
            "Повторить", lambda: self._with_tab('redo'),
            self._std_icon(SP.SP_ArrowForward, "edit-redo"),
            "Повторить (Ctrl+Y)")
        self.toolbar_undo_action.setEnabled(False)
        self.toolbar_redo_action.setEnabled(False)
        self._toolbar.addSeparator()

        add("Переместить вверх",
            lambda: self._with_tab('_move_channel_up'),
            self._std_icon(SP.SP_ArrowUp, "go-up"),
            "Переместить вверх")
        add("Переместить вниз",
            lambda: self._with_tab('_move_channel_down'),
            self._std_icon(SP.SP_ArrowDown, "go-down"),
            "Переместить вниз")
        self._toolbar.addSeparator()

        add("Копировать",
            lambda: self._with_tab('_copy_channel'),
            self._std_icon(SP.SP_FileDialogDetailedView, "edit-copy"),
            "Копировать (Ctrl+C)")
        add("Вырезать",
            lambda: self._with_tab('_cut_channel'),
            self._std_icon(SP.SP_FileDialogListView, "edit-cut"),
            "Вырезать (Ctrl+X)")
        add("Вставить",
            lambda: self._with_tab('_paste_channel'),
            self._std_icon(SP.SP_FileDialogContentsView, "edit-paste"),
            "Вставить (Ctrl+V)")
        self._toolbar.addSeparator()

        add("Плеер", self._play_selected,
            self._std_icon(SP.SP_MediaPlay, "media-playback-start"),
            "Открыть в плеере")
        self._toolbar.addSeparator()

        add("Увеличить", lambda: self._change_font(+1),
            self._std_icon(SP.SP_TitleBarMaxButton, "zoom-in"),
            "Увеличить шрифт")
        add("Уменьшить", lambda: self._change_font(-1),
            self._std_icon(SP.SP_TitleBarMinButton, "zoom-out"),
            "Уменьшить шрифт")

        if self.current_tab:
            self._on_undo_state(
                self.current_tab.undo_manager.can_undo(),
                self.current_tab.undo_manager.can_redo())

    def _setup_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.action_label = QLabel("Готов")
        self.action_label.setStyleSheet(
            "QLabel {"
            "  background-color: #D6EAF8;"
            "  color: #1B4F72;"
            "  padding: 3px 12px;"
            "  border-radius: 4px;"
            "  font-weight: bold;"
            "}")
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

    def show_progress(self, value: int, total: int, text: str = ""):
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(value)
        self.progress_bar.setVisible(True)
        if text:
            self.set_action(text)

    def hide_progress(self):
        self.progress_bar.setVisible(False)
        self.set_action("Готов")

    def _load_settings(self):
        s = QSettings("Ksenia", "M3UEditor")
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
        s = QSettings("Ksenia", "M3UEditor")
        s.setValue("window_geometry", self.saveGeometry())
        s.setValue("window_state", self.saveState())
        s.setValue("recent_files", self.recent_files)
        self.core.config.save()

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
        self.current_tab = tab
        tab.set_search_text(self.search_edit.text())
        tab.set_group_filter(self.group_combo.currentText())
        self._apply_config()
        self._update_window_title()
        self._update_groups()
        return tab

    def _new_file(self):
        self.set_action("Новый плейлист")
        self._create_tab()

    def _open_file(self):
        fp = open_file_dialog(self, "Открыть", M3U_FILTER)
        if fp:
            self._open_file_in_tab(fp)

    def _create_playlist_from_sources(self):
        dialog = PlaylistFromSourcesDialog(self.core, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        (sources, minimize, preserve, apply_ua, apply_bl,
         apply_domain_bl, apply_epg) = dialog.get_result()
        if not sources:
            return

        self.set_action("⏳ Загрузка из источников...")
        sources_to_load = sorted(sources, key=lambda s: s.priority)
        _stop = _StopToken()
        self._playlist_creation_stop = _stop
        win_ref = weakref.ref(self)

        def load_worker():
            try:
                logger.info(f"Создание плейлиста: "
                            f"{len(sources_to_load)} источников")
                all_channels: List[ChannelData] = []
                for src in sources_to_load:
                    if _stop.is_set():
                        return
                    logger.info(f"  Загрузка: {src.name} ({src.path})")
                    chs = self.core.link_source_manager.load_links_from_source(
                        src, use_cache=False, config=self.core.config,
                        update_meta=False, stop_token=_stop)
                    logger.info(f"    Получено: {len(chs)} каналов")
                    for ch in chs:
                        ch.link.link_source = src.name
                    all_channels.extend(chs)

                if apply_bl and self.core.blacklist_manager.get_all():
                    all_channels, _ = self.core.apply_blacklist_to_channels(
                        all_channels)

                if (apply_domain_bl
                        and self.core.domain_blacklist_manager.get_all()):
                    all_channels, _ = \
                        self.core.apply_domain_blacklist_clean(all_channels)

                if _stop.is_set():
                    return
                if (apply_ua
                        and self.core.domain_user_agent_manager.get_all_rules()):
                    self.core.apply_domain_user_agent(all_channels)

                if minimize:
                    seen_names = set()
                    final_channels = []
                    for ch in all_channels:
                        norm_name = ch.normalized_name()
                        if norm_name not in seen_names:
                            seen_names.add(norm_name)
                            final_channels.append(ch)
                    all_channels = final_channels

                if preserve:
                    priority_map = {s.name: s.priority
                                    for s in sources_to_load}
                    all_channels.sort(key=lambda ch: (
                        priority_map.get(ch.link.link_source, 999),
                        ch.original_index
                        if ch.original_index >= 0 else float('inf')))
                else:
                    all_channels.sort(
                        key=lambda ch: ch.original_index
                        if ch.original_index >= 0 else float('inf'))

                if _stop.is_set():
                    return
                logger.info(f"Всего подготовлено: "
                            f"{len(all_channels)} каналов")
                w = win_ref()
                if w is None or not _is_qobject_valid(w):
                    return
                self.core.playlist_from_sources_ready.emit(all_channels)
            except Exception as e:
                logger.exception(
                    "Ошибка создания плейлиста из источников")
                w = win_ref()
                if w is None or not _is_qobject_valid(w):
                    return
                self.core.playlist_from_sources_failed.emit(str(e))

        threading.Thread(target=load_worker, daemon=True).start()

    def _finish_create_playlist_from_sources(
            self, channels: List[ChannelData]):
        if not _is_qobject_valid(self):
            return
        logger.info(
            f"_finish_create_playlist_from_sources: "
            f"{len(channels)} каналов")
        self.hide_progress()
        if not channels:
            info_box(self, "Источники не содержат каналов "
                           "(или все отфильтрованы ЧС).")
            self.set_action("Готов")
            return
        tab = PlaylistTab(parent_window=self)
        tab.all_channels = channels
        tab.undo_manager.reset(tab.all_channels)
        tab.modified = True

        idx = self.tab_widget.addTab(tab, "Из источников")
        self.tabs[tab] = tab
        self.tab_widget.setCurrentIndex(idx)
        tab.undo_state_changed.connect(self._on_undo_state)
        tab.info_changed.connect(self._on_info)
        self.current_tab = tab
        tab.sync_to_core()
        tab.refresh_view()
        tab.update_modified_status()
        self._apply_config()
        self._update_window_title()
        self._update_groups()
        self.set_action(f"✓ Создан плейлист: {len(channels)} каналов")

    def _handle_playlist_creation_error(self, error_message: str):
        if not _is_qobject_valid(self):
            return
        self.hide_progress()
        error_box(self, f"Не удалось создать плейлист:\n"
                        f"{error_message}")
        self.set_action("✗ Ошибка создания плейлиста")

    def _open_file_in_tab(self, fp: str):
        logger.info(f"Открытие файла: {fp}")
        for tab in self.tabs.values():
            if tab.filepath == fp:
                self.tab_widget.setCurrentIndex(
                    self.tab_widget.indexOf(tab))
                return
        try:
            self.set_action(f"⏳ Открытие: {os.path.basename(fp)}")
            self._create_tab(filepath=fp)
            self._add_recent(fp)
            self.set_action(f"✓ Открыт: {os.path.basename(fp)}")
        except Exception as e:
            logger.exception("Ошибка открытия")
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
        fp = save_file_dialog(self, "Сохранить как", default,
                              M3U_FILTER, ".m3u")
        if not fp:
            return
        self.current_tab.flush_sync()
        if self.current_tab.save_to_file(fp):
            idx = self.tab_widget.indexOf(self.current_tab)
            if idx >= 0:
                self.tab_widget.setTabText(idx, os.path.basename(fp))
            self.set_action("✓ Файл сохранён")
            self._update_window_title()
            self._add_recent(fp)

    def _save_all(self):
        for tab in self.tabs.values():
            if not tab.modified:
                continue
            if not tab.filepath:
                default = f"playlist_{tab.tab_id[:6]}.m3u"
                fp = save_file_dialog(
                    self,
                    f"Сохранить "
                    f"'{tab.filepath or 'Безымянный'}'",
                    default, M3U_FILTER, ".m3u")
                if not fp:
                    continue
                idx = self.tab_widget.indexOf(tab)
                if idx >= 0:
                    self.tab_widget.setTabText(idx,
                                                os.path.basename(fp))
                self._add_recent(fp)
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

        def progress(i, total, text):
            w2 = win_ref()
            if w2 is not None and _is_qobject_valid(w2):
                try:
                    w2._bg_progress.emit(i, total, text)
                except Exception:
                    pass

        import threading as _th

        def _bg():
            try:
                t = tab_ref()
                if t is None:
                    return
                written, skipped = t.export_stable(
                    fp, progress_cb=progress)
                w2 = win_ref()
                if w2 is not None and _is_qobject_valid(w2):
                    try:
                        w2._bg_export_done.emit(written, skipped)
                    except Exception:
                        pass
            except Exception as e:
                w2 = win_ref()
                if w2 is not None and _is_qobject_valid(w2):
                    try:
                        w2._bg_export_failed.emit(str(e))
                    except Exception:
                        pass

        _th.Thread(target=_bg, daemon=True).start()

    def _import_file(self):
        fp = open_file_dialog(self, "Импорт", ALL_FILTER)
        if not fp:
            return
        try:
            with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            if not self.current_tab:
                self._new_file()
            tab = self.current_tab
            if not tab.header_manager.has_extm3u:
                tab.header_manager.parse_header(content)
            new_chs = M3UParser.parse(content, os.path.basename(fp))
            tab.all_channels.extend(new_chs)
            tab.save_state("Импорт файла")
            tab.sync_to_core()
            with tab._suppress_save():
                tab.model.set_channels(tab.all_channels)
            self._update_groups()
            self.set_action(f"✓ Импортировано: {len(new_chs)}")
        except Exception as e:
            error_box(self, str(e))

    def _export_file(self):
        if not self.current_tab:
            return
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
            tab.undo_state_changed.disconnect(self._on_undo_state)
        with suppress(TypeError, RuntimeError):
            tab.info_changed.disconnect(self._on_info)
        tab.disconnect_signals()
        self.core.unregister_tab(tab.tab_id)
        del self.tabs[w]
        self.tab_widget.removeTab(index)
        tab.deleteLater()
        if self.tab_widget.count() == 0:
            self.current_tab = None
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
                grp = self.group_combo.currentText() or GROUP_FILTER_ALL
                self.current_tab.set_group_filter(grp)
                self._update_window_title()
                self._update_groups()
                self.current_tab.update_info()
                self._on_undo_state(
                    self.current_tab.undo_manager.can_undo(),
                    self.current_tab.undo_manager.can_redo())
                return
        self.current_tab = None
        self._update_window_title()
        self._update_groups()
        self._on_info("Готов")
        self._on_undo_state(False, False)
        self.set_action("Готов")

    def _on_search_changed(self, text: str):
        if self.current_tab:
            self.current_tab.set_search_text(text)

    def _on_group_changed(self, group: str):
        if self.current_tab:
            self.current_tab.set_group_filter(group)

    def _update_groups(self):
        cur_group = self.group_combo.currentText()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem(GROUP_FILTER_ALL)
        if self.current_tab:
            for g in self.core.get_all_groups(self.current_tab.tab_id):
                self.group_combo.addItem(g)
        idx = self.group_combo.findText(cur_group)
        if idx < 0:
            idx = 0
        self.group_combo.setCurrentIndex(idx)
        self.group_combo.blockSignals(False)
        if self.current_tab:
            self.current_tab.set_group_filter(
                self.group_combo.currentText())

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
        if tab.current_channel and tab.current_channel.has_valid_url:
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
        info_box(self, "Нет каналов с URL в текущем фильтре", "Плеер")

    def _edit_header(self):
        if self.current_tab:
            self.current_tab.edit_playlist_header()

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
            self.current_tab.remove_metadata(opts, scope=dlg.get_scope())

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
        raw, ok = QInputDialog.getText(
            self, "Заблокировать домен/IP",
            "Домен или IP (можно вставить URL):")
        if not ok or not raw.strip():
            return
        value = URLUtils.normalize_host(raw)
        if not value:
            warn_box(self, "Не удалось разобрать домен/IP")
            return

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
            info_box(self, f"Правило «{v}» добавлено.\n"
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
        dlg = self._track_dialog(BlacklistDialog(self.core, self))
        dlg.exec()

    def _manage_domain_blacklist(self):
        dlg = self._track_dialog(DomainBlacklistDialog(self.core, self))
        dlg.exec()

    def _manage_sources(self):
        dlg = self._track_dialog(LinkSourceManagerDialog(
            self.core.link_source_manager, self.core.config, self))
        dlg.sources_updated.connect(self.core.sources_updated.emit)
        dlg.exec()

    def _manage_replacement_settings(self):
        dlg = self._track_dialog(LinkReplacementSettingsDialog(
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
            info_box(self, f"Файл лога ещё не создан:\n{log_path}",
                     "Лог")
            return
        try:
            open_in_os(log_path)
        except Exception as e:
            error_box(self, str(e))

    def _show_help(self):
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
            size = int(self.core.config.get('cell_font_size', 10))
        if delta == 0:
            new_size = int(self.core.config.get('cell_font_size', 10))
        else:
            new_size = max(6, min(24, size + delta))
        if new_size == size and delta != 0:
            return
        f.setPointSize(new_size)
        self.current_tab.table.setFont(f)
        self.core.config.set('cell_font_size', new_size)
        if int(self.core.config.get('cell_font_size', 0)) != new_size:
            self.core.config.save()
        self._apply_config()

    def _disconnect_app_signals(self):
        app = QApplication.instance()
        if app is not None:
            with suppress(TypeError, RuntimeError):
                app.paletteChanged.disconnect(self._on_palette_changed)
        with suppress(TypeError, RuntimeError):
            self.core.settings_changed.disconnect(
                self._on_settings_changed)
        with suppress(TypeError, RuntimeError):
            self.core.blacklist_updated.disconnect(
                self._on_blacklist_updated)
        with suppress(TypeError, RuntimeError):
            self.core.domain_blacklist_updated.disconnect(
                self._on_domain_blacklist_updated)
        with suppress(TypeError, RuntimeError):
            self.core.playlist_from_sources_ready.disconnect(
                self._finish_create_playlist_from_sources)
        with suppress(TypeError, RuntimeError):
            self.core.playlist_from_sources_failed.disconnect(
                self._handle_playlist_creation_error)

    def closeEvent(self, event):
        _stop = getattr(self, '_playlist_creation_stop', None)
        if _stop is not None:
            with suppress(Exception):
                _stop.set()
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

        with suppress(Exception):
            _app = QApplication.instance()
            if _app is not None:
                _app.quit()


def parse_cli_args(argv: Optional[List[str]] = None
                    ) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ksenia",
        description=f"Ksenia M3U Editor {APP_VERSION} — CLI/headless")
    p.add_argument("--headless", action="store_true",
                   help="Запустить без GUI")
    p.add_argument("--sources-file", default=None,
                   help="JSON-файл источников (экспорт из GUI)")
    p.add_argument("--export", default=None,
                   help="Куда писать M3U")
    p.add_argument("--export-stable", action="store_true",
                   help="Stable-экспорт с живой перепроверкой")
    p.add_argument("--check-workers", type=int,
                   default=STABLE_CHECK_MAX_WORKERS,
                   help="Число воркеров проверки")
    p.add_argument("--timeout", type=int,
                   default=STABLE_CHECK_TIMEOUT_SEC,
                   help="Таймаут проверки, сек")
    p.add_argument("--apply-blacklist", action="store_true",
                   default=True,
                   help="Применять ЧС каналов (удаляет)")
    p.add_argument("--no-domain-blacklist", action="store_true",
                   help="Не очищать ссылки по ЧС домен/IP")
    p.add_argument("--version", action="version",
                   version=f"Ksenia {APP_VERSION}")
    return p.parse_args(argv)


def run_cli(args: argparse.Namespace) -> int:
    """CLI-режим. Осознанное исключение для headless:
    собственная логика проверки URL."""
    _qcore = QCoreApplication.instance() or QCoreApplication(sys.argv)
    core = ApplicationCore.instance()

    if not args.sources_file:
        print("Ошибка: --sources-file обязателен в headless-режиме",
              file=sys.stderr)
        return 2
    if not args.export:
        print("Ошибка: --export обязателен в headless-режиме",
              file=sys.stderr)
        return 2
    if not os.path.exists(args.sources_file):
        print(f"Ошибка: файл не найден: {args.sources_file}",
              file=sys.stderr)
        return 2

    try:
        with open(args.sources_file, 'r', encoding='utf-8') as f:
            sources_data = json.load(f)
    except Exception as e:
        print(f"Ошибка чтения источников: {e}", file=sys.stderr)
        return 2
    if not isinstance(sources_data, list):
        print("Ошибка: ожидался JSON-массив источников",
              file=sys.stderr)
        return 2

    sources: List[LinkSource] = []
    skipped_sources = 0
    for d in sources_data:
        if isinstance(d, dict):
            src = LinkSource.from_dict(d)
            if src.name and src.enabled:
                sources.append(src)
            else:
                skipped_sources += 1
    if skipped_sources:
        print(f"[i] Пропущено источников (пустое имя или "
              f"enabled=false): {skipped_sources}", file=sys.stderr)
    if not sources:
        print("Ошибка: нет включённых источников", file=sys.stderr)
        return 2

    sources.sort(key=lambda s: s.priority)
    all_channels: List[ChannelData] = []
    for src in sources:
        print(f"[+] Загрузка: {src.name} ({src.path})")
        chs = core.link_source_manager.load_links_from_source(
            src, use_cache=False, config=core.config, update_meta=False)
        print(f"    Получено: {len(chs)} каналов")
        for ch in chs:
            ch.link.link_source = src.name
        all_channels.extend(chs)

    if args.apply_blacklist and core.blacklist_manager.get_all():
        before = len(all_channels)
        all_channels, removed = core.apply_blacklist_to_channels(
            all_channels)
        if removed:
            print(f"[+] ЧС каналов: удалено {removed} (было {before})")

    if (not args.no_domain_blacklist
            and core.domain_blacklist_manager.get_all()):
        before = len(all_channels)
        all_channels, cleaned = core.apply_domain_blacklist_clean(
            all_channels)
        if cleaned:
            print(f"[+] ЧС домен/IP: очищено ссылок {cleaned} "
                  f"(каналов осталось {len(all_channels)})")

    if core.domain_user_agent_manager.get_all_rules():
        modified = core.apply_domain_user_agent(all_channels)
        if modified:
            print(f"[+] UA-правила применены к {modified} каналам")

    if args.export_stable:
        settings = core.get_replacement_settings()
        ssm = core.stable_state_manager
        written = 0
        skipped = 0
        total = len(all_channels)
        print(f"[+] Stable-экспорт: {total} каналов, "
              f"timeout={args.timeout}s, workers={args.check_workers}")

        def stable_key(ch: ChannelData) -> str:
            if ch.meta.tvg_id:
                return f"tvg:{ch.meta.tvg_id.strip().lower()}"
            norm = ChannelNameNormalizer.normalize(ch.meta.name or "")
            return f"name:{norm}"

        def find_url(ch: ChannelData) -> Optional[str]:
            """CLI-режим: собственная логика проверки (исключение)."""
            cands: List[str] = []
            ref = ssm.get(stable_key(ch))
            if ref:
                cands.append(ref)
            if ch.link.url and ch.link.url not in cands:
                cands.append(ch.link.url)
            for u in ch.link.alternative_urls:
                if u and u not in cands:
                    cands.append(u)
            for u in cands:
                if not u or not u.strip():
                    continue
                if settings.is_blacklisted(u) or \
                        settings.is_filtered_domain(u):
                    continue
                ok, rt, _, _ = URLUtils.check_url(
                    u, args.timeout, verify_ssl=False,
                    max_retries=0, retry_delay=0.0)
                if ok is True and (
                        rt is None
                        or rt * 1000 <= STABLE_LATENCY_THRESHOLD_MS):
                    return u
            return None

        with open(args.export, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n\n")
            for i, ch in enumerate(all_channels):
                if (i + 1) % 25 == 0 or i == total - 1:
                    print(f"    Проверка: {i+1}/{total}")
                url = find_url(ch)
                if not url:
                    skipped += 1
                    continue
                key = stable_key(ch)
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
        print(f"[✓] Stable: записано {written}, пропущено {skipped}")
    else:
        with open(args.export, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n\n")
            for ch in all_channels:
                f.write(ch.link.extinf + '\n')
                for line in ch.link.extvlcopt_lines:
                    f.write(line + '\n')
                f.write((ch.link.url or '') + '\n')
        print(f"[✓] Экспорт: {len(all_channels)} каналов → "
              f"{args.export}")

    return 0


def _install_signal_handlers(app: QApplication):
    def handler(signum, frame):
        logger.info(f"Получен сигнал {signum}, завершение...")
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(ValueError, OSError):
            signal.signal(sig, handler)


def main():
    import argparse as _argparse
    _parser = _argparse.ArgumentParser(add_help=False)
    _parser.add_argument('--headless', action='store_true')
    _known, _ = _parser.parse_known_args()
    if _known.headless:
        args = parse_cli_args()
        try:
            rc = run_cli(args)
        except Exception as e:
            logger.exception("CLI error")
            print(f"Ошибка: {e}", file=sys.stderr)
            rc = 1
        sys.exit(rc)

    app = QApplication([sys.argv[0]])
    app.setApplicationName(f"Ksenia M3U Editor {APP_VERSION}")
    app.setOrganizationName("Ksenia")
    app.setQuitOnLastWindowClosed(True)
    _install_signal_handlers(app)
    window = MainWindow()
    window.show()
    rc = app.exec()
    sys.exit(rc)