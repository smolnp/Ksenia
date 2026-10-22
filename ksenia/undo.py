# -*- coding: utf-8 -*-
"""UndoRedoManager, SimpleDuplicateFinder."""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from constants import UNDO_MAX_STATES
from logging_setup import logger
from models import ChannelData
from name_normalizer import (NormMode, normalize_with_settings)


def _norm(ch: ChannelData, settings: Any) -> str:
    return normalize_with_settings(
        ch.meta.name or "", settings, NormMode.FULL)


class UndoRedoManager:
    def __init__(self, max_states: int = UNDO_MAX_STATES):
        self._undo_stack: List[Dict[str, Any]] = []
        self._redo_stack: List[Dict[str, Any]] = []
        self._max_states = max_states
        self._last_hash: Dict[int, str] = {}
        self._last_data: Dict[int, Dict[str, Any]] = {}
        self._last_order: List[int] = []
        self._initialized: bool = False
        self._lock = threading.RLock()
        self._apply_callback: Optional[
            Callable[[Dict[str, Any], bool], bool]] = None

    def bind_apply_callback(
            self,
            cb: Callable[[Dict[str, Any], bool], bool]) -> None:
        self._apply_callback = cb

    @staticmethod
    def _hash_channel(ch: ChannelData) -> str:
        d = ch.to_dict()
        return hashlib.blake2b(
            json.dumps(d, sort_keys=True, default=str)
                .encode('utf-8'),
            digest_size=8).hexdigest()

    def _current_snapshot(self, channels: List[ChannelData]
                          ) -> Tuple[Dict[int, str], List[int]]:
        snap: Dict[int, str] = {}
        order: List[int] = []
        for ch in channels:
            snap[ch.uid] = self._hash_channel(ch)
            order.append(ch.uid)
        return snap, order

    def save_state(self, channels: List[ChannelData],
                   description: str = ""):
        with self._lock:
            new_snap, new_order = self._current_snapshot(channels)

            if not self._initialized:
                self._last_hash = new_snap
                self._last_data = {ch.uid: ch.to_dict()
                                    for ch in channels}
                self._last_order = new_order
                self._initialized = True
                return

            old_snap = self._last_hash
            old_order = list(self._last_order)

            added_uids = [u for u in new_snap if u not in old_snap]
            removed_uids = [u for u in old_snap if u not in new_snap]
            changed_uids = [u for u in new_snap
                            if u in old_snap
                            and old_snap[u] != new_snap[u]]

            channels_by_uid = {ch.uid: ch for ch in channels}
            old_data_map = self._last_data

            added_data: List[Tuple[int, Dict[str, Any]]] = []
            changed_data: List[Tuple[int, Dict[str, Any],
                                      Dict[str, Any]]] = []
            for uid in added_uids:
                ch = channels_by_uid.get(uid)
                if ch is not None:
                    added_data.append((uid, ch.to_dict()))
            for uid in changed_uids:
                ch = channels_by_uid.get(uid)
                old_d = old_data_map.get(uid)
                if ch is not None and old_d is not None:
                    changed_data.append(
                        (uid, old_d, ch.to_dict()))

            removed_data: List[Tuple[int, Dict[str, Any]]] = [
                (uid, old_data_map[uid]) for uid in removed_uids
                if uid in old_data_map
            ]

            order_changed = (old_order != new_order)
            if not (added_data or removed_data or changed_data
                    or order_changed):
                return

            diff: Dict[str, Any] = {
                'description': description,
                'timestamp': datetime.now().isoformat(),
                'added': added_data,
                'removed': removed_data,
                'changed': changed_data,
                'old_order': old_order,
                'new_order': new_order,
            }

            self._undo_stack.append(diff)
            if len(self._undo_stack) > self._max_states:
                self._undo_stack.pop(0)
            self._redo_stack.clear()

            new_last_data = dict(old_data_map)
            for uid in removed_uids:
                new_last_data.pop(uid, None)
            for uid in added_uids:
                ch = channels_by_uid.get(uid)
                if ch is not None:
                    new_last_data[uid] = ch.to_dict()
            for uid in changed_uids:
                ch = channels_by_uid.get(uid)
                if ch is not None:
                    new_last_data[uid] = ch.to_dict()
            self._last_data = new_last_data
            self._last_hash = new_snap
            self._last_order = new_order

    def _apply(self, diff: Dict[str, Any],
               reverse: bool) -> bool:
        if self._apply_callback is None:
            return False
        try:
            return bool(self._apply_callback(diff, reverse))
        except Exception:
            logger.exception("apply_callback failed")
            return False

    def undo(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._undo_stack:
                return None
            diff = self._undo_stack[-1]
            if not self._apply(diff, reverse=True):
                return None
            self._undo_stack.pop()
            self._redo_stack.append(diff)
            self._restore_internal_state(diff, reverse=True)
            return diff

    def redo(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._redo_stack:
                return None
            diff = self._redo_stack[-1]
            if not self._apply(diff, reverse=False):
                return None
            self._redo_stack.pop()
            self._undo_stack.append(diff)
            self._restore_internal_state(diff, reverse=False)
            return diff

    def _restore_internal_state(self, diff: Dict[str, Any],
                                 reverse: bool):
        target_order = diff.get('old_order' if reverse else 'new_order')
        new_data: Dict[int, Dict[str, Any]] = dict(self._last_data)

        if reverse:
            for entry in diff.get('changed', []):
                if len(entry) >= 3:
                    uid, old_d, _new = entry[0], entry[1], entry[2]
                    new_data[uid] = old_d
            for uid, _d in diff.get('added', []):
                new_data.pop(uid, None)
            for uid, d in diff.get('removed', []):
                new_data[uid] = d
        else:
            for entry in diff.get('changed', []):
                if len(entry) >= 3:
                    uid, _old, new_d = entry[0], entry[1], entry[2]
                    new_data[uid] = new_d
            for uid, d in diff.get('added', []):
                new_data[uid] = d
            for uid, _d in diff.get('removed', []):
                new_data.pop(uid, None)

        self._last_data = new_data
        if target_order is not None:
            self._last_order = list(target_order)
        new_hash: Dict[int, str] = {}
        for uid, d in new_data.items():
            new_hash[uid] = hashlib.blake2b(
                json.dumps(d, sort_keys=True,
                           default=str).encode('utf-8'),
                digest_size=8).hexdigest()
        self._last_hash = new_hash

    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._undo_stack)

    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo_stack)

    def reset(self, channels: List[ChannelData]):
        with self._lock:
            snap, order = self._current_snapshot(channels)
            self._last_hash = snap
            self._last_data = {ch.uid: ch.to_dict()
                                for ch in channels}
            self._last_order = order
            self._initialized = True
            self._undo_stack.clear()
            self._redo_stack.clear()


