#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_ksenia.py — единый ремонт проекта Ksenia (v4, 16 правок).

Запуск из корня (где ksenia.py + ksenia_window.py):
    py fix_ksenia.py             # применить всё
    py fix_ksenia.py --dry-run   # только показать, что будет изменено
    py fix_ksenia.py --list      # показать список правок

Идемпотентен. Каждая правка проверяется на синтаксис через ast.parse.
При поломке — правка НЕ применяется, в отчёт идёт ошибка.
Бэкапов не делает — используй git для отката.
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Tuple


# ===========================================================================
# УТИЛИТЫ
# ===========================================================================

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
    """Число непустых не-комментарных строк."""
    return sum(1 for ln in s.split('\n')
               if ln.strip() and not ln.strip().startswith('#'))


def is_syntax_ok(text: str) -> Tuple[bool, str]:
    """Проверяет синтаксис. Возвращает (ok, сообщение)."""
    try:
        ast.parse(text)
        return True, ""
    except SyntaxError as e:
        return False, f"{e.lineno}: {e.msg}"


def apply_one_fix(text: str, fn: Callable
                  ) -> Tuple[str, bool, str]:
    """Применяет один фикс и проверяет синтаксис результата.

    Возвращает (новый_текст, применён, ошибка). Если синтаксис
    сломан — возвращает (старый_текст, False, ошибка).
    """
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
# ФАЗА 1 — ЗАМЕНА ОТНОСИТЕЛЬНЫХ ИМПОРТОВ
# ===========================================================================

_RE_REL_IMPORT_MODULE = re.compile(r'\bfrom \.(\w+)\.(\w+) import')
_RE_REL_IMPORT = re.compile(r'\bfrom \.(\w+) import')


def replace_relative_imports(text: str) -> Tuple[str, int]:
    """'from X.Y import' → 'from Y import'; 'from X import' → 'from X import'."""
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

        if _RE_ONE_LINE_FIX.match(line):
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
# РЕЕСТР КОД-ФИКСОВ (16 правок)
# ===========================================================================

# Формат: (имя_файла, описание, риск, функция)
# риск: "safe" — безопасно, "medium" — средний риск, требует теста
FIXES: List[Tuple[str, str, str, Callable[[str], Tuple[str, bool]]]] = []


