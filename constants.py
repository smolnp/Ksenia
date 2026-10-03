# -*- coding: utf-8 -*-
"""Все константы, статусы, цвета, фильтры."""

from __future__ import annotations

from typing import Dict

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QMessageBox, QDialogButtonBox


# =====================================================================
# Версия приложения
# =====================================================================
APP_VERSION = "0.2"


# =====================================================================
# Лимиты и таймауты
# =====================================================================
UNDO_MAX_STATES = 100
RECENT_FILES_MAX = 10
URL_CHECK_MAX_WORKERS = 4
DEFAULT_TIMEOUT = 5
CACHE_SCHEMA_VERSION = 12
LOADED_CHANNELS_TTL_SEC = 1800
MAX_URL_LENGTH = 4000
STREAMING_PROTOCOLS = frozenset(
    ('rtmp', 'rtsp', 'udp', 'tcp', 'rtp', 'srt', 'rist'))
EPG_MAX_BYTES = 32 * 1024 * 1024
SEARCH_DEBOUNCE_MS = 300
SYNC_DEBOUNCE_MS = 100
STATE_SAVE_DEBOUNCE_MS = 200
EPG_LOAD_TIMEOUT_SEC = 20
EPG_SOURCE_TIMEOUT_SEC = 15
SOURCE_LOAD_TIMEOUT_SEC = 15
FALLBACK_DAYS_DEFAULT = 3
SEARCH_WORKER_MAX = 4
MAX_LOADED_SOURCES = 64
DOMAIN_UA_CACHE_MAX = 8192
DOMAIN_BL_CACHE_MAX = 8192
MAX_SOURCE_FILE_BYTES = 256 * 1024 * 1024
EPG_CACHE_TTL_HOURS = 24
CHECK_RESULT_CACHE_TTL_HOURS = 24
THEME_ICON_NEGATIVE_TTL_SEC = 300
DUPLICATE_DIALOG_MAX_ROWS = 5000
DONATION_WALLET = "4100118517127"
DONATION_URL = f"https://yoomoney.ru/to/{DONATION_WALLET}"
REPLACEMENT_MAX_WORKERS_DEFAULT = 4
ALIVE_INDEX_CACHE_MAX = 5000
SOURCE_CHECK_BATCH_SIZE_DEFAULT = 100
SOURCE_CHECK_WORKERS_DEFAULT = 4
SOURCE_CHECK_TIMEOUT_DEFAULT = 3
SOURCE_CHECK_TRUST_SEC_DEFAULT = 3600


# =====================================================================
# VLC
# =====================================================================
VLC_DEFAULT_CHECK_TIMEOUT = 3
VLC_USER_AGENT = 'VLC/3.0.20 LibVLC/3.0.20'
VLC_INSTANCE_USER_AGENT = f"KseniaM3UEditor/{APP_VERSION}"
VLC_PLAYER_DEFAULT_VOLUME = 100
VLC_PLAYER_DEFAULT_WIDTH = 960
VLC_PLAYER_DEFAULT_HEIGHT = 600


# =====================================================================
# EPG (нечёткий поиск)
# =====================================================================
EPG_FUZZY_ENABLED_DEFAULT = True
EPG_FUZZY_THRESHOLD_DEFAULT = 0.85
EPG_FUZZY_MIN_LENGTH_DEFAULT = 5
EPG_FUZZY_MIN_GAP_DEFAULT = 0.05
EPG_FUZZY_CACHE_LIMIT = 4096
EPG_ALLOWED_META_FIELDS = frozenset(
    {'tvg_id', 'tvg_name', 'tvg_logo', 'tvg_chno'})


# =====================================================================
# Stable-экспорт
# =====================================================================
STABLE_STATE_FILE = "stable_state.json"
STABLE_LATENCY_THRESHOLD_MS = 1500
STABLE_CHECK_TIMEOUT_SEC = 5
STABLE_CHECK_MAX_WORKERS = 8


# =====================================================================
# Файловые фильтры для QFileDialog
# =====================================================================
M3U_FILTER = "M3U (*.m3u *.m3u8);;Все файлы (*.*)"
JSON_FILTER = "JSON (*.json);;Все файлы (*.*)"
CSV_FILTER = "CSV (*.csv);;Все файлы (*.*)"
ALL_FILTER = "Все файлы (*.*)"
EXE_FILTER = "Исполняемые файлы (*.exe);;Все файлы (*.*)"


