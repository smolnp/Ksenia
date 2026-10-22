# -*- coding: utf-8 -*-
"""Эвристика выбора канонического display-name из EPG."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from name_normalizer import (
    dominant_script, is_stub_name, strip_orig_suffix,
    strip_provider_prefix)


_LANG_PRIORITY: Dict[str, int] = {
    'ru': 100, 'uk': 90, 'be': 80,
    'en': 70, 'de': 60, 'fr': 55, 'es': 50, 'it': 45,
    'pl': 40, 'tr': 40, 'he': 35, 'ka': 30, 'hy': 30, 'az': 30,
    'ro': 25, 'bg': 25, 'cs': 25, 'sk': 25, 'hu': 25, 'hr': 25,
    'sr': 25, 'sl': 25, 'lv': 25, 'lt': 25, 'et': 25,
    'pt': 25, 'nl': 25, 'sv': 25, 'da': 25, 'no': 25, 'fi': 25,
}

_BAD_WORDS = (
    'home of', 'классика hd', 'orig', 'ориг',
    'backup', 'резерв', 'test', 'тест',
    'заглушка', 'placeholder', 'not 24/7', 'не 24/7',
    'н[её] 24/7',
)


def pick_canonical_display_name(
        names: List[str],
        langs: Optional[Dict[str, str]] = None,
        source_lang: str = 'ru',
        names_priority: Optional[Dict[str, int]] = None,
) -> str:
    if not names:
        return ""
    valid = [n for n in names if n and n.strip()]
    if not valid:
        return ""
    if len(valid) == 1:
        return valid[0]
    langs = langs or {}
    names_priority = names_priority or {}
    best_name = valid[0]
    best_score = -1e9
    for i, name in enumerate(valid):
        score = _score_name(name, i, len(valid), langs,
                             source_lang, names_priority)
        if score > best_score:
            best_score = score
            best_name = name
    return best_name


def _score_name(name: str, index: int, total: int,
                langs: Dict[str, str], source_lang: str,
                names_priority: Dict[str, int]) -> float:
    score = 0.0
    low = name.casefold()
    if names_priority:
        score += names_priority.get(name, 0) * 10
    score += (total - index) * 3.0
    lang = (langs.get(name, '') or '').casefold()
    score += _LANG_PRIORITY.get(lang, 0) * 0.3
    if is_stub_name(name):
        score -= 200
    if strip_provider_prefix(name) != name:
        score -= 25
    if strip_orig_suffix(name) != name:
        score -= 25
    for bad in _BAD_WORDS:
        if re.search(bad, low):
            score -= 15
    if name.isupper() and len(name) > 3:
        score -= 10
    elif name[:1].isupper() and not name.isupper():
        score += 5
    if len(name) > 50:
        score -= 5
    elif len(name) < 3:
        score -= 3
    script = dominant_script(name)
    if script == 'cyrillic' and source_lang in ('ru', 'uk', 'be'):
        score += 3
    elif script == 'latin' and source_lang in ('en', 'de', 'fr'):
        score += 3
    elif script == 'mixed':
        score -= 2
    if re.match(r'^\d{4,}', name):
        score -= 5
    if re.search(r'\[[^\]]+\]', name):
        score -= 8
    if low.startswith('['):
        score -= 5
    return score


def pick_all_alternative_names(names: List[str],
                                canonical: str,
                                max_alternatives: int = 20
                                ) -> List[str]:
    result: List[str] = []
    seen: set = set()
    for n in names:
        if not n or n == canonical:
            continue
        n = n.strip()
        if not n or n in seen:
            continue
        seen.add(n)
        result.append(n)
        if len(result) >= max_alternatives:
            break
        stripped = strip_provider_prefix(n)
        if stripped and stripped != n and stripped not in seen:
            seen.add(stripped)
            result.append(stripped)
            if len(result) >= max_alternatives:
                break
        no_orig = strip_orig_suffix(n)
        if no_orig and no_orig != n and no_orig not in seen:
            seen.add(no_orig)
            result.append(no_orig)
            if len(result) >= max_alternatives:
                break
    return result