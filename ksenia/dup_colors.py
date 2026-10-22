# -*- coding: utf-8 -*-
"""Детерминированные цвета дубликатов с LRU-кэшем."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Tuple

from PyQt6.QtGui import QColor

from theme import is_dark

DUP_NAME_SAT = 70
DUP_NAME_VAL = 100
DUP_URL_SAT = 60
DUP_URL_VAL = 100
DUP_BOTH_SAT = 55
DUP_BOTH_VAL = 95

_GOLDEN_ANGLE = 137.508
_CACHE_MAX = 4096

_cache: "OrderedDict[Tuple[str, int, int], QColor]" = OrderedDict()
_cache_lock = threading.RLock()


def clear_dup_color_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _hash_key(key: str) -> int:
    if not key:
        return 0
    h = 2166136261
    for ch in key:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _theme_adjusted(sat: int, val: int) -> Tuple[int, int]:
    if is_dark():
        return (_clamp(int(sat * 0.7), 0, 255),
                _clamp(val + 100, 0, 240))
    return _clamp(sat, 0, 255), _clamp(val, 0, 255)


def _dup_color_for_key(key: str, sat: int, val: int) -> QColor:
    if not key:
        return QColor(255, 255, 255)

    cache_key = (key, sat, val)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            _cache.move_to_end(cache_key)
            return QColor(cached)

    h = _hash_key(key)
    base_hue = h % 360
    golden_step = int(h * _GOLDEN_ANGLE) % 360
    hue = (base_hue * 3 + golden_step) % 360
    sat2, val2 = _theme_adjusted(sat, val)
    color = QColor.fromHsv(_clamp(hue, 0, 359), sat2, val2)

    with _cache_lock:
        _cache[cache_key] = QColor(color)
        _cache.move_to_end(cache_key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return color


def dup_color_name(key: str) -> QColor:
    return _dup_color_for_key(key, DUP_NAME_SAT, DUP_NAME_VAL)


def dup_color_url(key: str) -> QColor:
    return _dup_color_for_key(key, DUP_URL_SAT, DUP_URL_VAL)


def dup_color_both(key: str) -> QColor:
    return _dup_color_for_key(key, DUP_BOTH_SAT, DUP_BOTH_VAL)


DUP_TEXT_DARK = QColor(20, 20, 20)
DUP_TEXT_LIGHT = QColor(255, 255, 255)
DUP_TEXT_LUMA_THRESHOLD = 150.0


def dup_text_color_for_bg(bg: QColor) -> QColor:
    if bg is None:
        return DUP_TEXT_DARK
    luma = (0.299 * bg.red() + 0.585 * bg.green()
            + 0.114 * bg.blue())
    return (DUP_TEXT_LIGHT if luma < DUP_TEXT_LUMA_THRESHOLD
            else DUP_TEXT_DARK)