class SimpleDuplicateFinder:
    @staticmethod
    def find_duplicates_by_url(channels: List[ChannelData]
                               ) -> List[List[ChannelData]]:
        url_map: Dict[str, List[ChannelData]] = defaultdict(list)
        for ch in channels:
            if ch.has_valid_url:
                url_map[ch.link.url.strip()].append(ch)
        return [g for g in url_map.values() if len(g) > 1]

    @staticmethod
    def find_duplicates_by_name(channels: List[ChannelData],
                                 use_tvg_id: bool = False,
                                 settings: Any = None
                                 ) -> List[List[ChannelData]]:
        name_map: Dict[str, List[ChannelData]] = defaultdict(list)
        for ch in channels:
            key = _norm(ch, settings)
            if not key:
                continue
            if use_tvg_id and ch.meta.tvg_id:
                key = f"{key}|{ch.meta.tvg_id.lower()}"
            name_map[key].append(ch)
        return [g for g in name_map.values() if len(g) > 1]

    @staticmethod
    def get_duplicate_report(channels: List[ChannelData],
                              use_tvg_id: bool = False,
                              settings: Any = None
                              ) -> Dict[str, Any]:
        by_url = (SimpleDuplicateFinder
                  .find_duplicates_by_url(channels))
        by_name = (SimpleDuplicateFinder
                   .find_duplicates_by_name(
                       channels, use_tvg_id=use_tvg_id,
                       settings=settings))
        return {
            'by_url': by_url,
            'by_name': by_name,
            'total_url_duplicates': sum(len(g) - 1
                                          for g in by_url),
            'total_name_duplicates': sum(len(g) - 1
                                           for g in by_name),
        }

    @staticmethod
    def remove_duplicates_by_url(channels: List[ChannelData]
                                  ) -> Tuple[List[ChannelData], int]:
        seen: Set[str] = set()
        result: List[ChannelData] = []
        removed = 0
        for ch in channels:
            if ch.has_valid_url:
                if ch.link.url.strip() in seen:
                    removed += 1
                    continue
                seen.add(ch.link.url.strip())
            result.append(ch)
        return result, removed

    @staticmethod
    def remove_duplicates_by_name(channels: List[ChannelData],
                                   use_tvg_id: bool = False,
                                   settings: Any = None
                                   ) -> Tuple[List[ChannelData], int]:
        seen: Set[str] = set()
        result: List[ChannelData] = []
        removed = 0
        for ch in channels:
            key = _norm(ch, settings)
            if not key:
                result.append(ch)
                continue
            if use_tvg_id and ch.meta.tvg_id:
                key = f"{key}|{ch.meta.tvg_id.lower()}"
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            result.append(ch)
        return result, removed

    @staticmethod
    def find_duplicate_uids_with_keys(
            channels: List[ChannelData],
            use_tvg_id: bool = False,
            settings: Any = None
    ) -> Tuple[Set[int], Set[int],
                Dict[int, str], Dict[int, str]]:
        by_name: Dict[str, List[int]] = defaultdict(list)
        by_url: Dict[str, List[int]] = defaultdict(list)
        for ch in channels:
            key = _norm(ch, settings)
            if key:
                if use_tvg_id and ch.meta.tvg_id:
                    key = f"{key}|{ch.meta.tvg_id.lower()}"
                by_name[key].append(ch.uid)
            if ch.has_valid_url:
                by_url[ch.link.url.strip()].append(ch.uid)

        uids_name: Set[int] = set()
        uids_url: Set[int] = set()
        name_key_by_uid: Dict[int, str] = {}
        url_key_by_uid: Dict[int, str] = {}

        for key, uids in by_name.items():
            if len(uids) > 1:
                uids_name.update(uids)
                for uid in uids:
                    name_key_by_uid[uid] = key
        for key, uids in by_url.items():
            if len(uids) > 1:
                uids_url.update(uids)
                for uid in uids:
                    url_key_by_uid[uid] = key

        return (uids_name, uids_url,
                name_key_by_uid, url_key_by_uid)