# =====================================================================
# Группы / фильтры таблицы
# =====================================================================
GROUP_FILTER_ALL = "Все группы"
GROUP_FILTER_DUPLICATES = "🔁 Дубликаты"
DEFAULT_GROUP = "Без группы"


# =====================================================================
# Кнопки диалогов
# =====================================================================
YES_NO = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
OK_CANCEL = QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)
CLOSE_BB = QDialogButtonBox.StandardButton.Close


# =====================================================================
# Авто-поиск источников
# =====================================================================
SOURCES_FINDER_MAX_GITHUB_DEFAULT = 15
SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT = 10
SOURCES_FINDER_UPDATE_INTERVAL_HOURS = 24
SOURCES_FINDER_DEFAULT_PRIORITY = 5
SOURCES_FINDER_EXCLUDE_KEYWORDS = ('zabava', 'забава', 'wink')
SOURCES_FINDER_MAX_AGE_DAYS = 30
SOURCES_FINDER_HEALTH_WORKERS = 8
SOURCES_FINDER_HEALTH_TIMEOUT = 8
SOURCES_FINDER_HEALTH_MAX_BYTES = 8192


# =====================================================================
# Тексты статусов
# =====================================================================
class StatusText:
    UNCHECKED = "⚪ Не проверен"
    NO_URL = "∅ Нет URL"
    WORKING = "✓ Работает"
    NOT_WORKING = "✗ Не работает"
    UNSUPPORTED = "🟦 Не поддерживается"
    TIMEOUT = "⏳ Таймаут"
    DNS_FAIL = "🌐 DNS не резолвится"
    CONN_ERROR = "🔗 Ошибка соединения"
    GEOBLOCK = "🌍 Геоблок (403)"
    NOT_FOUND = "❌ Не найден (404)"
    CANCELLED = "Отменено"
    BLOCKED_BY_DOMAIN = "🚫 Заблокирован ЧС домен/IP"


# =====================================================================
# Цвета URL
# =====================================================================
URL_FG_COLORS: Dict[str, QColor] = {
    'no_url': QColor(160, 160, 160),
    'working': QColor(0, 140, 0),
    'not_working': QColor(200, 0, 0),
    'unsupported': QColor(0, 80, 200),
    'unchecked': QColor(120, 120, 0),
    'neutral': QColor(80, 80, 80),
    'orphan_bg': QColor(255, 250, 200),
}


# =====================================================================
# Подсветка дубликатов (детерминированный цвет по ключу группы)
# =====================================================================
DUP_NAME_SAT = 70
DUP_NAME_VAL = 100
DUP_URL_SAT = 60
DUP_URL_VAL = 100
DUP_BOTH_SAT = 55
DUP_BOTH_VAL = 95

_GOLDEN_ANGLE = 137.508


def _hash_key(key: str) -> int:
    """Стабильный неотрицательный хэш строки (FNV-1a 32-bit)."""
    if not key:
        return 0
    h = 2166136261
    for ch in key:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _dup_color_for_key(key: str, sat: int, val: int) -> QColor:
    """Детерминированный цвет для группы дубликатов.

    Разные ключи → разные оттенки за счёт золотого угла.
    Одинаковый ключ → одинаковый цвет при любых перерисовках.
    """
    if not key:
        return QColor(255, 255, 255)
    h = _hash_key(key)
    base_hue = h % 360
    golden_step = int(h * _GOLDEN_ANGLE) % 360
    hue = (base_hue * 3 + golden_step) % 360
    return QColor.fromHsv(hue, sat, val)


def dup_color_name(key: str) -> QColor:
    return _dup_color_for_key(key, DUP_NAME_SAT, DUP_NAME_VAL)


def dup_color_url(key: str) -> QColor:
    return _dup_color_for_key(key, DUP_URL_SAT, DUP_URL_VAL)


def dup_color_both(key: str) -> QColor:
    return _dup_color_for_key(key, DUP_BOTH_SAT, DUP_BOTH_VAL)


# Контрастный цвет текста для фона
DUP_TEXT_DARK = QColor(20, 20, 20)
DUP_TEXT_LIGHT = QColor(255, 255, 255)
DUP_TEXT_LUMA_THRESHOLD = 150.0


def dup_text_color_for_bg(bg: QColor) -> QColor:
    """Контрастный цвет текста для фона bg."""
    if bg is None:
        return DUP_TEXT_DARK
    luma = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return DUP_TEXT_LIGHT if luma < DUP_TEXT_LUMA_THRESHOLD else DUP_TEXT_DARK