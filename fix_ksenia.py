#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_ksenia.py — единый ремонт проекта Ksenia (v6).

Чинит регрессии R1-R13 из аудита v4 + v6:
  • сортировка по «№» (текущая позиция строки) при открытии,
  • add_channel None-safe,
  • дубль watchdog в ksenia.py.

Идемпотентен. Каждая правка проверяется на синтаксис.

Запуск:
    py fix_ksenia.py             # применить
    py fix_ksenia.py --dry-run   # показать без записи
    py fix_ksenia.py --list      # список правок
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Tuple


def _patch(text: str, old: str, new: str, count: int = 1
           ) -> Tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, count), True


def _patch_all(text: str, old: str, new: str) -> Tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new), True


def code_lines(s: str) -> int:
    return sum(1 for ln in s.split('\n')
               if ln.strip() and not ln.strip().startswith('#'))


def is_syntax_ok(text: str) -> Tuple[bool, str]:
    try:
        ast.parse(text)
        return True, ""
    except SyntaxError as e:
        return False, f"{e.lineno}: {e.msg}"


def apply_one_fix(text: str, fn: Callable
                  ) -> Tuple[str, bool, str]:
    try:
        new_text, ok = fn(text)
    except Exception as e:
        return text, False, f"исключение в фиксере: {e}"
    if not ok:
        return text, False, ""
    ok_syntax, err = is_syntax_ok(new_text)
    if not ok_syntax:
        return text, False, f"синтаксис сломан: {err}"
    return new_text, True, ""


# ===========================================================================
# ФАЗА 1 — ОТНОСИТЕЛЬНЫЕ ИМПОРТЫ
# ===========================================================================

_RE_REL_IMPORT_MODULE = re.compile(r'\bfrom \.(\w+)\.(\w+) import')
_RE_REL_IMPORT = re.compile(r'\bfrom \.(\w+) import')


def replace_relative_imports(text: str) -> Tuple[str, int]:
    n = 0

    def _rep2(m):
        nonlocal n
        n += 1
        return f'from {m.group(2)} import'

    text = _RE_REL_IMPORT_MODULE.sub(_rep2, text)

    def _rep1(m):
        nonlocal n
        n += 1
        return f'from {m.group(1)} import'

    text = _RE_REL_IMPORT.sub(_rep1, text)
    return text, n


# ===========================================================================
# ФАЗА 2 — ЧИСТКА МАРКЕРОВ
# ===========================================================================

_RE_BLOCK_HEADER = re.compile(
    r'^\s*#\s*=+\s*(?:KSENIA\s+FIX|FIX|KSENIA\s+PLAYER\s+PATCH).*?=+\s*$',
    re.IGNORECASE,
)
_RE_PURE_SEP = re.compile(r'^\s*#\s*=+\s*$')
_RE_ONE_LINE_FIX = re.compile(
    r'^\s*#\s*v\d+\.\d+(?:\.\d+)?\s*(?:fix\s*:?\s*|:\s*|[—\-–]\s*)',
    re.IGNORECASE,
)
_RE_BARE_VERSION = re.compile(
    r'^\s*#\s*v\d+\.\d+(?:\.\d+)?\s*(?:\(.*\))?\s*$',
    re.IGNORECASE,
)


def clean_markers(text: str) -> Tuple[str, int]:
    lines = text.split('\n')
    out: List[str] = []
    removed = 0
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        if _RE_BLOCK_HEADER.match(line):
            removed += 1
            i += 1
            while i < n:
                cur = lines[i]
                if _RE_BLOCK_HEADER.match(cur):
                    break
                if _RE_PURE_SEP.match(cur):
                    removed += 1
                    i += 1
                    break
                s = cur.strip()
                if s and not s.startswith('#'):
                    break
                removed += 1
                i += 1
            while out and out[-1].strip() == '':
                out.pop()
            if i < n and lines[i].strip() != '':
                out.append('')
            continue

        if _RE_ONE_LINE_FIX.match(line) or _RE_BARE_VERSION.match(line):
            removed += 1
            i += 1
            while i < n:
                cur = lines[i]
                s = cur.strip()
                if s == '' or s.startswith('#'):
                    removed += 1
                    i += 1
                    continue
                break
            continue

        out.append(line)
        i += 1

    cleaned: List[str] = []
    blank = 0
    for ln in out:
        if ln.strip() == '':
            blank += 1
            if blank <= 1:
                cleaned.append(ln)
        else:
            blank = 0
            cleaned.append(ln)

    return '\n'.join(cleaned), removed


