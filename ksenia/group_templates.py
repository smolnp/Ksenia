# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Менеджер шаблонов группировки каналов (group-title)."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from logging_setup import logger
from models import ChannelData
from name_normalizer import NormMode, normalize_with_settings
from storage import BaseJsonStore


DEFAULT_FALLBACK_GROUP = "Без группы"


def _norm_key(name: str, settings: Any = None) -> str:
    if not name:
        return ""
    return normalize_with_settings(name, settings, NormMode.FULL)


class GroupTemplate:
    __slots__ = ('name', 'rules', 'created', 'modified',
                 'source_description')

    def __init__(self, name: str = ""):
        self.name: str = name
        self.rules: Dict[str, str] = {}
        self.source_description: Dict[str, str] = {}
        self.created: float = time.time()
        self.modified: float = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'rules': dict(self.rules),
            'source_description': dict(self.source_description),
            'created': self.created,
            'modified': self.modified,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GroupTemplate':
        t = cls()
        t.name = (data.get('name') or "").strip() or "Без имени"
        raw_rules = data.get('rules') or {}
        if isinstance(raw_rules, dict):
            t.rules = {str(k): str(v)
                       for k, v in raw_rules.items()
                       if k and v}
        raw_desc = data.get('source_description') or {}
        if isinstance(raw_desc, dict):
            t.source_description = {str(k): str(v)
                                     for k, v in raw_desc.items()
                                     if k}
        try:
            t.created = float(data.get('created') or time.time())
        except (ValueError, TypeError):
            t.created = time.time()
        try:
            t.modified = float(data.get('modified') or time.time())
        except (ValueError, TypeError):
            t.modified = time.time()
        return t

    def copy(self) -> 'GroupTemplate':
        c = GroupTemplate(self.name)
        c.rules = dict(self.rules)
        c.source_description = dict(self.source_description)
        c.created = self.created
        c.modified = self.modified
        return c


