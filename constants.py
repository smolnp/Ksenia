# -*- coding: utf-8 -*-
"""Все константы, статусы, цвета, фильтры."""

from __future__ import annotations
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QMessageBox, QDialogButtonBox
from typing import Dict

APP_VERSION = "0.1"

UNDO_MAX_STATES = 100
RECENT_FILES_MAX = 10
URL_CHECK_MAX_WORKERS = 4
DEFAULT_TIMEOUT = 5
CACHE_SCHEMA_VERSION = 11
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
ENABLE_HEAD_FOR_STREAMS = False
VLC_DEFAULT_CHECK_TIMEOUT = 3
VLC_USER_AGENT = 'VLC/3.0.20 LibVLC/3.0.20'
VLC_STREAM_CONTENT_TYPES = (
    'application/vnd.apple.mpegurl',
    'application/x-mpegurl',
    'audio/mpegurl',
    'audio/x-mpegurl',
    'application/dash+xml',
    'video/mp2t',
    'video/mp4',
    'application/octet-stream',
    'video/x-flv',
    'application/x-mpegts',
)
EPG_FUZZY_ENABLED_DEFAULT = True
EPG_FUZZY_THRESHOLD_DEFAULT = 0.85
EPG_FUZZY_MIN_LENGTH_DEFAULT = 5
EPG_FUZZY_MIN_GAP_DEFAULT = 0.05
EPG_FUZZY_CACHE_LIMIT = 4096
EPG_ALLOWED_META_FIELDS = frozenset(
    {'tvg_id', 'tvg_name', 'tvg_logo', 'tvg_chno'})
VLC_INSTANCE_USER_AGENT = f"KseniaM3UEditor/{APP_VERSION}"
VLC_PLAYER_DEFAULT_VOLUME = 100
VLC_PLAYER_DEFAULT_WIDTH = 960
VLC_PLAYER_DEFAULT_HEIGHT = 600
STABLE_STATE_FILE = "stable_state.json"
STABLE_LATENCY_THRESHOLD_MS = 1500
STABLE_CHECK_TIMEOUT_SEC = 5
STABLE_CHECK_MAX_WORKERS = 8
M3U_FILTER = "M3U (*.m3u *.m3u8);;Все файлы (*.*)"
JSON_FILTER = "JSON (*.json);;Все файлы (*.*)"
CSV_FILTER = "CSV (*.csv);;Все файлы (*.*)"
ALL_FILTER = "Все файлы (*.*)"
EXE_FILTER = "Исполняемые файлы (*.exe);;Все файлы (*.*)"
GROUP_FILTER_ALL = "Все группы"
DEFAULT_GROUP = "Без группы"
YES_NO = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
OK_CANCEL = QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)
CLOSE_BB = QDialogButtonBox.StandardButton.Close


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


URL_FG_COLORS: Dict[str, QColor] = {
    'no_url': QColor(160, 160, 160),
    'working': QColor(0, 140, 0),
    'not_working': QColor(200, 0, 0),
    'unsupported': QColor(0, 80, 200),
    'unchecked': QColor(120, 120, 0),
    'neutral': QColor(80, 80, 80),
    'orphan_bg': QColor(255, 250, 200),
}