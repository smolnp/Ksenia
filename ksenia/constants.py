# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Константы, статусы, фильтры (без цветов — они в theme.py)."""
from __future__ import annotations

from PyQt6.QtWidgets import QDialogButtonBox, QMessageBox

APP_VERSION = "0.2.3"  # ← bump версии после P0
APP_ORG = "Ksenia"
APP_NAME = "KseniaM3UEditor"

DEFAULT_EPG_URL = ""

NORMALIZATION_SETTINGS_KEYS = (
    'remove_parentheses_in_names',
    'remove_brackets_in_names',
    'remove_emojis_in_names',
    'ignore_special_chars_in_names',
    'use_iptv_org_aliases',
)

UNDO_MAX_STATES = 100
RECENT_FILES_MAX = 10
URL_CHECK_MAX_WORKERS = 8
DEFAULT_TIMEOUT = 5
MIN_SQLITE_VERSION = (3, 24, 0)
LOADED_CHANNELS_TTL_SEC = 1800
MAX_URL_LENGTH = 4000
MAX_M3U_LINES = 2_000_000
MAX_JSON_BYTES = 256 * 1024 * 1024
EPG_MAX_BYTES = 32 * 1024 * 1024
SEARCH_DEBOUNCE_MS = 500
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
THEME_ICON_CACHE_MAX = 256

DUPLICATE_DIALOG_MAX_ROWS = 5000
DONATION_WALLET = "4100118517127"
DONATION_URL = f"https://yoomoney.ru/to/{DONATION_WALLET}"
REPLACEMENT_MAX_WORKERS_DEFAULT = 4
ALIVE_INDEX_CACHE_MAX = 5000
SOURCE_CHECK_BATCH_SIZE_DEFAULT = 100
SOURCE_CHECK_WORKERS_DEFAULT = 4
SOURCE_CHECK_TIMEOUT_DEFAULT = 3
SOURCE_CHECK_TRUST_SEC_DEFAULT = 3600
SEARCH_FUTURES_TIMEOUT_SEC = 300
FUTURES_SHUTDOWN_WAIT_SEC = 5.0
SQLITE_BATCH_CHUNK = 300
MAX_CHANNELS_PER_PLAYLIST = 500_000

VERIFY_CANDIDATES_TIMEOUT_DEFAULT = 3
VERIFY_CANDIDATES_MAX_WORKERS_DEFAULT = 4
SAFETY_MAX_SNAPSHOTS_DEFAULT = 100
EPG_ALTERNATIVE_NAMES_MAX = 20

PLAYER_DEFAULT_VOLUME = 100
PLAYER_ASPECT_DEFAULT = "auto"
PLAYER_HWDEC_DEFAULT = "auto-safe"
PLAYER_ADAPTIVE_LOGIC_DEFAULT = "highest"
PLAYER_NETWORK_CACHING_DEFAULT_MS = 1500
PLAYER_LIVE_CACHING_DEFAULT_MS = 1500
PLAYER_STATUS_POLL_MS = 300
PLAYER_IPC_CONNECT_TIMEOUT_MS = 10000

EPG_FUZZY_ENABLED_DEFAULT = True
EPG_FUZZY_THRESHOLD_DEFAULT = 0.85
EPG_FUZZY_MIN_LENGTH_DEFAULT = 5
EPG_FUZZY_MIN_GAP_DEFAULT = 0.05
EPG_FUZZY_CACHE_LIMIT = 4096
EPG_ALLOWED_META_FIELDS = frozenset(
    {'tvg_id', 'tvg_name', 'tvg_logo', 'tvg_chno'})

STABLE_STATE_FILE = "stable_state.json"
STABLE_LATENCY_THRESHOLD_MS = 1500
STABLE_CHECK_TIMEOUT_SEC = 5
STABLE_CHECK_MAX_WORKERS = 8

M3U_FILTER = "M3U (*.m3u *.m3u8);;Все файлы (*.*)"
JSON_FILTER = "JSON (*.json);;Все файлы (*.*)"
CSV_FILTER = "CSV (*.csv);;Все файлы (*.*)"
ALL_FILTER = "Все файлы (*.*)"

GROUP_FILTER_ALL = "Все группы"
GROUP_FILTER_DUPLICATES = "🔁 Дубликаты"
DEFAULT_GROUP = "Без группы"

# ── Фильтр статуса ──
STATUS_FILTER_ALL = "Всего"
STATUS_FILTER_WITH_URL = "Со ссылками"
STATUS_FILTER_WITHOUT_URL = "Без ссылок"
STATUS_FILTER_WORKING = "Работают"
STATUS_FILTER_NOT_WORKING = "Не работают"
STATUS_FILTER_UNCHECKED = "Не проверены"

STATUS_FILTER_ITEMS = [
    STATUS_FILTER_ALL,
    STATUS_FILTER_WITH_URL,
    STATUS_FILTER_WITHOUT_URL,
    STATUS_FILTER_WORKING,
    STATUS_FILTER_NOT_WORKING,
    STATUS_FILTER_UNCHECKED,
]

YES_NO = (QMessageBox.StandardButton.Yes |
          QMessageBox.StandardButton.No)
OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)
CLOSE_BB = QDialogButtonBox.StandardButton.Close

FFPROBE_TIMEOUT_SEC = 5
FFPROBE_PATH = "ffprobe"
FFPROBE_MAX_WORKERS = 4

AUTO_REPAIR_MAX_ATTEMPTS = 2
AUTO_REPAIR_SCHEME_TIMEOUT = 3

AUTO_CHECK_INTERVAL_HOURS_DEFAULT = 24
AUTO_CHECK_MIN_INTERVAL_HOURS = 1


class StatusText:
    UNCHECKED = "⚪ Не проверено"
    NO_URL = "∅ Нет ссылки"           # ← было «Нет URL»
    WORKING = "✓ Работает"
    NOT_WORKING = "✗ Не работает"
    UNSUPPORTED = "🟦 Не поддерживается"
    TIMEOUT = "⏳ Таймаут"
    DNS_FAIL = "🌐 DNS не разрешается"  # ← было «DNS не резолвится»
    CONN_ERROR = "🔗 Ошибка соединения"
    GEOBLOCK = "🌍 Геоблок (403)"
    NOT_FOUND = "❌ Не найден (404)"
    CANCELLED = "⏹ Отменено"
    BLOCKED_BY_DOMAIN = "🚫 Домен/IP в чёрном списке"  # ← было «Заблокирован ЧС домен/IP»
    EMPTY_RESPONSE = "📭 Пустой ответ"
    HTML_INSTEAD_OF_STREAM = "📄 HTML вместо потока"
    JSON_INSTEAD_OF_STREAM = "📄 JSON вместо потока"
    NON_VIDEO_CONTENT = "📄 Не видео-контент"
    REPAIRED = "🔧 Автопочинка"       # ← было «Починена»
    FFPROBE_MISSING = "🎬 ffprobe недоступен"


SAVE_ENCODINGS = ('auto', 'utf-8', 'utf-8-sig', 'cp1251')