# ===========================================================================
# РЕЕСТР КОД-ФИКСОВ
# ===========================================================================

FIXES: List[Tuple[str, str, str, Callable[[str], Tuple[str, bool]]]] = []


def register(fname: str, desc: str, risk: str = "safe"):
    def deco(fn):
        FIXES.append((fname, desc, risk, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# constants.py: APP_VERSION 0.1
# ---------------------------------------------------------------------------
@register("constants.py", 'APP_VERSION → "0.1"', "safe")
def fx_const_version(text: str) -> Tuple[str, bool]:
    if 'APP_VERSION = "0.1"' in text:
        return text, False
    for old in ('APP_VERSION = "2026"', 'APP_VERSION = "2025"',
                'APP_VERSION = "2024"'):
        text, ok = _patch(text, old, 'APP_VERSION = "0.1"')
        if ok:
            return text, True
    return text, False


# ---------------------------------------------------------------------------
# ksenia.py: убрать битый shebang
# ---------------------------------------------------------------------------
@register("ksenia.py", "убрать битый shebang", "safe")
def fx_ksenia_shebang(text: str) -> Tuple[str, bool]:
    if text.startswith('#!usrbinenv python3\n'):
        return text[len('#!usrbinenv python3\n'):], True
    return text, False


# ---------------------------------------------------------------------------
# utils.py: расширить MOJIBAKE_TRIGGERS
# ---------------------------------------------------------------------------
@register("utils.py",
          "fix_encoding: расширить MOJIBAKE_TRIGGERS", "safe")
def fx_utils_mojibake_triggers(text: str) -> Tuple[str, bool]:
    if "'Ð°'" in text or "'ÐŸ'" in text:
        return text, False
    old = "MOJIBAKE_TRIGGERS = ('–°', '–∞', '–µ', '–∏', '—Å', '—Ä', '√©', '√®')"
    if old not in text:
        return text, False
    new = (
        "MOJIBAKE_TRIGGERS = (\n"
        "        # cp1251 → latin1 (кириллица)\n"
        "        'Ð°', 'Ð±', 'Ð²', 'Ð³', 'Ð´', 'Ðµ', 'Ð¶', 'Ð·',\n"
        "        'Ð¸', 'Ð¹', 'Ðº', 'Ð»', 'Ð¼', 'Ð½', 'Ð¾', 'Ð¿',\n"
        "        'Ñ€', 'Ñ', 'Ñ‚', 'Ñƒ', 'Ñ„', 'Ñ…', 'Ñ†', 'Ñ‡',\n"
        "        'ÐŸ', 'Ð', 'Ð¡', 'Ð¢', 'Ð£', 'Ð¤', 'Ð¥', 'Ð¦',\n"
        "        'Ñ‰', 'ÑŠ', 'Ñ‹', 'ÑŒ', 'Ñ', 'ÑŽ', 'Ñ',\n"
        "        # cp1251 → utf-8 двойное перекодирование\n"
        "        '–°', '–∞', '–µ', '–∏', '—Å', '—Ä', '√©', '√®',\n"
        "    )"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# models.py: _reserve_uid race condition
# ---------------------------------------------------------------------------
@register("models.py",
          "_reserve_uid: int+lock вместо itertools.count", "safe")
def fx_models_reserve_uid(text: str) -> Tuple[str, bool]:
    if "_uid_next: int = 1" in text:
        return text, False
    old = (
        "    _uid_counter = itertools.count(1)\n"
        "    _uid_lock = threading.Lock()\n"
        "    _uid_max = 0\n"
    )
    new = (
        "    _uid_next: int = 1\n"
        "    _uid_lock = threading.Lock()\n"
    )
    text, c1 = _patch(text, old, new)
    if not c1:
        return text, False

    old_next = (
        "    @classmethod\n"
        "    def _next_uid(cls) -> int:\n"
        "        with cls._uid_lock:\n"
        "            return next(cls._uid_counter)\n"
        "\n"
        "    @classmethod\n"
        "    def _reserve_uid(cls, uid: int):\n"
        "        with cls._uid_lock:\n"
        "            if uid >= cls._uid_max:\n"
        "                cls._uid_max = uid\n"
        "                cls._uid_counter = itertools.count(uid + 1)\n"
    )
    new_next = (
        "    @classmethod\n"
        "    def _next_uid(cls) -> int:\n"
        "        with cls._uid_lock:\n"
        "            uid = cls._uid_next\n"
        "            cls._uid_next += 1\n"
        "            return uid\n"
        "\n"
        "    @classmethod\n"
        "    def _reserve_uid(cls, uid: int):\n"
        "        with cls._uid_lock:\n"
        "            if uid >= cls._uid_next:\n"
        "                cls._uid_next = uid + 1\n"
    )
    text, c2 = _patch(text, old_next, new_next)
    return text, (c1 or c2)


# ---------------------------------------------------------------------------
# models.py: restore_from_dict — сброс кэшей
# ---------------------------------------------------------------------------
@register("models.py",
          "restore_from_dict: сброс _cached_hash/norm", "safe")
def fx_models_restore_invalidate(text: str) -> Tuple[str, bool]:
    if "self._invalidate_caches()" in text:
        return text, False
    old = (
        "        with suppress(ValueError, TypeError):\n"
        "            object.__setattr__(self, 'original_index',\n"
        "                               int(data.get('original_index', -1)))\n"
    )
    new = (
        "        with suppress(ValueError, TypeError):\n"
        "            object.__setattr__(self, 'original_index',\n"
        "                               int(data.get('original_index', -1)))\n"
        "        self._invalidate_caches()\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# models.py: update_extinf — не писать DEFAULT_GROUP
# ---------------------------------------------------------------------------
@register("models.py",
          "update_extinf: не писать group-title для DEFAULT_GROUP", "safe")
def fx_models_extinf_group(text: str) -> Tuple[str, bool]:
    if "if self.group and self.group != DEFAULT_GROUP:" in text:
        return text, False
    old = (
        "        if self.group:\n"
        "            parts.append(f'group-title=\"{self._escape(self.group)}\"')\n"
    )
    new = (
        "        if self.group and self.group != DEFAULT_GROUP:\n"
        "            parts.append(f'group-title=\"{self._escape(self.group)}\"')\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# blacklists.py: add_channel None-safe
# ---------------------------------------------------------------------------
@register("blacklists.py",
          "add_channel: None-safe .lower()", "safe")
def fx_bl_add_channel_none_safe(text: str) -> Tuple[str, bool]:
    old = (
        "        for it in self._store._data:\n"
        "            if (it.get('name', '').lower() == name.lower() and\n"
        "                    it.get('tvg_id', '').lower() == tvg_id.lower()):\n"
    )
    new = (
        "        for it in self._store._data:\n"
        "            if ((it.get('name') or '').lower() == (name or '').lower() and\n"
        "                    (it.get('tvg_id') or '').lower() == (tvg_id or '').lower()):\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# blacklists.py: remove_channel None-safe (страховка)
# ---------------------------------------------------------------------------
@register("blacklists.py",
          "remove_channel: None-safe .lower()", "safe")
def fx_bl_remove_channel_none_safe(text: str) -> Tuple[str, bool]:
    old = (
        "            for i, it in enumerate(self._store._data):\n"
        "                if (it.get('name', '').lower() == name.lower() and\n"
        "                        it.get('tvg_id', '').lower() == tvg_id.lower()):\n"
    )
    new = (
        "            for i, it in enumerate(self._store._data):\n"
        "                if ((it.get('name') or '').lower() == (name or '').lower() and\n"
        "                        (it.get('tvg_id') or '').lower() == (tvg_id or '').lower()):\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# blacklists.py: filter_channels None-safe
# ---------------------------------------------------------------------------
@register("blacklists.py",
          "filter_channels: None-safe .lower()", "safe")
def fx_bl_filter_channels_none_safe(text: str) -> Tuple[str, bool]:
    old = (
        "        for bi in bl:\n"
        "            n = bi.get('name', '').strip().lower()\n"
        "            t = bi.get('tvg_id', '').strip().lower()\n"
    )
    new = (
        "        for bi in bl:\n"
        "            n = (bi.get('name') or '').strip().lower()\n"
        "            t = (bi.get('tvg_id') or '').strip().lower()\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# workers.py: fresh.get с .lower()
# ---------------------------------------------------------------------------
@register("workers.py",
          "SourceUrlCheckWorker: fresh.get с .lower()", "safe")
def fx_workers_fresh_lower(text: str) -> Tuple[str, bool]:
    if "fresh.get((ch.meta.name.lower(), url))" in text:
        return text, False
    old = "fresh.get((ch.meta.name, url))"
    new = "fresh.get((ch.meta.name.lower(), url))"
    return _patch_all(text, old, new)


# ---------------------------------------------------------------------------
# epg.py: сброс _fuzzy_cache после load_from_xmltv
# ---------------------------------------------------------------------------
@register("epg.py",
          "load_from_xmltv: сброс _fuzzy_cache", "safe")
def fx_epg_fuzzy_cache_reset(text: str) -> Tuple[str, bool]:
    if "        # Сброс fuzzy-кэша" in text:
        return text, False
    old = (
        "        if self.cache_manager:\n"
        "            if to_cache:\n"
        "                self.cache_manager.save_epg_entries(to_cache, source)\n"
        "            if info_to_cache:\n"
        "                self.cache_manager.save_epg_channels(info_to_cache, source)\n"
        "        return count\n"
    )
    new = (
        "        if self.cache_manager:\n"
        "            if to_cache:\n"
        "                self.cache_manager.save_epg_entries(to_cache, source)\n"
        "            if info_to_cache:\n"
        "                self.cache_manager.save_epg_channels(info_to_cache, source)\n"
        "        # Сброс fuzzy-кэша: _channel_info обновилось\n"
        "        with self._fuzzy_cache_lock:\n"
        "            self._fuzzy_cache.clear()\n"
        "        return count\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# epg.py: _tokens_for LRU
# ---------------------------------------------------------------------------
@register("epg.py",
          "_tokens_for: LRU вместо clear()", "safe")
def fx_epg_token_lru(text: str) -> Tuple[str, bool]:
    if "drop = max(1, len(self._token_cache) // 8)" in text:
        return text, False
    old = (
        "        with self._token_cache_lock:\n"
        "            if len(self._token_cache) >= EPG_FUZZY_CACHE_LIMIT:\n"
        "                self._token_cache.clear()\n"
        "            self._token_cache[text] = tokens\n"
    )
    new = (
        "        with self._token_cache_lock:\n"
        "            if len(self._token_cache) >= EPG_FUZZY_CACHE_LIMIT:\n"
        "                drop = max(1, len(self._token_cache) // 8)\n"
        "                for _ in range(drop):\n"
        "                    self._token_cache.pop(\n"
        "                        next(iter(self._token_cache)), None)\n"
        "            self._token_cache[text] = tokens\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# epg.py: is_loaded — правильный лок
# ---------------------------------------------------------------------------
@register("epg.py",
          "is_loaded: читать _channel_info под _channel_info_lock", "safe")
def fx_epg_is_loaded_lock(text: str) -> Tuple[str, bool]:
    if "has_entries = bool(self._entries)" in text:
        return text, False
    old = (
        "    @property\n"
        "    def is_loaded(self) -> bool:\n"
        "        with self._lock:\n"
        "            return bool(self._entries) or bool(self._channel_info)\n"
    )
    new = (
        "    @property\n"
        "    def is_loaded(self) -> bool:\n"
        "        with self._lock:\n"
        "            has_entries = bool(self._entries)\n"
        "        if has_entries:\n"
        "            return True\n"
        "        with self._channel_info_lock:\n"
        "            return bool(self._channel_info)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# epg.py: find_channel_info — нормализация norm_key
# ---------------------------------------------------------------------------
@register("epg.py",
          "find_channel_info: нормализовать norm_key", "safe")
def fx_epg_find_norm_key(text: str) -> Tuple[str, bool]:
    if "ChannelNameNormalizer.normalize(\n                            self._channel_info[c].display_name" in text:
        return text, False
    old = (
        "                    snapshot = [(c, (self._channel_info[c].display_name or c))\n"
        "                                for c in cid_set if c in self._channel_info]\n"
    )
    new = (
        "                    snapshot = [\n"
        "                        (c, ChannelNameNormalizer.normalize(\n"
        "                            self._channel_info[c].display_name or c))\n"
        "                        for c in cid_set if c in self._channel_info\n"
        "                    ]\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: _load_file через QTimer
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "PlaylistTab: отложить _load_file через QTimer", "safe")
def fx_kw_defer_load(text: str) -> Tuple[str, bool]:
    if "QTimer.singleShot(0, lambda: self._load_file(_fp))" in text:
        return text, False
    old = (
        "        if filepath and os.path.exists(filepath):\n"
        "            self._load_file(filepath)\n"
        "        else:\n"
        "            self.refresh_view()\n"
    )
    new = (
        "        if filepath and os.path.exists(filepath):\n"
        "            # Отложить загрузку — сигналы info_changed/undo_state_changed\n"
        "            # будут подключены в MainWindow._create_tab до вызова.\n"
        "            _fp = filepath\n"
        "            QTimer.singleShot(0, lambda: self._load_file(_fp))\n"
        "        else:\n"
        "            self.refresh_view()\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: UA-правила → modified = True
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_load_file: UA-правила → modified = True", "safe")
def fx_kw_ua_modified(text: str) -> Tuple[str, bool]:
    old = (
        "        try:\n"
        "            if self.core.domain_user_agent_manager.get_all_rules():\n"
        "                modified = self.core.apply_domain_user_agent(self.all_channels)\n"
        "                if modified:\n"
        "                    logger.info(f\"UA применён к {modified} каналам\")\n"
        "        except Exception:\n"
        "            logger.exception(\"UA rules error\")\n"
    )
    new = (
        "        try:\n"
        "            if self.core.domain_user_agent_manager.get_all_rules():\n"
        "                modified = self.core.apply_domain_user_agent(self.all_channels)\n"
        "                if modified:\n"
        "                    logger.info(f\"UA применён к {modified} каналам\")\n"
        "                    self.modified = True\n"
        "        except Exception:\n"
        "            logger.exception(\"UA rules error\")\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: _delete_channel очищает selected
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_delete_channel: очистить selected/current", "safe")
def fx_kw_delete_clear_selected(text: str) -> Tuple[str, bool]:
    old = (
        "        self.save_state(\"Удаление канала\")\n"
        "        self.all_channels = [ch for ch in self.all_channels\n"
        "                             if ch.uid not in uids]\n"
        "        self.sync_to_core()\n"
        "        with self._suppress_save():\n"
        "            self.model.set_channels(self.all_channels)\n"
    )
    new = (
        "        self.save_state(\"Удаление канала\")\n"
        "        self.all_channels = [ch for ch in self.all_channels\n"
        "                             if ch.uid not in uids]\n"
        "        self.sync_to_core()\n"
        "        self.selected_channels = []\n"
        "        self.current_channel = None\n"
        "        with self._suppress_save():\n"
        "            self.model.set_channels(self.all_channels)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: _cut_channel только current
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_cut_channel: только current_channel", "safe")
def fx_kw_cut_channel(text: str) -> Tuple[str, bool]:
    old = (
        "    def _cut_channel(self):\n"
        "        if self.current_channel:\n"
        "            self._copy_channel()\n"
        "            self._delete_channel()\n"
    )
    new = (
        "    def _cut_channel(self):\n"
        "        if not self.current_channel:\n"
        "            return\n"
        "        target = self.current_channel\n"
        "        self._copy_channel()\n"
        "        uids = {target.uid}\n"
        "        if not confirm(self, f\"Вырезать канал '{target.meta.name}'?\"):\n"
        "            return\n"
        "        self.save_state(\"Вырезание канала\")\n"
        "        self.all_channels = [ch for ch in self.all_channels\n"
        "                             if ch.uid not in uids]\n"
        "        self.sync_to_core()\n"
        "        self.selected_channels = []\n"
        "        self.current_channel = None\n"
        "        with self._suppress_save():\n"
        "            self.model.set_channels(self.all_channels)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: _on_model_data_changed шорткат
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_on_model_data_changed: шорткат для UI-ролей", "safe")
def fx_kw_data_changed_shortcut(text: str) -> Tuple[str, bool]:
    old = (
        "    def _on_model_data_changed(self, top_left, bottom_right, roles=None):\n"
        "        if self._suppress_state_save or self._loading:\n"
        "            return\n"
        "        self.save_state(\"Правка ячейки\")\n"
        "        self.sync_to_core()\n"
        "        self.update_info()\n"
    )
    new = (
        "    def _on_model_data_changed(self, top_left, bottom_right, roles=None):\n"
        "        if self._suppress_state_save or self._loading:\n"
        "            return\n"
        "        # UI-роли не меняют данные — только update_info\n"
        "        if roles is not None and all(\n"
        "                r in (Qt.ItemDataRole.DisplayRole,\n"
        "                      Qt.ItemDataRole.ForegroundRole,\n"
        "                      Qt.ItemDataRole.BackgroundRole,\n"
        "                      Qt.ItemDataRole.ToolTipRole,\n"
        "                      Qt.ItemDataRole.TextAlignmentRole)\n"
        "                for r in roles):\n"
        "            self.update_info()\n"
        "            return\n"
        "        self.save_state(\"Правка ячейки\")\n"
        "        self.sync_to_core()\n"
        "        self.update_info()\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: V6 FIX #1 — сортировка по умолчанию
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "таблица: применить сортировку по № сразу", "safe")
def fx_kw_initial_sort(text: str) -> Tuple[str, bool]:
    if "self.model.sort(0, Qt.SortOrder.AscendingOrder)" in text:
        return text, False
    old = (
        "        h.blockSignals(True)\n"
        "        h.setSortIndicator(0, Qt.SortOrder.AscendingOrder)\n"
        "        h.blockSignals(False)\n"
        "        h.sortIndicatorChanged.connect(self._on_sort_indicator_changed)\n"
    )
    new = (
        "        h.blockSignals(True)\n"
        "        h.setSortIndicator(0, Qt.SortOrder.AscendingOrder)\n"
        "        h.blockSignals(False)\n"
        "        h.sortIndicatorChanged.connect(self._on_sort_indicator_changed)\n"
        "        # Применить сортировку сразу — иначе таблица выглядит\n"
        "        # отсортированной, но модель не сортирована.\n"
        "        self.model.sort(0, Qt.SortOrder.AscendingOrder)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: V6 FIX #2 — колонка № = текущая позиция
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "data(): колонка № = текущая позиция в таблице", "safe")
def fx_kw_col0_current_row(text: str) -> Tuple[str, bool]:
    if ("        if col == 0:\n"
        "            return str(index.row() + 1)\n") in text:
        return text, False
    old = (
        "        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):\n"
        "            if col == 0:\n"
        "                if ch.original_index >= 0:\n"
        "                    return str(ch.original_index + 1)\n"
        "                return str(index.row() + 1)\n"
    )
    new = (
        "        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):\n"
        "            if col == 0:\n"
        "                # Показываем текущую позицию строки в таблице.\n"
        "                # При сортировке номера переупорядочиваются.\n"
        "                return str(index.row() + 1)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: watchdog до exec (страховка)
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "watchdog: запускать до app.exec()", "safe")
def fx_kw_watchdog_before_exec(text: str) -> Tuple[str, bool]:
    if "    # v5: watchdog ДО app.exec()" in text:
        return text, False
    old = (
        "    _install_signal_handlers(app)\n"
        "    window = MainWindow()\n"
        "    window.show()\n"
        "    rc = app.exec()\n"
        "    _install_exit_watchdog()\n"
        "    sys.exit(rc)\n"
    )
    new = (
        "    _install_signal_handlers(app)\n"
        "    # v5: watchdog ДО app.exec() — если event loop зависнет,\n"
        "    # принудительно выйти через 3 секунды после возврата из exec().\n"
        "    _install_exit_watchdog()\n"
        "    window = MainWindow()\n"
        "    window.show()\n"
        "    rc = app.exec()\n"
        "    sys.exit(rc)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: V6 FIX #3 — убрать дубль watchdog
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "main(): убрать дубль _install_exit_watchdog", "safe")
def fx_kw_remove_dup_watchdog(text: str) -> Tuple[str, bool]:
    """Удаляем _install_exit_watchdog из ksenia_window.py,
    оставляя только его вызов в main()."""
    if "# v5: watchdog вынесен в ksenia_window.py" in text:
        return text, False
    old = (
        "def _install_exit_watchdog():\n"
        "    \"\"\"v6.3: если Qt не завершил QThread-воркеры за 3 секунды\n"
        "    после app.exec(), принудительно выходим.\n"
        "\n"
        "    Сначала пробуем sys.exit(1) — даёт Python шанс отработать\n"
        "    atexit-хуки (закрытие SQLite WAL, сессий requests).\n"
        "    Если и это не сработало — os._exit(0) как последний шанс.\n"
        "    \"\"\"\n"
        "    import threading as _t\n"
        "\n"
        "    def _watchdog():\n"
        "        # Ждём 3 секунды; если главный поток ещё жив — выходим.\n"
        "        _t.Event().wait(3.0)\n"
        "        logger.warning(\n"
        "            \"[watchdog] Принудительное завершение (os._exit)\")\n"
        "        os._exit(0)\n"
        "\n"
        "    _t.Thread(target=_watchdog, daemon=True,\n"
        "              name=\"exit-watchdog\").start()\n"
    )
    new = (
        "# v5: watchdog вынесен в ksenia_window.py — единая точка входа.\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# ksenia_window.py: V6 FIX #4 — убрать дубль _install_exit_watchdog
# ---------------------------------------------------------------------------
@register("ksenia.py",
          "убрать дублирующийся watchdog", "safe")
def fx_ksenia_remove_dup_watchdog(text: str) -> Tuple[str, bool]:
    if "def _install_exit_watchdog" not in text:
        return text, False
    old = (
        "def _install_exit_watchdog():\n"
        "    import threading as _t\n"
        "\n"
        "    def _watchdog():\n"
        "        _t.Event().wait(3.0)\n"
        "        logger.warning(\"[watchdog] Принудительное завершение (sys.exit)\")\n"
        "        try:\n"
        "            sys.exit(1)\n"
        "        except SystemExit:\n"
        "            pass\n"
        "        _t.Event().wait(1.0)\n"
        "        logger.warning(\"[watchdog] os._exit(0) — жёсткий выход\")\n"
        "        os._exit(0)\n"
        "\n"
        "    _t.Thread(target=_watchdog, daemon=True,\n"
        "              name=\"exit-watchdog\").start()\n"
    )
    new = (
        "# v5: watchdog перенесён в ksenia_window.py (единая точка входа).\n"
    )
    return _patch(text, old, new)


# ===========================================================================
# ГЛАВНАЯ
# ===========================================================================

def find_root(base: Path):
    if (base / "ksenia.py").exists() and (base / "ksenia_window.py").exists():
        return base
    for p in base.iterdir():
        if p.is_dir() and (p / "ksenia_window.py").exists():
            return p
    return None


def print_fixes_list():
    print("=" * 78)
    print("  Список правок fix_ksenia.py v6")
    print("=" * 78)
    by_file: dict = {}
    for fname, desc, risk, _fn in FIXES:
        by_file.setdefault(fname, []).append((risk, desc))
    for fname in sorted(by_file):
        print(f"\n{fname}:")
        for risk, desc in by_file[fname]:
            mark = "[РИСК]" if risk == "medium" else "[safe]"
            print(f"  {mark:8} {desc}")
    print(f"\nВсего: {len(FIXES)} правок")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fix_ksenia",
        description="Ksenia — автофиксер проекта (v6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать файлы, только показать")
    parser.add_argument("--list", action="store_true",
                        help="Показать список правок и выйти")
    args = parser.parse_args()

    if args.list:
        print_fixes_list()
        return 0

    print("=" * 78)
    print("  Ksenia — fix_ksenia.py (v6)")
    if args.dry_run:
        print("  [DRY-RUN] Файлы не будут изменены")
    print("=" * 78)

    base = Path(__file__).parent.resolve()
    root = find_root(base)
    if root is None:
        print(f"[!] Не нашёл проект в {base}")
        print("    Ожидаю: ksenia.py + ksenia_window.py рядом.")
        return 2

    print(f"[*] Проект: {root}\n")

    py_files = sorted(root.glob("*.py"))
    print(f"[*] Найдено .py: {len(py_files)}\n")

    stats = {
        'init_removed': False,
        'pycache_removed': False,
        'imports_replaced': 0,
        'markers_cleaned': 0,
        'fixes_applied': 0,
        'fixes_failed': [],
        'syntax_broken': [],
    }

    # --- ФАЗА 0: __init__.py и __pycache__ -------------------------------
    init_file = root / "__init__.py"
    if init_file.exists():
        if not args.dry_run:
            init_file.unlink()
        stats['init_removed'] = True
        print("[+] Удалён __init__.py")
    pycache = root / "__pycache__"
    if pycache.exists():
        if not args.dry_run:
            shutil.rmtree(pycache, ignore_errors=True)
        stats['pycache_removed'] = True
        print("[+] Удалён __pycache__")

    # --- ФАЗА 1: относительные импорты -----------------------------------
    print("\n[*] Фаза 1: замена относительных импортов...")
    for fp in py_files:
        if fp.name == "__init__.py":
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text, n = replace_relative_imports(text)
        if n == 0:
            continue
        ok, err = is_syntax_ok(new_text)
        if not ok:
            print(f"  [!] {fp.name}: синтаксис сломан ({err}) — пропуск")
            continue
        if not args.dry_run:
            fp.write_text(new_text, encoding="utf-8")
        print(f"  [+] {fp.name}: заменено {n} импортов")
        stats['imports_replaced'] += n

    # --- ФАЗА 2: чистка маркеров -----------------------------------------
    print("\n[*] Фаза 2: чистка мусорных маркеров версий...")
    for fp in py_files:
        if not fp.exists():
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text, removed = clean_markers(text)
        if removed == 0:
            continue
        if code_lines(text) != code_lines(new_text):
            print(f"  [!] {fp.name}: чистка задела код — пропуск")
            continue
        ok, err = is_syntax_ok(new_text)
        if not ok:
            print(f"  [!] {fp.name}: синтаксис сломан ({err}) — пропуск")
            continue
        if not args.dry_run:
            fp.write_text(new_text, encoding="utf-8")
        print(f"  [+] {fp.name}: удалено {removed} строк-маркеров")
        stats['markers_cleaned'] += removed

    # --- ФАЗА 3: код-фиксы -----------------------------------------------
    print("\n[*] Фаза 3: код-фиксы...")
    by_file: dict = {}
    for fname, desc, risk, fn in FIXES:
        by_file.setdefault(fname, []).append((desc, risk, fn))

    for fname, items in by_file.items():
        fp = root / fname
        if not fp.exists():
            print(f"  [!] {fname}: файла нет")
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"  [!] {fname}: не UTF-8")
            continue
        new_text = text
        file_changed = False
        for desc, risk, fn in items:
            new_text, ok, err = apply_one_fix(new_text, fn)
            if ok:
                mark = "[РИСК]" if risk == "medium" else "[+]"
                print(f"  {mark} {fname}: {desc}")
                stats['fixes_applied'] += 1
                file_changed = True
            elif err:
                print(f"  [!] {fname}: {desc} — {err}")
                stats['fixes_failed'].append((fname, desc, err))
        if not file_changed:
            continue
        ok, err = is_syntax_ok(new_text)
        if not ok:
            print(f"  [!] {fname}: финальный синтаксис сломан — не сохраняю")
            stats['syntax_broken'].append(fname)
            continue
        if not args.dry_run:
            fp.write_text(new_text, encoding="utf-8")

    # --- ИТОГИ -----------------------------------------------------------
    print()
    print("=" * 78)
    print(f"[✓] Импортов заменено:    {stats['imports_replaced']}")
    print(f"[✓] Маркеров удалено:     {stats['markers_cleaned']}")
    print(f"[✓] Код-фиксов применено: {stats['fixes_applied']}")
    if stats['init_removed']:
        print("[✓] __init__.py удалён")
    if stats['pycache_removed']:
        print("[✓] __pycache__ удалён")
    if stats['fixes_failed']:
        print(f"[!] Фиксов с ошибками:    {len(stats['fixes_failed'])}")
        for fname, desc, err in stats['fixes_failed'][:10]:
            print(f"    - {fname}: {desc} — {err}")

    # --- ПРОВЕРКИ --------------------------------------------------------
    print("\n[*] Проверка синтаксиса всех .py...")
    bad = []
    for f in sorted(root.glob("*.py")):
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append((f.name, e.lineno, e.msg))
    if bad:
        for name, ln, msg in bad:
            print(f"  [!] {name}:{ln}: {msg}")
    else:
        print("  [✓] Все файлы синтаксически корректны")

    print("\n[*] Проверка ключевых маркеров:")
    checks = [
        ("constants.py", 'APP_VERSION = "0.1"',
         "версия не сброшена на 0.1"),
        ("utils.py", "'Ð°'",
         "MOJIBAKE_TRIGGERS не расширены"),
        ("models.py", "_uid_next: int = 1",
         "_reserve_uid на itertools.count"),
        ("models.py", "self._invalidate_caches()",
         "restore_from_dict без сброса кэша"),
        ("models.py", "if self.group and self.group != DEFAULT_GROUP:",
         "update_extinf пишет DEFAULT_GROUP"),
        ("blacklists.py", "(it.get('name') or '').lower()",
         "add_channel не None-safe"),
        ("workers.py", "fresh.get((ch.meta.name.lower(), url))",
         "fresh.get без .lower()"),
        ("ksenia_window.py", "self.modified = True",
         "UA-правила не помечают modified"),
        ("ksenia_window.py", "self.selected_channels = []\n        self.current_channel = None",
         "_delete_channel не очищает selected"),
        ("ksenia_window.py", "self.model.sort(0, Qt.SortOrder.AscendingOrder)",
         "сортировка при старте не добавлена"),
        ("ksenia_window.py", "return str(index.row() + 1)",
         "колонка № не показывает текущую позицию"),
    ]
    all_ok = True
    for fname, marker, consequence in checks:
        fp = root / fname
        if not fp.exists():
            print(f"  [!] {fname}: файла нет")
            all_ok = False
            continue
        text = fp.read_text(encoding="utf-8")
        if marker in text:
            print(f"  [✓] {fname}: {marker[:60]}")
        else:
            print(f"  [!] {fname}: нет '{marker[:60]}' — {consequence}")
            all_ok = False

    print()
    print("=" * 78)
    if all_ok and not bad:
        print("[✓] Готово. Все проверки пройдены.")
    else:
        print("[!] Готово, но есть предупреждения — см. выше.")
    if args.dry_run:
        print("[DRY-RUN] Ничего не записано.")
    else:
        print(f"    Запустите:  cd {root}")
        print("                py .\\ksenia.py")
        print("    Откат:      git checkout -- .")
    return 0


if __name__ == "__main__":
    sys.exit(main())
