# -*- coding: utf-8 -*-
"""PlayerController — управление встроенным mpv-плеером
для PlaylistTab."""
from __future__ import annotations

import weakref
from contextlib import suppress
from typing import List, Optional

from PyQt6.QtCore import QObject, QItemSelectionModel
from PyQt6.QtWidgets import QAbstractItemView

from dialogs_base import _is_qobject_valid
from logging_setup import logger
from models import ChannelData
from paths import error_box, warn_box
from player import (EmbeddedPlayerDialog, get_mpv_error,
                    is_mpv_available)


class PlayerController(QObject):
    def __init__(self, tab: 'PlaylistTab'):
        super().__init__(tab)
        self._tab_ref = weakref.ref(tab)
        self._dialog: Optional[EmbeddedPlayerDialog] = None
        self._syncing_selection = False

    def _tab(self):
        t = self._tab_ref()
        if t is None or not _is_qobject_valid(t):
            return None
        return t

    def is_dialog_open(self) -> bool:
        return (self._dialog is not None
                and _is_qobject_valid(self._dialog))

    @staticmethod
    def _get_visible_channels(tab) -> List[ChannelData]:
        model = getattr(tab, 'model', None)
        if model is None:
            return list(getattr(tab, 'all_channels', []) or [])
        fn = getattr(model, 'channels_filtered', None)
        if callable(fn):
            try:
                result = list(fn())
                if result:
                    return result
            except Exception:
                logger.exception("channels_filtered")
        return list(getattr(tab, 'all_channels', []) or [])

    def play_row(self, row: int):
        tab = self._tab()
        if tab is None:
            return
        ch = tab.channel_for_row(row)
        if not ch:
            return
        if not ch.has_valid_url:
            warn_box(tab, "Нет URL", "Плеер")
            return
        if not is_mpv_available():
            error_box(
                tab,
                "Встроенный плеер недоступен.\n\n"
                + get_mpv_error(),
                "Плеер")
            return
        self.close_dialog()
        playlist = self._get_visible_channels(tab)
        dlg = EmbeddedPlayerDialog(ch, tab, playlist=playlist)
        dlg.channel_changed.connect(
            self._on_channel_changed_from_player)
        dlg.channel_status_changed.connect(
            self._on_channel_status_changed)
        dlg.channel_modified.connect(self._on_channel_modified)
        dlg.destroyed.connect(self._on_destroyed)
        self._dialog = dlg
        dlg.show()
        w = tab.parent_window
        if w is not None and _is_qobject_valid(w):
            w.set_action(
                f"▶ Воспроизведение: {ch.meta.name}")

    def _on_channel_status_changed(self, uid: int, ok: bool):
        tab = self._tab()
        if tab is None:
            return
        try:
            tab.mark_channel_status(uid, ok)
        except Exception:
            logger.exception("mark_channel_status")

    def _on_channel_modified(self, uid: int):
        tab = self._tab()
        if tab is None:
            return
        try:
            tab.modified = True
            tab.update_modified_status()
        except Exception:
            logger.exception("_on_channel_modified")

    def _on_channel_changed_from_player(self, uid: int):
        tab = self._tab()
        if tab is None:
            return
        row = self._row_of_uid_in_model(tab, uid)
        if row < 0:
            with suppress(Exception):
                tab.table.clearSelection()
            return
        self._syncing_selection = True
        try:
            idx = tab.model.index(row, 0)
            if not idx.isValid():
                return
            tab.table.setCurrentIndex(idx)
            sel = tab.table.selectionModel()
            if sel is not None:
                sel.select(
                    idx,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QItemSelectionModel.SelectionFlag.Rows)
            tab.table.scrollTo(
                idx,
                QAbstractItemView.ScrollHint.PositionAtCenter)
            ch = tab.model.channel_at(row)
            if ch is not None:
                tab.current_channel = ch
                tab.selected_channels = [ch]
        except Exception:
            logger.exception("_on_channel_changed_from_player")
        finally:
            self._syncing_selection = False

    @staticmethod
    def _row_of_uid_in_model(tab, uid: int) -> int:
        model = getattr(tab, 'model', None)
        if model is None:
            return -1
        try:
            fn = getattr(model, 'row_of_uid', None)
            if callable(fn):
                return int(fn(uid))
        except Exception:
            logger.exception("_row_of_uid_in_model")
        return -1

    def on_table_selection_changed(self):
        if self._syncing_selection:
            return
        tab = self._tab()
        if tab is None or not self.is_dialog_open():
            return
        ch = tab.current_channel
        if ch is None or not ch.has_valid_url:
            return
        dlg = self._dialog
        if dlg is None or not _is_qobject_valid(dlg):
            return
        if dlg.channel is not None and dlg.channel.uid == ch.uid:
            return
        idx = next(
            (i for i, c in enumerate(dlg.playlist)
             if c.uid == ch.uid), -1)
        if idx < 0:
            return
        self._syncing_selection = True
        try:
            dlg._play_at(idx)
        except Exception:
            logger.exception("_play_at")
        finally:
            self._syncing_selection = False

    def close_dialog(self):
        dlg = self._dialog
        self._dialog = None
        if dlg is None:
            return
        with suppress(Exception):
            if not _is_qobject_valid(dlg):
                return
            if getattr(dlg, 'player', None) is not None:
                with suppress(Exception):
                    dlg.player.release()
            dlg.close()

    def _on_destroyed(self, *_args):
        self._dialog = None