class GroupTemplateManager:
    def __init__(self, config_dir: str):
        self._path = os.path.join(config_dir,
                                    "group_templates.json")
        self._store = BaseJsonStore(self._path, [])
        self._lock = threading.RLock()
        self._templates: List[GroupTemplate] = [
            GroupTemplate.from_dict(x)
            for x in self._store._data
            if isinstance(x, dict) and x.get('name')]
        self._index_by_name: Dict[str, GroupTemplate] = {
            t.name: t for t in self._templates}

    def _persist_locked(self) -> bool:
        self._store._data = [t.to_dict() for t in self._templates]
        return self._store.save()

    def _rebuild_index_locked(self):
        self._index_by_name = {t.name: t for t in self._templates}

    def get_all(self) -> List[GroupTemplate]:
        with self._lock:
            return [t.copy() for t in self._templates]

    def get_names(self) -> List[str]:
        with self._lock:
            return sorted(self._index_by_name.keys(),
                          key=lambda s: s.lower())

    def get(self, name: str) -> Optional[GroupTemplate]:
        with self._lock:
            t = self._index_by_name.get(name)
            return t.copy() if t else None

    def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._index_by_name

    def save_template(self, template: GroupTemplate,
                      overwrite: bool = False) -> Tuple[bool, str]:
        if not template or not template.name:
            return False, "Пустое имя шаблона"
        with self._lock:
            existing = self._index_by_name.get(template.name)
            if existing is not None and not overwrite:
                return False, f"Шаблон «{template.name}» уже есть"
            if existing is not None:
                existing.rules = dict(template.rules)
                existing.source_description = dict(
                    template.source_description)
                existing.modified = time.time()
            else:
                t = template.copy()
                t.created = time.time()
                t.modified = time.time()
                self._templates.append(t)
                self._index_by_name[t.name] = t
            ok = self._persist_locked()
            if not ok:
                return False, "Не удалось сохранить шаблон на диск"
            return True, ""

    def rename_template(self, old_name: str,
                         new_name: str) -> Tuple[bool, str]:
        new_name = (new_name or "").strip()
        if not new_name:
            return False, "Пустое новое имя"
        with self._lock:
            t = self._index_by_name.get(old_name)
            if t is None:
                return False, f"Шаблон «{old_name}» не найден"
            if new_name in self._index_by_name:
                return False, f"Шаблон «{new_name}» уже есть"
            del self._index_by_name[old_name]
            t.name = new_name
            t.modified = time.time()
            self._index_by_name[new_name] = t
            ok = self._persist_locked()
            if not ok:
                return False, "Не удалось переименовать"
            return True, ""

    def delete_template(self, name: str) -> bool:
        with self._lock:
            t = self._index_by_name.pop(name, None)
            if t is None:
                return False
            self._templates = [x for x in self._templates
                                if x.name != name]
            return self._persist_locked()

    def build_from_channels(
            self, channels: List[ChannelData],
            name: str,
            settings: Any = None,
            include_only_existing_groups: bool = True,
    ) -> Tuple[Optional[GroupTemplate], str]:
        if not channels:
            return None, "Плейлист пуст"
        if not name or not name.strip():
            return None, "Введите имя шаблона"

        t = GroupTemplate(name.strip())
        used = 0
        conflicts: Dict[str, str] = {}
        for ch in channels:
            g = (ch.meta.group or "").strip()
            if (include_only_existing_groups
                    and (not g or g == DEFAULT_FALLBACK_GROUP)):
                continue
            if not g:
                g = DEFAULT_FALLBACK_GROUP
            key = _norm_key(ch.meta.name, settings)
            if not key:
                continue
            prev = t.rules.get(key)
            if prev is not None and prev != g:
                if key not in conflicts:
                    conflicts[key] = (
                        f"«{ch.meta.name}»: "
                        f"«{prev}» vs «{g}» (оставлено первое)")
                continue
            t.rules[key] = g
            if key not in t.source_description:
                t.source_description[key] = ch.meta.name or key
            used += 1

        if not t.rules:
            return None, ("Нечего сохранять: у каналов нет "
                          "group-title или все имена пусты")

        msg = f"Каналов в шаблоне: {len(t.rules)}"
        if conflicts:
            msg += f"\nКонфликтов (оставлено первое): {len(conflicts)}"
        return t, msg

    def apply_to_channels(
            self, template: GroupTemplate,
            channels: List[ChannelData],
            settings: Any = None,
            *,
            keep_unmatched: bool = True,
            create_missing_groups: bool = False,
            fallback_group: str = DEFAULT_FALLBACK_GROUP,
            case_insensitive: bool = True,
    ) -> Dict[str, Any]:
        if template is None or not channels:
            return {'changed': 0, 'matched': 0, 'unmatched': 0,
                    'missing': 0}

        rules = template.rules

        changed = 0
        matched = 0
        unmatched = 0
        seen_in_playlist = set()

        for ch in channels:
            key = _norm_key(ch.meta.name, settings)
            if not key:
                unmatched += 1
                if not keep_unmatched:
                    if ch.meta.group != fallback_group:
                        ch.meta.group = fallback_group
                        ch.update_extinf()
                        ch.touch()
                        changed += 1
                continue
            seen_in_playlist.add(key)
            target = rules.get(key)
            if target is None:
                unmatched += 1
                if not keep_unmatched:
                    if ch.meta.group != fallback_group:
                        ch.meta.group = fallback_group
                        ch.update_extinf()
                        ch.touch()
                        changed += 1
                continue
            matched += 1
            if ch.meta.group != target:
                ch.meta.group = target
                ch.update_extinf()
                ch.touch()
                changed += 1

        missing = sum(1 for k in rules if k not in seen_in_playlist)
        return {
            'changed': changed,
            'matched': matched,
            'unmatched': unmatched,
            'missing': missing,
        }