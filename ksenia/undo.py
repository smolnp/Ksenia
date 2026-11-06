# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""UndoRedoManager (Command-паттерн), SimpleDuplicateFinder.

Undo/Redo построен на командах, а не на снапшотах.

Почему Command, а не snapshot-diff:
  - "Проверка ссылок" меняет 50k каналов. Snapshot-diff на
    каждом действии = 50k × 2 × полный dict = 400 МБ.
  - Command хранит только изменившиеся поля: 50k × ~50 байт
    = 2.5 МБ. Разница в 160 раз.

_last_data хранит ChannelSnapshot (NamedTuple) — только для
сравнения на следующем save_state (hash в C-цикле).
"""
from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from constants import UNDO_MAX_STATES
from logging_setup import logger
from models import ChannelData, ChannelSnapshot
from name_normalizer import (NormMode, normalize_with_settings)


# Бюджет на весь _undo_stack: 300 МБ.
_MAX_MEMORY_BYTES = 300 * 1024 * 1024
# Максимум действий в _undo_stack.
_MAX_ACTIONS = UNDO_MAX_STATES  # 100


def _norm(ch: ChannelData, settings: Any) -> str:
    return normalize_with_settings(
        ch.meta.name or "", settings, NormMode.FULL)


# ── Command-паттерн ──

class Command:
    """Базовая команда. apply/undo знают, как менять канал.

    НЕ используем SetField(path) — url это property,
    а has_url — отдельное поле. Типизированные команды
    корректнее и быстрее (нет строкового парсинга).
    """
    __slots__ = ('uid',)

    def __init__(self, uid: int):
        self.uid = uid

    def apply(self, by_uid: Dict[int, ChannelData]) -> None:
        raise NotImplementedError

    def undo(self, by_uid: Dict[int, ChannelData]) -> None:
        raise NotImplementedError

    def approx_bytes(self) -> int:
        return 64


class SetMetaField(Command):
    __slots__ = ('field', 'old', 'new')

    def __init__(self, uid: int, field: str,
                 old: Any, new: Any):
        super().__init__(uid)
        self.field = field
        self.old = old
        self.new = new

    def apply(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is not None:
            setattr(ch.meta, self.field, self.new)
            ch.update_extinf()

    def undo(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is not None:
            setattr(ch.meta, self.field, self.old)
            ch.update_extinf()

    def approx_bytes(self):
        return 64 + 2 * 32


class SetLinkUrl(Command):
    __slots__ = ('old', 'new')

    def __init__(self, uid: int, old: str, new: str):
        super().__init__(uid)
        self.old = old
        self.new = new

    def apply(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is not None:
            ch.link.url = self.new
            ch.link.has_url = bool(self.new)
            ch.update_extinf()

    def undo(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is not None:
            ch.link.url = self.old
            ch.link.has_url = bool(self.old)
            ch.update_extinf()

    def approx_bytes(self):
        return 64 + 2 * 64


class SetLinkField(Command):
    __slots__ = ('field', 'old', 'new')

    def __init__(self, uid: int, field: str,
                 old: Any, new: Any):
        super().__init__(uid)
        self.field = field
        self.old = old
        self.new = new

    def apply(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is not None:
            setattr(ch.link, self.field, self.new)

    def undo(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is not None:
            setattr(ch.link, self.field, self.old)

    def approx_bytes(self):
        return 64 + 2 * 32


class SetStatus(Command):
    __slots__ = ('url_status', 'status_text', 'status_code',
                 'resolution', 'ttfb_ms', 'link_response_time',
                 'link_quality', 'old_values')

    def __init__(self, uid: int,
                 new_url_status: Optional[bool],
                 new_status_text: str,
                 new_status_code: Optional[int],
                 new_resolution: str,
                 new_ttfb_ms: Optional[float],
                 new_link_response_time: Optional[float],
                 new_link_quality: int,
                 old_values: Tuple):
        super().__init__(uid)
        self.url_status = new_url_status
        self.status_text = new_status_text
        self.status_code = new_status_code
        self.resolution = new_resolution
        self.ttfb_ms = new_ttfb_ms
        self.link_response_time = new_link_response_time
        self.link_quality = new_link_quality
        self.old_values = old_values

    def apply(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is None:
            return
        ch.status.url_status = self.url_status
        ch.status.status_text = self.status_text
        ch.status.status_code = self.status_code
        ch.status.resolution = self.resolution
        ch.status.ttfb_ms = self.ttfb_ms
        ch.status.link_response_time = self.link_response_time
        from models import LinkQuality
        try:
            ch.status.link_quality = LinkQuality(
                int(self.link_quality or 0))
        except (ValueError, TypeError):
            ch.status.link_quality = LinkQuality.UNKNOWN

    def undo(self, by_uid):
        ch = by_uid.get(self.uid)
        if ch is None:
            return
        (old_us, old_st, old_sc, old_res, old_ttfb,
         old_rt, old_lq) = self.old_values
        ch.status.url_status = old_us
        ch.status.status_text = old_st
        ch.status.status_code = old_sc
        ch.status.resolution = old_res
        ch.status.ttfb_ms = old_ttfb
        ch.status.link_response_time = old_rt
        from models import LinkQuality
        try:
            ch.status.link_quality = LinkQuality(
                int(old_lq or 0))
        except (ValueError, TypeError):
            ch.status.link_quality = LinkQuality.UNKNOWN

    def approx_bytes(self):
        return 64 + 2 * 80


class AddChannel(Command):
    __slots__ = ('snapshot',)

    def __init__(self, snapshot: ChannelSnapshot):
        super().__init__(snapshot.uid)
        self.snapshot = snapshot

    def apply(self, by_uid):
        # Восстановить канал в all_channels — работа PlaylistTab.
        # Здесь только логируем; применение делает callback.
        pass

    def undo(self, by_uid):
        pass

    def approx_bytes(self):
        return 64 + len(self.snapshot) * 32


class RemoveChannel(Command):
    __slots__ = ('snapshot', 'original_index')

    def __init__(self, snapshot: ChannelSnapshot,
                 original_index: int):
        super().__init__(snapshot.uid)
        self.snapshot = snapshot
        self.original_index = original_index

    def apply(self, by_uid):
        pass

    def undo(self, by_uid):
        pass

    def approx_bytes(self):
        return 64 + len(self.snapshot) * 32


class ReorderChannels(Command):
    __slots__ = ('old_order', 'new_order')

    def __init__(self, old_order: List[int],
                 new_order: List[int]):
        super().__init__(-1)
        self.old_order = list(old_order)
        self.new_order = list(new_order)

    def apply(self, by_uid):
        pass

    def undo(self, by_uid):
        pass

    def approx_bytes(self):
        return 64 + (len(self.old_order) + len(self.new_order)) * 8


class UndoRedoManager:
    def __init__(self, max_states: int = _MAX_ACTIONS):
        self._undo_stack: List[List[Command]] = []
        self._redo_stack: List[List[Command]] = []
        self._max_states = min(max_states, _MAX_ACTIONS)
        # Снапшоты — только для сравнения, не для восстановления.
        self._last_data: Dict[int, ChannelSnapshot] = {}
        self._tombstones: Set[int] = set()
        self._last_order: List[int] = []
        self._initialized: bool = False
        self._lock = threading.RLock()
        self._apply_callback: Optional[
            Callable[[List[Command], bool], bool]] = None
        # Приблизительный размер _last_data в байтах.
        self._last_data_bytes: int = 0
        # Приблизительный размер _undo_stack.
        self._undo_bytes: int = 0

    def bind_apply_callback(
            self,
            cb: Callable[[List[Command], bool], bool]) -> None:
        self._apply_callback = cb

    def _snapshot(self, ch: ChannelData) -> ChannelSnapshot:
        return ch.snapshot()

    def save_state(self, channels: List[ChannelData],
                   description: str = ""):
        """Сравнивает текущее состояние с _last_data и создаёт
        список команд для изменений.

        P1.2: O(N) по времени (сравнение хэшей), O(K) по памяти
        (только изменившиеся снапшоты + команды).
        """
        with self._lock:
            # 1. Снимок текущего состояния.
            by_uid: Dict[int, ChannelData] = {
                ch.uid: ch for ch in channels}
            current_snap: Dict[int, ChannelSnapshot] = {
                ch.uid: ch.snapshot() for ch in channels}
            current_order: List[int] = [ch.uid for ch in channels]

            if not self._initialized:
                self._last_data = current_snap
                self._last_order = current_order
                self._initialized = True
                self._last_data_bytes = self._estimate_bytes(
                    current_snap)
                return

            # 2. Сравниваем.
            commands: List[Command] = []
            added_uids: List[int] = []
            removed_uids: List[int] = []
            old_order = self._last_order

            for uid, new_snap in current_snap.items():
                if uid in self._tombstones:
                    continue
                old_snap = self._last_data.get(uid)
                if old_snap is None:
                    added_uids.append(uid)
                    continue
                if old_snap == new_snap:
                    continue
                cmds = self._diff_snapshots(old_snap, new_snap)
                commands.extend(cmds)

            for uid in self._last_data.keys():
                if uid not in current_snap:
                    removed_uids.append(uid)

            order_changed = (old_order != current_order)
            if not (commands or added_uids
                    or removed_uids or order_changed):
                return

            # 3. Команды для добавлений/удалений/порядка.
            for uid in added_uids:
                snap = current_snap[uid]
                commands.append(AddChannel(snap))
            for uid in removed_uids:
                old_snap = self._last_data.get(uid)
                if old_snap is not None:
                    commands.append(RemoveChannel(old_snap, 0))
            if order_changed:
                commands.append(ReorderChannels(
                    old_order, current_order))

            if not commands:
                return

            # 4. Кладём в стек.
            self._undo_stack.append(commands)
            self._redo_stack.clear()
            self._undo_bytes += self._estimate_commands_bytes(
                commands)

            # 5. Обновляем _last_data (только для изменившихся).
            for uid, snap in current_snap.items():
                if uid in self._tombstones:
                    continue
                old = self._last_data.get(uid)
                if old is None or old != snap:
                    if old is not None:
                        self._last_data_bytes -= self._estimate_bytes(
                            {uid: old})
                    self._last_data_bytes += self._estimate_bytes(
                        {uid: snap})
                    self._last_data[uid] = snap
            for uid in removed_uids:
                old = self._last_data.pop(uid, None)
                if old is not None:
                    self._last_data_bytes -= self._estimate_bytes(
                        {uid: old})
            self._last_order = current_order

            # 6. Бюджет: выбрасываем старые действия целиком.
            self._enforce_budget()

    def _diff_snapshots(self, old: ChannelSnapshot,
                        new: ChannelSnapshot) -> List[Command]:
        cmds: List[Command] = []
        uid = old.uid

        # meta-поля
        meta_fields = (
            'name', 'group', 'tvg_id', 'tvg_name', 'tvg_logo',
            'region', 'quality', 'tvg_shift', 'timeshift',
            'catchup', 'catchup_source', 'catchup_days',
            'tvg_rec', 'tvg_chno', 'audio_track', 'epg_source',
        )
        for f in meta_fields:
            ov = getattr(old, f, None)
            nv = getattr(new, f, None)
            if ov != nv:
                cmds.append(SetMetaField(uid, f, ov, nv))

        # epg_alternative_names
        if tuple(old.epg_alternative_names or ()) != \
                tuple(new.epg_alternative_names or ()):
            cmds.append(SetMetaField(
                uid, 'epg_alternative_names',
                list(old.epg_alternative_names or []),
                list(new.epg_alternative_names or [])))

        # link-поля
        if old.url != new.url:
            cmds.append(SetLinkUrl(uid, old.url, new.url))
        link_fields = (
            'user_agent', 'has_url', 'link_source', 'is_repaired',
        )
        for f in link_fields:
            ov = getattr(old, f, None)
            nv = getattr(new, f, None)
            if ov != nv:
                cmds.append(SetLinkField(uid, f, ov, nv))
        if tuple(old.extvlcopt_lines or ()) != \
                tuple(new.extvlcopt_lines or ()):
            cmds.append(SetLinkField(
                uid, 'extvlcopt_lines',
                list(old.extvlcopt_lines or []),
                list(new.extvlcopt_lines or [])))
        if tuple(old.extra_headers or ()) != \
                tuple(new.extra_headers or ()):
            cmds.append(SetLinkField(
                uid, 'extra_headers',
                dict(old.extra_headers or {}),
                dict(new.extra_headers or {})))
        if tuple(old.alternative_urls or ()) != \
                tuple(new.alternative_urls or ()):
            cmds.append(SetLinkField(
                uid, 'alternative_urls',
                list(old.alternative_urls or []),
                list(new.alternative_urls or [])))

        # status
        old_status = (
            old.url_status, old.status_text, old.status_code,
            old.resolution, old.ttfb_ms, old.link_response_time,
            old.link_quality,
        )
        new_status = (
            new.url_status, new.status_text, new.status_code,
            new.resolution, new.ttfb_ms, new.link_response_time,
            new.link_quality,
        )
        if old_status != new_status:
            cmds.append(SetStatus(
                uid,
                new_url_status=new.url_status,
                new_status_text=new.status_text,
                new_status_code=new.status_code,
                new_resolution=new.resolution,
                new_ttfb_ms=new.ttfb_ms,
                new_link_response_time=new.link_response_time,
                new_link_quality=new.link_quality,
                old_values=old_status,
            ))

        # original_index
        if old.original_index != new.original_index:
            cmds.append(SetMetaField(
                uid, '__original_index__',
                old.original_index, new.original_index))

        return cmds

    def _enforce_budget(self):
        """Выбрасываем старые действия целиком, пока не
        уложимся в _MAX_MEMORY_BYTES и _max_states."""
        while (len(self._undo_stack) > self._max_states
               or self._undo_bytes > _MAX_MEMORY_BYTES):
            if not self._undo_stack:
                break
            dropped = self._undo_stack.pop(0)
            self._undo_bytes -= self._estimate_commands_bytes(
                dropped)
            # Снапшоты для каналов, которые были только в
            # этом действии, остаются в _last_data — это ок,
            # они всё ещё актуальны.

    @staticmethod
    def _estimate_bytes(snapshots: Dict[int, ChannelSnapshot]
                        ) -> int:
        # Приблизительная оценка: 2 КБ на канал.
        return len(snapshots) * 2048

    @staticmethod
    def _estimate_commands_bytes(commands: List[Command]) -> int:
        return sum(c.approx_bytes() for c in commands)

    def _apply(self, commands: List[Command],
               reverse: bool) -> bool:
        if self._apply_callback is None:
            return False
        try:
            return bool(self._apply_callback(commands, reverse))
        except Exception:
            logger.exception("apply_callback failed")
            return False

    def undo(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._undo_stack:
                return None
            commands = self._undo_stack[-1]
            if not self._apply(commands, reverse=True):
                return None
            self._undo_stack.pop()
            self._redo_stack.append(commands)
            self._undo_bytes -= self._estimate_commands_bytes(
                commands)
            return {'description': 'undo',
                    'commands': commands}

    def redo(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._redo_stack:
                return None
            commands = self._redo_stack[-1]
            if not self._apply(commands, reverse=False):
                return None
            self._redo_stack.pop()
            self._undo_stack.append(commands)
            self._undo_bytes += self._estimate_commands_bytes(
                commands)
            return {'description': 'redo',
                    'commands': commands}

    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._undo_stack)

    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo_stack)

    def reset(self, channels: List[ChannelData]):
        with self._lock:
            self._last_data = {
                ch.uid: ch.snapshot() for ch in channels}
            self._last_order = [ch.uid for ch in channels]
            self._last_data_bytes = self._estimate_bytes(
                self._last_data)
            self._initialized = True
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._undo_bytes = 0
            self._tombstones.clear()


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