def register(fname: str, desc: str, risk: str = "safe"):
    def deco(fn):
        FIXES.append((fname, desc, risk, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 1. constants.py — APP_VERSION
# ---------------------------------------------------------------------------
@register("constants.py", 'APP_VERSION "2026" → "0.1"', "safe")
def fx_const_version(text: str) -> Tuple[str, bool]:
    if 'APP_VERSION = "0.1"' in text:
        return text, False
    return _patch(text, 'APP_VERSION = "2026"', 'APP_VERSION = "0.1"')


# ---------------------------------------------------------------------------
# 2. constants.py — убрать 'text/plain' из stream-типов
# ---------------------------------------------------------------------------
@register("constants.py",
          "убрать 'text/plain' из VLC_STREAM_CONTENT_TYPES", "safe")
def fx_const_text_plain(text: str) -> Tuple[str, bool]:
    return _patch(
        text,
        "    'application/x-mpegts',\n    'text/plain',\n)",
        "    'application/x-mpegts',\n)",
    )


# ---------------------------------------------------------------------------
# 3. paths.py — logging guards
# ---------------------------------------------------------------------------
@register("paths.py", "logging: guard-переменные", "safe")
def fx_paths_logging(text: str) -> Tuple[str, bool]:
    if "_logging_initialized = False" in text:
        return text, False
    anchor = "def _setup_logging():"
    if anchor not in text:
        return text, False
    return _patch(
        text, anchor,
        "_logging_initialized = False\n"
        "_logging_lock = threading.Lock()\n\n\n" + anchor,
    )


# ---------------------------------------------------------------------------
# 4. paths.py — parse_datetime: суффикс 'Z'
# ---------------------------------------------------------------------------
@register("paths.py", "parse_datetime: суффикс 'Z'", "safe")
def fx_paths_parse_z(text: str) -> Tuple[str, bool]:
    if 'iso = s[:-1] + "+00:00" if s.endswith("Z") else s' in text:
        return text, False
    old = (
        'def parse_datetime(s, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:\n'
        '    if not s:\n'
        '        return None\n'
        '    for f in (fmt, None):\n'
        '        try:\n'
        '            return datetime.strptime(s, f) if f else datetime.fromisoformat(s)\n'
        '        except (ValueError, TypeError):\n'
        '            continue\n'
        '    return None'
    )
    new = (
        'def parse_datetime(s, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:\n'
        '    if not s:\n'
        '        return None\n'
        '    if isinstance(s, str):\n'
        '        iso = s[:-1] + "+00:00" if s.endswith("Z") else s\n'
        '    else:\n'
        '        return None\n'
        '    for f in (fmt, None):\n'
        '        try:\n'
        '            return datetime.strptime(s, f) if f else datetime.fromisoformat(iso)\n'
        '        except (ValueError, TypeError):\n'
        '            continue\n'
        '    return None'
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 5. utils.py — fix_encoding: добавить типичные маркеры
# ---------------------------------------------------------------------------
@register("utils.py",
          "fix_encoding: расширить MOJIBAKE_TRIGGERS", "safe")
def fx_utils_mojibake_triggers(text: str) -> Tuple[str, bool]:
    old = "MOJIBAKE_TRIGGERS = ('–°', '–∞', '–µ', '–∏', '—Å', '—Ä', '√©', '√®')"
    if "MOJIBAKE_TRIGGERS = (" not in text:
        return text, False
    if "'ÐŸ'" in text or "'Ð°'" in text:
        return text, False  # уже расширен
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
# 6. utils.py — URLUtils._validate_url → _url_error (алиас)
# ---------------------------------------------------------------------------
@register("utils.py", "URLUtils._validate_url → _url_error (алиас)", "safe")
def fx_utils_validate(text: str) -> Tuple[str, bool]:
    if "_url_error" in text:
        return text, False
    old = (
        "    @staticmethod\n"
        "    def _validate_url(url: str) -> Optional[str]:\n"
    )
    new = (
        "    @staticmethod\n"
        "    def _url_error(url: str) -> Optional[str]:\n"
    )
    text, c1 = _patch(text, old, new)
    old_alias = (
        "        if p.scheme not in ('http', 'https'):\n"
        "            return f\"Неподдерживаемый протокол: {p.scheme}\"\n"
        "        return None\n"
    )
    new_alias = (
        "        if p.scheme not in ('http', 'https'):\n"
        "            return f\"Неподдерживаемый протокол: {p.scheme}\"\n"
        "        return None\n"
        "\n"
        "    _validate_url = _url_error\n"
    )
    text, c2 = _patch(text, old_alias, new_alias)
    return text, (c1 or c2)


# ---------------------------------------------------------------------------
# 7. utils.py — _read_first_chunk: убрать wait
# ---------------------------------------------------------------------------
@register("utils.py", "_read_first_chunk: убрать wait", "safe")
def fx_utils_read_first_chunk(text: str) -> Tuple[str, bool]:
    return _patch(
        text,
        "    def _read_first_chunk(response, chunk_size: int = 4096,\n"
        "                          wait: float = 0.15) -> bool:\n"
        "        # v6.0: НЕ ждём 1.5 сек — читаем один чанк сразу.\n"
        "        # Для IPTV-потоков достаточно факта «данные пошли».\n",
        "    def _read_first_chunk(response, chunk_size: int = 4096) -> bool:\n",
    )


# ---------------------------------------------------------------------------
# 8. models.py — _reserve_uid race condition
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
# 9. models.py — restore_from_dict: сброс кэшей
# ---------------------------------------------------------------------------
@register("models.py",
          "restore_from_dict: сброс _cached_hash/norm", "safe")
def fx_models_restore_invalidate(text: str) -> Tuple[str, bool]:
    if "self._invalidate_caches()" in text and \
       text.count("self._invalidate_caches()") >= 2:
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
# 10. models.py — update_extinf: не писать DEFAULT_GROUP
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
# 11. models.py — _build_attr_map при импорте
# ---------------------------------------------------------------------------
@register("models.py", "вызвать _build_attr_map() при импорте", "safe")
def fx_models_attr_map(text: str) -> Tuple[str, bool]:
    if "ChannelData._build_attr_map()" in text and \
       text.count("ChannelData._build_attr_map()") >= 1:
        # Уже есть (мог быть добавлен предыдущим фиксером)
        return text, False
    anchor = "\n\nclass EPGEntry:"
    if anchor not in text:
        return text, False
    return _patch(text, anchor,
                  "\n\nChannelData._build_attr_map()\n\n\nclass EPGEntry:")


# ---------------------------------------------------------------------------
# 12. config.py — deepcopy(DEFAULT)
# ---------------------------------------------------------------------------
@register("config.py", "deepcopy(DEFAULT)", "safe")
def fx_config_deepcopy(text: str) -> Tuple[str, bool]:
    if "copy.deepcopy(self.DEFAULT)" in text:
        return text, False
    changed = False
    if "import copy" not in text:
        text, ok = _patch(text, "import os\nimport json\n",
                          "import os\nimport json\nimport copy\n")
        changed = changed or ok
    text, ok = _patch(
        text,
        "        self.config: Dict[str, Any] = dict(self.DEFAULT)",
        "        self.config: Dict[str, Any] = copy.deepcopy(self.DEFAULT)",
    )
    return text, (changed or ok)


# ---------------------------------------------------------------------------
# 13. workers.py — EPG_SOURCE_TIMEOUT_SEC в импорт
# ---------------------------------------------------------------------------
@register("workers.py", "EPG_SOURCE_TIMEOUT_SEC в импорт", "safe")
def fx_workers_epg_timeout(text: str) -> Tuple[str, bool]:
    head = text[:text.find("class ")] if "class " in text else text[:2000]
    if "EPG_SOURCE_TIMEOUT_SEC" in head:
        return text, False
    return _patch(
        text,
        "    EPG_FUZZY_MIN_GAP_DEFAULT, STREAMING_PROTOCOLS)\n",
        "    EPG_FUZZY_MIN_GAP_DEFAULT, STREAMING_PROTOCOLS,\n"
        "    EPG_SOURCE_TIMEOUT_SEC)\n",
    )


# ---------------------------------------------------------------------------
# 14. workers.py — SourceUrlCheckWorker: fresh.get(...lower())
# ---------------------------------------------------------------------------
@register("workers.py",
          "SourceUrlCheckWorker: fresh.get с .lower()", "safe")
def fx_workers_fresh_lower(text: str) -> Tuple[str, bool]:
    old = (
        "                hit = fresh.get((ch.meta.name, url))\n"
        "                if hit and (now - hit['last_check']) < self.trust_sec:\n"
    )
    new = (
        "                hit = fresh.get((ch.meta.name.lower(), url))\n"
        "                if hit and (now - hit['last_check']) < self.trust_sec:\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 15. epg.py — _fuzzy_cache сбрасывается при load_from_xmltv
# ---------------------------------------------------------------------------
@register("epg.py",
          "load_from_xmltv: сброс _fuzzy_cache", "safe")
def fx_epg_fuzzy_cache_reset(text: str) -> Tuple[str, bool]:
    if "with self._fuzzy_cache_lock:\n                self._fuzzy_cache.clear()" in text:
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
        "        # Сбросить fuzzy-кэш: _channel_info обновилось\n"
        "        with self._fuzzy_cache_lock:\n"
        "            self._fuzzy_cache.clear()\n"
        "        return count\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 16. epg.py — find_channel_info: нормализовать norm_key
# ---------------------------------------------------------------------------
@register("epg.py",
          "find_channel_info: нормализовать norm_key [РИСК]", "medium")
def fx_epg_norm_key(text: str) -> Tuple[str, bool]:
    old = (
        "            target_tokens = self._tokens_for(target)\n"
        "            if target_tokens:\n"
        "                cid_set: Set[str] = set()\n"
        "                with self._channel_info_lock:\n"
        "                    for t in target_tokens:\n"
        "                        cid_set |= self._channel_info_by_token.get(t, set())\n"
        "                    snapshot = [(c, (self._channel_info[c].display_name or c))\n"
        "                                for c in cid_set if c in self._channel_info]\n"
    )
    new = (
        "            target_tokens = self._tokens_for(target)\n"
        "            if target_tokens:\n"
        "                cid_set: Set[str] = set()\n"
        "                with self._channel_info_lock:\n"
        "                    for t in target_tokens:\n"
        "                        cid_set |= self._channel_info_by_token.get(t, set())\n"
        "                    snapshot = [\n"
        "                        (c, ChannelNameNormalizer.normalize(\n"
        "                            self._channel_info[c].display_name or c))\n"
        "                        for c in cid_set if c in self._channel_info\n"
        "                    ]\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 17. blacklists.py — `(bi.get('tvg_id') or '').lower()` (2 места)
# ---------------------------------------------------------------------------
@register("blacklists.py",
          "None-safe .lower() для tvg_id", "safe")
def fx_bl_none_safe(text: str) -> Tuple[str, bool]:
    changed = False
    old1 = (
        "        for it in self._store._data:\n"
        "            if (it.get('name', '').lower() == name.lower() and\n"
        "                    it.get('tvg_id', '').lower() == tvg_id.lower()):\n"
    )
    new1 = (
        "        for it in self._store._data:\n"
        "            if ((it.get('name') or '').lower() == (name or '').lower() and\n"
        "                    (it.get('tvg_id') or '').lower() == (tvg_id or '').lower()):\n"
    )
    text, ok = _patch(text, old1, new1)
    changed = changed or ok

    old2 = (
        "            for i, it in enumerate(self._store._data):\n"
        "                if (it.get('name', '').lower() == name.lower() and\n"
        "                        it.get('tvg_id', '').lower() == tvg_id.lower()):\n"
    )
    new2 = (
        "            for i, it in enumerate(self._store._data):\n"
        "                if ((it.get('name') or '').lower() == (name or '').lower() and\n"
        "                        (it.get('tvg_id') or '').lower() == (tvg_id or '').lower()):\n"
    )
    text, ok = _patch(text, old2, new2)
    changed = changed or ok

    old3 = (
        "        for bi in bl:\n"
        "            n = bi.get('name', '').strip().lower()\n"
        "            t = bi.get('tvg_id', '').strip().lower()\n"
    )
    new3 = (
        "        for bi in bl:\n"
        "            n = (bi.get('name') or '').strip().lower()\n"
        "            t = (bi.get('tvg_id') or '').strip().lower()\n"
    )
    text, ok = _patch(text, old3, new3)
    changed = changed or ok

    old4 = (
        "            for bi in bl:\n"
        "                n = bi.get('name', '').strip().lower()\n"
        "                t = bi.get('tvg_id', '').strip().lower()\n"
    )
    new4 = (
        "            for bi in bl:\n"
        "                n = (bi.get('name') or '').strip().lower()\n"
        "                t = (bi.get('tvg_id') or '').strip().lower()\n"
    )
    text, ok = _patch(text, old4, new4)
    changed = changed or ok

    return text, changed


# ---------------------------------------------------------------------------
# 18. blacklists.py — DomainBlacklistManager._persist_locked: откат
# ---------------------------------------------------------------------------
@register("blacklists.py",
          "DomainBlacklist._persist_locked: откат при ошибке", "safe")
def fx_bl_persist_rollback(text: str) -> Tuple[str, bool]:
    old = (
        "    def _persist_locked(self) -> bool:\n"
        "        snapshot = [r.to_dict() for r in self._rules]\n"
        "        self._store._data = snapshot\n"
        "        ok = self._store.save()\n"
        "        if not ok:\n"
        "            # Пытаемся восстановить из уже загруженного _store._data\n"
        "            # (там лежит последняя успешно сохранённая версия).\n"
        "            logger.error(\"DomainBlacklist persist failed\")\n"
        "        return ok\n"
    )
    new = (
        "    def _persist_locked(self) -> bool:\n"
        "        old_data = self._store._data\n"
        "        self._store._data = [r.to_dict() for r in self._rules]\n"
        "        ok = self._store.save()\n"
        "        if not ok:\n"
        "            self._store._data = old_data\n"
        "            logger.error(\n"
        "                \"DomainBlacklist persist failed, rolled back\")\n"
        "        return ok\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 19. dialogs.py — shiboken6
# ---------------------------------------------------------------------------
@register("dialogs.py", "_HAS_SHIBOKEN + import shiboken6", "safe")
def fx_dialogs_shiboken(text: str) -> Tuple[str, bool]:
    if "_HAS_SHIBOKEN" in text and "import shiboken6" in text:
        return text, False
    anchor = "from workers import SourcesRefreshWorker\n"
    if anchor not in text:
        return text, False
    insert = (
        "\ntry:\n"
        "    import shiboken6\n"
        "    _HAS_SHIBOKEN = True\n"
        "except ImportError:\n"
        "    shiboken6 = None\n"
        "    _HAS_SHIBOKEN = False\n"
    )
    return _patch(text, anchor, anchor + insert)


# ---------------------------------------------------------------------------
# 20. dialogs.py — QWidget, QTabWidget
# ---------------------------------------------------------------------------
@register("dialogs.py", "QWidget, QTabWidget в QtWidgets", "safe")
def fx_dialogs_widgets(text: str) -> Tuple[str, bool]:
    if "QWidget, QTabWidget)" in text:
        return text, False
    return _patch(
        text,
        "    QStyle, QMenu, QSlider, QApplication)\n",
        "    QStyle, QMenu, QSlider, QApplication,\n"
        "    QWidget, QTabWidget)\n",
    )


# ---------------------------------------------------------------------------
# 21. dialogs.py — URL_CHECK_MAX_WORKERS
# ---------------------------------------------------------------------------
@register("dialogs.py", "URL_CHECK_MAX_WORKERS в импорт", "safe")
def fx_dialogs_constants(text: str) -> Tuple[str, bool]:
    if "URL_CHECK_MAX_WORKERS, VLC_DEFAULT_CHECK_TIMEOUT" in text:
        return text, False
    return _patch(
        text,
        "    REPLACEMENT_MAX_WORKERS_DEFAULT)\n",
        "    REPLACEMENT_MAX_WORKERS_DEFAULT,\n"
        "    URL_CHECK_MAX_WORKERS, VLC_DEFAULT_CHECK_TIMEOUT)\n",
    )


# ---------------------------------------------------------------------------
# 22. dialogs.py — import csv
# ---------------------------------------------------------------------------
@register("dialogs.py", "import csv", "safe")
def fx_dialogs_csv(text: str) -> Tuple[str, bool]:
    if "\nimport csv\n" in text:
        return text, False
    return _patch(text, "import json\nimport time\n",
                  "import json\nimport csv\nimport time\n")


# ---------------------------------------------------------------------------
# 23. dialogs.py — 'ApplicationCore' в кавычки
# ---------------------------------------------------------------------------
@register("dialogs.py", "обернуть ApplicationCore в кавычки", "safe")
def fx_dialogs_annot_core(text: str) -> Tuple[str, bool]:
    return _patch_all(
        text,
        "def __init__(self, core: ApplicationCore,",
        "def __init__(self, core: 'ApplicationCore',",
    )


# ---------------------------------------------------------------------------
# 24. dialogs.py — 'PlaylistHeaderManager' в кавычки
# ---------------------------------------------------------------------------
@register("dialogs.py", "обернуть PlaylistHeaderManager в кавычки", "safe")
def fx_dialogs_annot_hm(text: str) -> Tuple[str, bool]:
    return _patch(
        text,
        "def __init__(self, header_manager: PlaylistHeaderManager, parent=None):",
        "def __init__(self, header_manager: 'PlaylistHeaderManager', parent=None):",
    )


# ---------------------------------------------------------------------------
# 25. dialogs.py — убрать DomainUserAgentRule из models
# ---------------------------------------------------------------------------
@register("dialogs.py", "убрать дубль DomainUserAgentRule из models", "safe")
def fx_dialogs_rule_dup(text: str) -> Tuple[str, bool]:
    return _patch(
        text,
        "from models import ChannelData, DomainUserAgentRule\n",
        "from models import ChannelData\n",
    )


# ---------------------------------------------------------------------------
# 26. dialogs.py — _NumericItem импорт
# ---------------------------------------------------------------------------
@register("dialogs.py", "_NumericItem: убрать импорт из utils", "safe")
def fx_dialogs_numeric_import(text: str) -> Tuple[str, bool]:
    return _patch(
        text,
        "from utils import URLUtils, _NumericItem\n",
        "from utils import URLUtils\n",
    )


# ---------------------------------------------------------------------------
# 27. dialogs.py — _NumericItem локально
# ---------------------------------------------------------------------------
@register("dialogs.py", "_NumericItem: вставить класс локально", "safe")
def fx_dialogs_numeric_class(text: str) -> Tuple[str, bool]:
    if "class _NumericItem(QTableWidgetItem)" in text:
        return text, False
    anchor = "class BaseDialog(QDialog):"
    if anchor not in text:
        return text, False
    insert = (
        "class _NumericItem(QTableWidgetItem):\n"
        "    \"\"\"QTableWidgetItem с числовым сравнением для сортировки.\"\"\"\n"
        "    def __init__(self, value: int):\n"
        "        super().__init__(str(value))\n"
        "        self._value = int(value)\n"
        "        self.setTextAlignment(Qt.AlignmentFlag.AlignCenter)\n"
        "\n"
        "    def __lt__(self, other):\n"
        "        if isinstance(other, _NumericItem):\n"
        "            return self._value < other._value\n"
        "        try:\n"
        "            other_val = int(other.text())\n"
        "            return self._value < other_val\n"
        "        except (ValueError, AttributeError):\n"
        "            return super().__lt__(other)\n"
        "\n"
        "\n"
    )
    return _patch(text, anchor, insert + anchor)


# ---------------------------------------------------------------------------
# 28. player.py — try/except import vlc
# ---------------------------------------------------------------------------
@register("player.py", "try/except import vlc", "safe")
def fx_player_vlc(text: str) -> Tuple[str, bool]:
    if "_HAS_VLC_MODULE" in text and "import vlc" in text:
        return text, False
    anchor = "from dialogs import BaseDialog, _is_qobject_valid\n"
    if anchor not in text:
        return text, False
    insert = (
        "\ntry:\n"
        "    import vlc\n"
        "    _HAS_VLC_MODULE = True\n"
        "    _VLC_IMPORT_ERROR = \"\"\n"
        "except ImportError as e:\n"
        "    vlc = None\n"
        "    _HAS_VLC_MODULE = False\n"
        "    _VLC_IMPORT_ERROR = str(e)\n"
        "except Exception as e:\n"
        "    vlc = None\n"
        "    _HAS_VLC_MODULE = False\n"
        "    _VLC_IMPORT_ERROR = str(e)\n"
    )
    return _patch(text, anchor, anchor + insert)


# ---------------------------------------------------------------------------
# 29. player.py — end_reached: убрать QTimer.singleShot
# ---------------------------------------------------------------------------
@register("player.py",
          "end_reached: убрать QTimer.singleShot [РИСК]", "medium")
def fx_player_end_reached(text: str) -> Tuple[str, bool]:
    old = (
        "            events.event_attach(\n"
        "                vlc.EventType.MediaPlayerEndReached,\n"
        "                lambda *a: QTimer.singleShot(0, self.end_reached.emit))\n"
    )
    new = (
        "            events.event_attach(\n"
        "                vlc.EventType.MediaPlayerEndReached,\n"
        "                lambda *a: self.end_reached.emit())\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 30. player.py — warn_box в импорт paths
# ---------------------------------------------------------------------------
@register("player.py", "warn_box в импорт paths", "safe")
def fx_player_warn(text: str) -> Tuple[str, bool]:
    if "warn_box" in text and "logger, error_box, warn_box" in text:
        return text, False
    return _patch(
        text,
        "from paths import logger, error_box, info_box, save_file_dialog\n",
        "from paths import (logger, error_box, warn_box, info_box,\n"
        "    save_file_dialog)\n",
    )


# ---------------------------------------------------------------------------
# 31. ksenia_window.py — _META_CHECKS
# ---------------------------------------------------------------------------
@register("ksenia_window.py", "_META_CHECKS", "safe")
def fx_kw_meta(text: str) -> Tuple[str, bool]:
    if "_META_CHECKS = (" in text:
        return text, False
    anchor = "class PlaylistTab(QWidget):"
    if anchor not in text:
        return text, False
    insert = (
        "_META_CHECKS = (\n"
        "    ('tvg_id',      lambda ch: bool(ch.meta.tvg_id)),\n"
        "    ('tvg_logo',    lambda ch: bool(ch.meta.tvg_logo)),\n"
        "    ('tvg_name',    lambda ch: bool(ch.meta.tvg_name)),\n"
        "    ('group_title', lambda ch: bool(ch.meta.group and\n"
        "                                    ch.meta.group != DEFAULT_GROUP)),\n"
        "    ('user_agent',  lambda ch: bool(ch.link.user_agent)),\n"
        ")\n\n\n"
    )
    return _patch(text, anchor, insert + anchor)


# ---------------------------------------------------------------------------
# 32. ksenia_window.py — _HAS_VLC_MODULE
# ---------------------------------------------------------------------------
@register("ksenia_window.py", "_HAS_VLC_MODULE в ksenia_window", "safe")
def fx_kw_vlc_globals(text: str) -> Tuple[str, bool]:
    if "_HAS_VLC_MODULE" in text[:4000]:
        return text, False
    m = re.search(r'\n\nclass ApplicationCore', text)
    if not m:
        return text, False
    insert = (
        "\n\n# Флаги VLC (могут отсутствовать на системе)\n"
        "try:\n"
        "    import vlc\n"
        "    _HAS_VLC_MODULE = True\n"
        "    _VLC_IMPORT_ERROR = \"\"\n"
        "except ImportError as _e:\n"
        "    vlc = None\n"
        "    _HAS_VLC_MODULE = False\n"
        "    _VLC_IMPORT_ERROR = str(_e)\n"
        "except Exception as _e:\n"
        "    vlc = None\n"
        "    _HAS_VLC_MODULE = False\n"
        "    _VLC_IMPORT_ERROR = str(_e)\n"
    )
    return _patch(text, "\n\nclass ApplicationCore",
                  insert + "\n\nclass ApplicationCore")


# ---------------------------------------------------------------------------
# 33. ksenia_window.py — _is_gui_thread: is → ==
# ---------------------------------------------------------------------------
@register("dialogs.py",
          "_is_gui_thread: is → == [РИСК]", "medium")
def fx_dialogs_is_gui_thread(text: str) -> Tuple[str, bool]:
    old = (
        "    try:\n"
        "        return QThread.currentThread() is app.thread()\n"
        "    except Exception:\n"
        "        return True\n"
    )
    new = (
        "    try:\n"
        "        return QThread.currentThread() == app.thread()\n"
        "    except Exception:\n"
        "        return True\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 34. ksenia_window.py — watchdog: os._exit без sys.exit
# ---------------------------------------------------------------------------
@register("ksenia_window.py", "watchdog: os._exit без sys.exit", "safe")
def fx_kw_watchdog(text: str) -> Tuple[str, bool]:
    old = (
        '        logger.warning("[watchdog] Принудительное завершение (sys.exit)")\n'
        '        try:\n'
        '            sys.exit(1)\n'
        '        except SystemExit:\n'
        '            pass\n'
        '        # Если sys.exit не помог — даём ещё секунду и жёсткий exit.\n'
        '        _t.Event().wait(1.0)\n'
        '        logger.warning("[watchdog] os._exit(0) — жёсткий выход")\n'
        '        os._exit(0)\n'
    )
    new = (
        '        logger.warning(\n'
        '            "[watchdog] Принудительное завершение (os._exit)")\n'
        '        os._exit(0)\n'
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 35. ksenia_window.py — _delete_channel: очистить selected
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_delete_channel: очистить selected_channels/current", "safe")
def fx_kw_delete_clear_selected(text: str) -> Tuple[str, bool]:
    if "self.selected_channels = []\n        self.current_channel = None" in text \
       and text.count("self.selected_channels = []\n        self.current_channel = None") >= 2:
        return text, False
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
# 36. ksenia_window.py — _cut_channel: только current
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_cut_channel: удалять только current_channel", "safe")
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
        "        # Удаляем ТОЛЬКО текущий канал, не selected_channels\n"
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
# 37. ksenia_window.py — apply_domain_user_agent → modified
# ---------------------------------------------------------------------------
@register("ksenia_window.py",
          "_load_file: apply_domain_user_agent → modified=True", "safe")
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
# 38. ksenia_window.py — config.py is_filtered_domain [РИСК]
# ---------------------------------------------------------------------------
@register("config.py",
          "is_filtered_domain: проверка host [РИСК]", "medium")
def fx_config_filtered_domain(text: str) -> Tuple[str, bool]:
    old = (
        "    def is_filtered_domain(self, url: str) -> bool:\n"
        "        if not url:\n"
        "            return False\n"
        "        u = url.lower()\n"
        "        return any(d.lower() in u for d in self.temporary_domains) or \\\n"
        "               any(d.lower() in u for d in self.unsafe_domains)\n"
    )
    new = (
        "    def is_filtered_domain(self, url: str) -> bool:\n"
        "        if not url:\n"
        "            return False\n"
        "        try:\n"
        "            from utils import URLUtils\n"
        "            host = URLUtils.extract_host(url) or ''\n"
        "        except Exception:\n"
        "            host = ''\n"
        "        if host:\n"
        "            for d in list(self.temporary_domains) + list(self.unsafe_domains):\n"
        "                dn = (d or '').strip().lower().strip('.')\n"
        "                if not dn:\n"
        "                    continue\n"
        "                if host == dn or host.endswith('.' + dn):\n"
        "                    return True\n"
        "            return False\n"
        "        # Fallback: если host не извлекли — старая логика\n"
        "        u = url.lower()\n"
        "        return any((d or '').lower() in u\n"
        "                   for d in self.temporary_domains) or \\\n"
        "               any((d or '').lower() in u\n"
        "                   for d in self.unsafe_domains)\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 39. ksenia.py — плоские импорты
# ---------------------------------------------------------------------------
@register("ksenia.py",
          "импорт ksenia_window.main (плоская структура)", "safe")
def fx_ksenia_flat(text: str) -> Tuple[str, bool]:
    old = (
        "if __package__ is None or __package__ == '':\n"
        "    sys.path.insert(\n"
        "        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "    from ksenia.ksenia_window import (\n"
        "        parse_cli_args, run_cli, MainWindow,\n"
        "    )\n"
        "    from ksenia.constants import APP_VERSION\n"
        "    from ksenia.paths import logger\n"
        "else:\n"
        "    from ksenia_window import parse_cli_args, run_cli, MainWindow\n"
        "    from constants import APP_VERSION\n"
        "    from paths import logger\n"
    )
    new = (
        "from ksenia_window import parse_cli_args, run_cli, MainWindow\n"
        "from constants import APP_VERSION\n"
        "from paths import logger\n"
    )
    return _patch(text, old, new)


# ---------------------------------------------------------------------------
# 40. undo.py — save_state: флаг _initialized
# ---------------------------------------------------------------------------
@register("undo.py",
          "save_state: _initialized вместо пустого снапшота", "safe")
def fx_undo_initialized(text: str) -> Tuple[str, bool]:
    if "_initialized: bool = False" in text:
        return text, False
    # 1. Добавить флаг в __init__
    old_init = (
        "        self._last_order: List[int] = []\n"
        "        self._lock = threading.RLock()\n"
    )
    new_init = (
        "        self._last_order: List[int] = []\n"
        "        self._initialized: bool = False\n"
        "        self._lock = threading.RLock()\n"
    )
    text, c1 = _patch(text, old_init, new_init)
    if not c1:
        return text, False

    # 2. Заменить условие первого вызова
    old_cond = (
        "            if not self._last_snapshot and not self._last_data:\n"
        "                self._last_snapshot = new_snap\n"
        "                self._last_data = {ch.uid: ch.to_dict() for ch in channels}\n"
        "                self._last_order = new_order\n"
        "                return\n"
    )
    new_cond = (
        "            if not self._initialized:\n"
        "                self._last_snapshot = new_snap\n"
        "                self._last_data = {ch.uid: ch.to_dict() for ch in channels}\n"
        "                self._last_order = new_order\n"
        "                self._initialized = True\n"
        "                return\n"
    )
    text, c2 = _patch(text, old_cond, new_cond)
    if not c2:
        return text, False

    # 3. Сбросить флаг в reset
    old_reset = (
        "    def reset(self, channels: List[ChannelData]):\n"
        "        with self._lock:\n"
        "            snap, order = self._current_snapshot(channels)\n"
        "            self._last_snapshot = snap\n"
        "            self._last_data = {ch.uid: ch.to_dict() for ch in channels}\n"
        "            self._last_order = order\n"
        "            self._undo_stack.clear()\n"
        "            self._redo_stack.clear()\n"
    )
    new_reset = (
        "    def reset(self, channels: List[ChannelData]):\n"
        "        with self._lock:\n"
        "            snap, order = self._current_snapshot(channels)\n"
        "            self._last_snapshot = snap\n"
        "            self._last_data = {ch.uid: ch.to_dict() for ch in channels}\n"
        "            self._last_order = order\n"
        "            self._initialized = True\n"
        "            self._undo_stack.clear()\n"
        "            self._redo_stack.clear()\n"
    )
    text, c3 = _patch(text, old_reset, new_reset)
    return text, (c1 or c2 or c3)


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
    print("  Список правок fix_ksenia.py")
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
        description="Ksenia — автофиксер проекта")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать файлы, только показать")
    parser.add_argument("--list", action="store_true",
                        help="Показать список правок и выйти")
    args = parser.parse_args()

    if args.list:
        print_fixes_list()
        return 0

    print("=" * 78)
    print("  Ksenia — fix_ksenia.py (v4, 16+ правок)")
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

    print("\n[*] Проверка относительных импортов...")
    left = []
    for f in sorted(root.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in re.finditer(r'\bfrom \.\w', text):
            left.append((f.name, m.start()))
    if left:
        print(f"  [!] Осталось относительных импортов: {len(left)}")
        for name, _ in left[:10]:
            print(f"      - {name}")
    else:
        print("  [✓] Относительных импортов не осталось")

    print("\n[*] Проверка ключевых символов:")
    checks = [
        ("dialogs.py", "class _NumericItem(QTableWidgetItem)",
         "dialogs.py не сможет использовать _NumericItem"),
        ("models.py", "ChannelData._build_attr_map()",
         "_ATTR_MAP останется пустым"),
        ("dialogs.py", "_HAS_SHIBOKEN",
         "NameError в _is_qobject_valid"),
        ("player.py", "_HAS_VLC_MODULE",
         "NameError в _init_vlc"),
        ("ksenia_window.py", "_HAS_VLC_MODULE",
         "NameError в MainWindow"),
        ("ksenia.py", "from ksenia_window import",
         "ksenia.py не импортирует точку входа"),
        ("utils.py", "'Ð°'",
         "MOJIBAKE_TRIGGERS не расширены"),
        ("models.py", "_uid_next: int = 1",
         "_reserve_uid остался на itertools.count"),
        ("undo.py", "_initialized: bool = False",
         "undo.save_state не получил флаг"),
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
            print(f"  [✓] {fname}: {marker.strip()}")
        else:
            print(f"  [!] {fname}: нет '{marker.strip()}' — {consequence}")
            all_ok = False

    print()
    print("=" * 78)
    if all_ok and not bad:
        print("[✓] Готово. Все проверки пройдены.")
    else:
        print("[!] Готово, но есть предупреждения — см. выше.")
    if args.dry_run:
        print("[DRY-RUN] Ничего не записано. Убери --dry-run для применения.")
    else:
        print(f"    Запустите:  cd {root}")
        print("                py .\\ksenia.py")
        print("    Откат:      git checkout -- .")
    return 0


if __name__ == "__main__":
    sys.exit(main())
