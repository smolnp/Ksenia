# -*- coding: utf-8 -*-
"""UndoRedoManager, SimpleDuplicateFinder."""

from __future__ import annotations
import json
import hashlib
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict
from .constants import UNDO_MAX_STATES
from .models import ChannelData


class UndoRedoManager:
    def __init__(self, max_states: int = UNDO_MAX_STATES):
        self._undo_stack: List[Dict[str, Any]] = []
        self._redo_stack: List[Dict[str, Any]] = []
        self._max_states = max_states
        self._last_snapshot: Dict[int, str] = {}
        self._last_data: Dict[int, Dict[str, Any]] = {}
        self._last_order: List[int] = []
        self._lock = threading.RLock()

    @staticmethod
    def _hash_channel(ch: ChannelData) -> str:
        mod = ch.modified_date
        if ch._cached_hash is not None and ch._cached_hash_mod == mod:
            return ch._cached_hash
        d = ch.to_dict()
        h = hashlib.blake2b(
            json.dumps(d, sort_keys=True, default=str).encode('utf-8'),
            digest_size=8).hexdigest()
        object.__setattr__(ch, '_cached_hash', h)
        object.__setattr__(ch, '_cached_hash_mod', mod)
        return h

    def _current_snapshot(self, channels: List[ChannelData]
                          ) -> Tuple[Dict[int, str], List[int]]:
        snap: Dict[int, str] = {}
        order: List[int] = []
        for ch in channels:
            snap[ch.uid] = self._hash_channel(ch)
            order.append(ch.uid)
        return snap, order

    def save_state(self, channels: List[ChannelData], description: str = ""):
        with self._lock:
            new_snap, new_order = self._current_snapshot(channels)

            if not self._last_snapshot and not self._last_data:
                self._last_snapshot = new_snap
                self._last_data = {ch.uid: ch.to_dict() for ch in channels}
                self._last_order = new_order
                return

            old_snap = self._last_snapshot
            old_order = list(self._last_order)

            added_uids = [uid for uid in new_snap if uid not in old_snap]
            removed_uids = [uid for uid in old_snap if uid not in new_snap]
            changed_uids = [uid for uid in new_snap
                            if uid in old_snap and old_snap[uid] != new_snap[uid]]

            old_data_map = self._last_data
            channels_by_uid: Dict[int, ChannelData] = {ch.uid: ch for ch in channels}

            new_data_map: Dict[int, Dict[str, Any]] = {}
            for uid in added_uids:
                ch = channels_by_uid.get(uid)
                if ch is not None:
                    new_data_map[uid] = ch.to_dict()
            for uid in changed_uids:
                ch = channels_by_uid.get(uid)
                if ch is not None:
                    new_data_map[uid] = ch.to_dict()

            added_data = [(uid, new_data_map[uid]) for uid in added_uids
                          if uid in new_data_map]
            removed_data = [(uid, old_data_map[uid]) for uid in removed_uids
                            if uid in old_data_map]
            changed_data = []
            for uid in changed_uids:
                old_d = old_data_map.get(uid)
                new_d = new_data_map.get(uid)
                if old_d is not None and new_d is not None:
                    changed_data.append((uid, old_d, new_d))

            order_changed = (old_order != new_order)
            if not (added_data or removed_data or changed_data or order_changed):
                return

            diff: Dict[str, Any] = {
                'description': description,
                'timestamp': datetime.now().isoformat(),
                'added': added_data,
                'removed': removed_data,
                'changed': changed_data,
                'old_order': old_order,
                'new_order': new_order,
                'old_snapshot': dict(old_snap),
                'new_snapshot': dict(new_snap),
            }

            self._undo_stack.append(diff)
            if len(self._undo_stack) > self._max_states:
                self._undo_stack.pop(0)
            self._redo_stack.clear()

            new_last_data = dict(old_data_map)
            for uid in removed_uids:
                new_last_data.pop(uid, None)
            for uid in added_uids:
                if uid in new_data_map:
                    new_last_data[uid] = new_data_map[uid]
            for uid in changed_uids:
                if uid in new_data_map:
                    new_last_data[uid] = new_data_map[uid]
            self._last_data = new_last_data
            self._last_snapshot = new_snap
            self._last_order = new_order

    def undo(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._undo_stack:
                return None
            diff = self._undo_stack.pop()
            self._redo_stack.append(diff)
            self._restore_internal_state(diff, reverse=True)
            return diff

    def redo(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._redo_stack:
                return None
            diff = self._redo_stack.pop()
            self._undo_stack.append(diff)
            self._restore_internal_state(diff, reverse=False)
            return diff

    def _restore_internal_state(self, diff: Dict[str, Any], reverse: bool):
        target_snap = diff.get('old_snapshot' if reverse else 'new_snapshot')
        target_order = diff.get('old_order' if reverse else 'new_order')
        if target_snap is not None:
            self._last_snapshot = dict(target_snap)
        if target_order is not None:
            self._last_order = list(target_order)
        new_data: Dict[int, Dict[str, Any]] = dict(self._last_data)
        if reverse:
            for entry in diff.get('changed', []):
                if len(entry) >= 3:
                    uid, old_d, _new_d = entry[0], entry[1], entry[2]
                    new_data[uid] = old_d
            for uid, _d in diff.get('added', []):
                new_data.pop(uid, None)
            for uid, d in diff.get('removed', []):
                new_data[uid] = d
        else:
            for entry in diff.get('changed', []):
                if len(entry) >= 3:
                    uid, _old_d, new_d = entry[0], entry[1], entry[2]
                    new_data[uid] = new_d
            for uid, d in diff.get('added', []):
                new_data[uid] = d
            for uid, _d in diff.get('removed', []):
                new_data.pop(uid, None)
        self._last_data = new_data

    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._undo_stack)

    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo_stack)

    def reset(self, channels: List[ChannelData]):
        with self._lock:
            snap, order = self._current_snapshot(channels)
            self._last_snapshot = snap
            self._last_data = {ch.uid: ch.to_dict() for ch in channels}
            self._last_order = order
            self._undo_stack.clear()
            self._redo_stack.clear()


class SimpleDuplicateFinder:
    @staticmethod
    def find_duplicates_by_url(channels: List[ChannelData]) -> List[List[ChannelData]]:
        url_map: Dict[str, List[ChannelData]] = defaultdict(list)
        for ch in channels:
            if ch.has_valid_url:
                url_map[ch.link.url.strip()].append(ch)
        return [group for group in url_map.values() if len(group) > 1]

    @staticmethod
    def find_duplicates_by_name(channels: List[ChannelData],
                                 use_tvg_id: bool = False
                                 ) -> List[List[ChannelData]]:
        name_map: Dict[str, List[ChannelData]] = defaultdict(list)
        for ch in channels:
            key = ch.normalized_name()
            if not key:
                continue
            if use_tvg_id and ch.meta.tvg_id:
                key = f"{key}|{ch.meta.tvg_id.lower()}"
            name_map[key].append(ch)
        return [group for group in name_map.values() if len(group) > 1]

    @staticmethod
    def get_duplicate_report(channels: List[ChannelData],
                              use_tvg_id: bool = False) -> Dict[str, Any]:
        by_url = SimpleDuplicateFinder.find_duplicates_by_url(channels)
        by_name = SimpleDuplicateFinder.find_duplicates_by_name(
            channels, use_tvg_id=use_tvg_id)
        return {
            'by_url': by_url,
            'by_name': by_name,
            'total_url_duplicates': sum(len(g) - 1 for g in by_url),
            'total_name_duplicates': sum(len(g) - 1 for g in by_name),
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
                                   use_tvg_id: bool = False
                                   ) -> Tuple[List[ChannelData], int]:
        seen: Set[str] = set()
        result: List[ChannelData] = []
        removed = 0
        for ch in channels:
            key = ch.normalized_name()
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
