import sys
import os
import re
import json
import requests
import concurrent.futures
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple, Set
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse
import time
import logging
import hashlib
import csv
from difflib import SequenceMatcher
import socket
import urllib3
import unicodedata

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QGroupBox, QFormLayout, QLineEdit, QPushButton, QComboBox,
    QLabel, QMenuBar, QMenu, QStatusBar, QToolBar,
    QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
    QListWidget, QListWidgetItem, QScrollArea, QGridLayout,
    QInputDialog, QToolButton, QStyle, QTextEdit, QCheckBox,
    QRadioButton, QProgressBar, QProgressDialog, QFrame,
    QPlainTextEdit, QSpacerItem, QSizePolicy, QSlider,
    QSpinBox, QDoubleSpinBox, QButtonGroup
)
from PyQt6.QtCore import (
    Qt, QTimer, QSettings, QSize, QPoint,
    QStringListModel, QEvent, pyqtSignal,
    QThread, QObject, QModelIndex, QRunnable, QThreadPool,
    QMetaObject, Q_ARG, pyqtSlot
)
from PyQt6.QtGui import (
    QAction, QKeySequence, QColor, QFont, QIcon,
    QTextCursor, QClipboard, QDesktopServices, QPalette,
    QContextMenuEvent, QShortcut, QTextCharFormat, QSyntaxHighlighter,
    QFontMetrics, QPainter, QBrush, QPen, QGuiApplication
)


class SystemThemeManager:
    
    @staticmethod
    def get_hotkeys() -> Dict[str, str]:
        return {
            'open': 'Ctrl+O',
            'save': 'Ctrl+S',
            'save_as': 'Ctrl+Shift+S',
            'new': 'Ctrl+N',
            'find': 'Ctrl+F',
            'add': 'Ctrl+Shift+A',
            'delete': 'Delete',
            'exit': 'Alt+F4',
            'copy': 'Ctrl+C',
            'paste': 'Ctrl+V',
            'undo': 'Ctrl+Z',
            'redo': 'Ctrl+Y'
        }
    
    @staticmethod
    def get_config_dir() -> str:
        if sys.platform == "linux" or sys.platform == "linux2":
            config_home = os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
            return os.path.join(config_home, "ksenia")
        elif sys.platform == "darwin":
            return os.path.expanduser("~/Library/Application Support/Ksenia")
        else:
            return os.path.expanduser("~/.ksenia")


class LinkQuality(Enum):
    UNKNOWN = 0
    WORKING = 1
    NOT_WORKING = 2


class ChannelNameNormalizer:
    """Класс для нормализации названий каналов с интеллектуальным удалением информации"""
    
    QUALITY_PATTERNS = [
        r'\(\s*(?:720|1080|1280|480|360|2160|4K|8K|UHD|HD|SD|FHD|FullHD)[pPi]?\s*\)',
        r'\(\s*(?:720|1080|1280|480|360|2160)[pP]\s*\)',
        r'\(\s*(?:4K|8K|UHD|HD|SD|FHD|FullHD)\s*\)',
        r'\(\s*(?:60|50|30|25)[fFpPsS]?\s*\)',
        r'\(\s*(?:High|Medium|Low)\s+Quality\s*\)',
        r'\(\s*\[?[0-9]+[pPi]\s*\]?\s*\)',
        r'\(\s*HQ\s*\)',
        r'\(\s*LQ\s*\)',
        r'\(\s*SQ\s*\)',
    ]
    
    BRACKETS_TO_REMOVE = [
        r'\[Geo-blocked\]',
        r'\[Not\s+24/7\]',
        r'\[Geo[-\s]?blocked\]',
        r'\[Not\s*24\s*/\s*7\]',
        r'\[Blocked\]',
        r'\[Restricted\]',
        r'\[GeoRestricted\]',
    ]
    
    EMOJI_PATTERN = re.compile(
        '['
        '\U0001F600-\U0001F64F'
        '\U0001F300-\U0001F5FF'
        '\U0001F680-\U0001F6FF'
        '\U0001F1E0-\U0001F1FF'
        '\U00002702-\U000027B0'
        '\U000024C2-\U0001F251'
        ']+',
        flags=re.UNICODE
    )
    
    @staticmethod
    def normalize(name: str, remove_parentheses: bool = True, remove_brackets: bool = True, 
                  remove_emojis: bool = True, remove_special_chars: bool = True) -> str:
        if not name:
            return ""
        
        normalized = name
        
        if remove_parentheses:
            for pattern in ChannelNameNormalizer.QUALITY_PATTERNS:
                normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)
            normalized = re.sub(r'\(\s*\)', '', normalized)
        
        if remove_brackets:
            for pattern in ChannelNameNormalizer.BRACKETS_TO_REMOVE:
                normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)
            normalized = re.sub(r'\[\s*\]', '', normalized)
        
        normalized = re.sub(r'\{[^}]*\}', '', normalized)
        
        if remove_emojis:
            normalized = ChannelNameNormalizer.EMOJI_PATTERN.sub('', normalized)
        
        if remove_special_chars:
            normalized = re.sub(r'[^\w\s\-\+\\/\.\,:()\[\]]', ' ', normalized)
            normalized = re.sub(r'\s+', ' ', normalized)
        
        normalized = normalized.lower().strip()
        normalized = re.sub(r'\s+', ' ', normalized)
        
        return normalized
    
    @staticmethod
    def get_normalized_variants(name: str) -> List[str]:
        variants = []
        base_normalized = ChannelNameNormalizer.normalize(name)
        if base_normalized:
            variants.append(base_normalized)
        no_quality = ChannelNameNormalizer.normalize(name, remove_emojis=False)
        if no_quality and no_quality != base_normalized:
            variants.append(no_quality)
        no_emojis = ChannelNameNormalizer.normalize(name, remove_parentheses=False, remove_brackets=False)
        if no_emojis and no_emojis != base_normalized:
            variants.append(no_emojis)
        no_remove_brackets = ChannelNameNormalizer.normalize(name, remove_parentheses=False, remove_brackets=False, remove_emojis=True)
        if no_remove_brackets and no_remove_brackets != base_normalized:
            variants.append(no_remove_brackets)
        return list(dict.fromkeys(variants))
    
    @staticmethod
    def calculate_similarity_normalized(name1: str, name2: str, settings: 'LinkReplacementSettings') -> float:
        if settings.ignore_special_chars_in_names:
            norm1 = ChannelNameNormalizer.normalize(name1)
            norm2 = ChannelNameNormalizer.normalize(name2)
        else:
            norm1 = name1.lower()
            norm2 = name2.lower()
        return SequenceMatcher(None, norm1, norm2).ratio()
    
    @staticmethod
    def extract_quality(name: str) -> Optional[str]:
        for pattern in ChannelNameNormalizer.QUALITY_PATTERNS:
            match = re.search(pattern, name, re.IGNORECASE)
            if match:
                return match.group(0)
        return None
    
    @staticmethod
    def extract_country_code(name: str) -> Optional[str]:
        match = re.search(r'\[([A-Z]{2,3})\]', name)
        if match:
            return match.group(1)
        return None
    
    @staticmethod
    def extract_region(name: str) -> Optional[str]:
        match = re.search(r'\(([А-Яа-яёЁ\s\-]+)\)', name)
        if match:
            region = match.group(1).strip()
            if not re.search(r'[0-9]+[pPi]', region, re.IGNORECASE):
                return region
        return None
    
    @staticmethod
    def extract_timezone(name: str) -> Optional[str]:
        match = re.search(r'\(\+(\d+)\)', name)
        if match:
            return match.group(1)
        return None
    
    @staticmethod
    def fix_encoding(text: str) -> str:
        """Исправление кодировки для строк, которые выглядят как испорченная кириллица"""
        if not text:
            return text
        
        try:
            if '–' in text or '—' in text or '∏' in text or 'ø' in text:
                bytes_data = text.encode('latin1')
                decoded = bytes_data.decode('utf-8', errors='replace')
                if any(c in decoded for c in ['а', 'б', 'в', 'г', 'д']):
                    return decoded
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        
        try:
            if any(ord(c) > 127 for c in text):
                return text
        except:
            pass
        
        return text


class LinkSource:
    
    def __init__(self):
        self.name: str = ""
        self.path: str = ""
        self.source_type: str = "local"
        self.last_updated: Optional[datetime] = None
        self.total_links: int = 0
        self.working_links: int = 0
        self.priority: int = 5
        self.enabled: bool = True
        self.auto_update: bool = False
        self.update_interval_hours: int = 24
        self.tags: List[str] = []
        self.description: str = ""
        self.link_cache: Dict[str, List[str]] = {}
        self.last_cache_update: Optional[datetime] = None
        self.encoding: str = "utf-8"
    
    def copy(self) -> 'LinkSource':
        source = LinkSource()
        source.name = self.name
        source.path = self.path
        source.source_type = self.source_type
        source.last_updated = self.last_updated
        source.total_links = self.total_links
        source.working_links = self.working_links
        source.priority = self.priority
        source.enabled = self.enabled
        source.auto_update = self.auto_update
        source.update_interval_hours = self.update_interval_hours
        source.tags = self.tags.copy()
        source.description = self.description
        source.link_cache = self.link_cache.copy()
        source.last_cache_update = self.last_cache_update
        source.encoding = self.encoding
        return source
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'path': self.path,
            'source_type': self.source_type,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            'total_links': self.total_links,
            'working_links': self.working_links,
            'priority': self.priority,
            'enabled': self.enabled,
            'auto_update': self.auto_update,
            'update_interval_hours': self.update_interval_hours,
            'tags': self.tags,
            'description': self.description,
            'link_cache': self.link_cache,
            'last_cache_update': self.last_cache_update.isoformat() if self.last_cache_update else None,
            'encoding': self.encoding
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LinkSource':
        source = cls()
        source.name = data.get('name', '')
        source.path = data.get('path', '')
        source.source_type = data.get('source_type', 'local')
        source.total_links = data.get('total_links', 0)
        source.working_links = data.get('working_links', 0)
        source.priority = data.get('priority', 5)
        source.enabled = data.get('enabled', True)
        source.auto_update = data.get('auto_update', False)
        source.update_interval_hours = data.get('update_interval_hours', 24)
        source.tags = data.get('tags', [])
        source.description = data.get('description', '')
        source.link_cache = data.get('link_cache', {})
        source.encoding = data.get('encoding', 'utf-8')
        
        last_updated = data.get('last_updated')
        if last_updated:
            try:
                source.last_updated = datetime.fromisoformat(last_updated)
            except (ValueError, TypeError):
                source.last_updated = None
        
        last_cache_update = data.get('last_cache_update')
        if last_cache_update:
            try:
                source.last_cache_update = datetime.fromisoformat(last_cache_update)
            except (ValueError, TypeError):
                source.last_cache_update = None
        
        return source


class ChannelData:
    
    def __init__(self):
        self.name: str = ""
        self.group: str = "Без группы"
        self.tvg_id: str = ""
        self.tvg_logo: str = ""
        self.url: str = ""
        self.extinf: str = ""
        self.user_agent: str = ""
        self.extvlcopt_lines: List[str] = []
        self.extra_headers: Dict[str, str] = {}
        self.has_url: bool = True
        self.url_status: Optional[bool] = None
        self.url_check_time: Optional[datetime] = None
        self.link_source: str = ""
        self.link_quality: LinkQuality = LinkQuality.UNKNOWN
        self.link_response_time: Optional[float] = None
        self.alternative_urls: List[str] = []
        self.url_history: List[Dict[str, Any]] = []
        self.last_link_replacement: Optional[datetime] = None
        self.created_date: datetime = datetime.now()
        self.modified_date: datetime = datetime.now()
    
    def copy(self) -> 'ChannelData':
        channel = ChannelData()
        channel.name = self.name
        channel.group = self.group
        channel.tvg_id = self.tvg_id
        channel.tvg_logo = self.tvg_logo
        channel.url = self.url
        channel.extinf = self.extinf
        channel.user_agent = self.user_agent
        channel.extvlcopt_lines = self.extvlcopt_lines.copy()
        channel.extra_headers = self.extra_headers.copy()
        channel.has_url = self.has_url
        channel.url_status = self.url_status
        channel.url_check_time = self.url_check_time
        channel.link_source = self.link_source
        channel.link_quality = self.link_quality
        channel.link_response_time = self.link_response_time
        channel.alternative_urls = self.alternative_urls.copy()
        channel.url_history = self.url_history.copy()
        channel.last_link_replacement = self.last_link_replacement
        channel.created_date = self.created_date
        channel.modified_date = self.modified_date
        return channel
    
    def copy_metadata_only(self) -> 'ChannelData':
        channel = ChannelData()
        channel.name = self.name
        channel.group = self.group
        channel.tvg_id = self.tvg_id
        channel.tvg_logo = self.tvg_logo
        channel.user_agent = self.user_agent
        channel.extvlcopt_lines = self.extvlcopt_lines.copy()
        channel.extra_headers = self.extra_headers.copy()
        channel.update_extinf()
        channel.created_date = self.created_date
        channel.modified_date = self.modified_date
        return channel
    
    def update_metadata_from(self, source_channel: 'ChannelData'):
        self.group = source_channel.group
        self.tvg_id = source_channel.tvg_id
        self.tvg_logo = source_channel.tvg_logo
        self.user_agent = source_channel.user_agent
        self.extvlcopt_lines = source_channel.extvlcopt_lines.copy()
        self.extra_headers = source_channel.extra_headers.copy()
        self.update_extinf()
        self.modified_date = datetime.now()
    
    def update_extinf(self):
        parts = ["#EXTINF:-1"]
        if self.tvg_id:
            parts.append(f'tvg-id="{self.tvg_id}"')
        if self.tvg_logo:
            parts.append(f'tvg-logo="{self.tvg_logo}"')
        if self.group:
            parts.append(f'group-title="{self.group}"')
        parts.append(f',{self.name}')
        self.extinf = ' '.join(parts)
    
    def parse_extvlcopt_headers(self):
        self.extra_headers = {}
        self.user_agent = ""
        
        for line in self.extvlcopt_lines:
            if not line or '=' not in line:
                continue
                
            if line.startswith('#EXTVLCOPT:http-user-agent='):
                try:
                    user_agent = line.replace('#EXTVLCOPT:http-user-agent=', '').strip('"')
                    self.extra_headers['User-Agent'] = user_agent
                    self.user_agent = user_agent
                except (ValueError, IndexError):
                    logger.warning(f"Не удалось распарсить User-Agent: {line}")
            elif line.startswith('#EXTVLCOPT:http-referrer='):
                try:
                    referrer = line.replace('#EXTVLCOPT:http-referrer=', '').strip('"')
                    self.extra_headers['Referer'] = referrer
                except (ValueError, IndexError):
                    logger.warning(f"Не удалось распарсить Referer: {line}")
            elif line.startswith('#EXTVLCOPT:http-header='):
                try:
                    header_line = line.replace('#EXTVLCOPT:http-header=', '').strip('"')
                    if ':' in header_line:
                        key, value = header_line.split(':', 1)
                        self.extra_headers[key.strip()] = value.strip()
                except (ValueError, IndexError):
                    logger.warning(f"Не удалось распарсить заголовок: {line}")
    
    def update_extvlcopt_from_headers(self):
        self.extvlcopt_lines = []
        
        if self.user_agent:
            self.extvlcopt_lines.append(f'#EXTVLCOPT:http-user-agent="{self.user_agent}"')
        
        for key, value in self.extra_headers.items():
            if key.lower() == 'user-agent':
                continue
            elif key.lower() == 'referer':
                self.extvlcopt_lines.append(f'#EXTVLCOPT:http-referrer="{value}"')
            else:
                self.extvlcopt_lines.append(f'#EXTVLCOPT:http-header="{key}: {value}"')
    
    def add_url_to_history(self, old_url: str, new_url: str, reason: str, source: str = ""):
        self.url_history.append({
            'old_url': old_url,
            'new_url': new_url,
            'reason': reason,
            'source': source,
            'timestamp': datetime.now().isoformat(),
            'channel_name': self.name
        })
        
        if len(self.url_history) > 10:
            self.url_history = self.url_history[-10:]
        self.modified_date = datetime.now()
    
    def get_quality_color(self) -> QColor:
        if self.link_quality == LinkQuality.UNKNOWN:
            return QColor("gray")
        elif self.link_quality == LinkQuality.WORKING:
            return QColor("green")
        elif self.link_quality == LinkQuality.NOT_WORKING:
            return QColor("red")
        else:
            return QColor("gray")
    
    def get_quality_text(self) -> str:
        if self.link_quality == LinkQuality.UNKNOWN:
            return "Неизвестно"
        elif self.link_quality == LinkQuality.WORKING:
            return "Работает"
        elif self.link_quality == LinkQuality.NOT_WORKING:
            return "Не работает"
        else:
            return "Неизвестно"
    
    def get_status_text(self) -> str:
        if not self.has_url or not self.url or not self.url.strip():
            return "∅ Нет URL"
        elif self.url_status is None:
            return "? Не проверялось"
        elif self.url_status is True:
            return "✓ Работает"
        elif self.url_status is False:
            return "✗ Не работает"
        else:
            return "? Неизвестно"
    
    def get_status_tooltip(self) -> str:
        tooltip = f"Канал: {self.name}\nГруппа: {self.group}\n"
        
        if not self.has_url or not self.url or not self.url.strip():
            tooltip += "Нет URL"
        elif self.url_status is None:
            tooltip += "Не проверялось"
        elif self.url_status:
            if self.url_check_time:
                tooltip += f"Работает (проверено: {self.url_check_time.strftime('%H:%M:%S')})"
            else:
                tooltip += "Работает"
        else:
            if self.url_check_time:
                tooltip += f"Не работает (проверено: {self.url_check_time.strftime('%H:%M:%S')})"
            else:
                tooltip += "Не работает"
        
        if self.link_response_time is not None:
            tooltip += f"\nВремя ответа: {self.link_response_time:.2f} сек"
        
        if self.link_quality != LinkQuality.UNKNOWN:
            tooltip += f"\nСтатус: {self.get_quality_text()}"
        
        if self.alternative_urls:
            tooltip += f"\nАльтернативных ссылок: {len(self.alternative_urls)}"
        
        if self.url_history:
            last_change = self.url_history[-1]
            tooltip += f"\nПоследняя замена: {last_change.get('reason', '')}"
        
        tooltip += f"\nСоздан: {self.created_date.strftime('%Y-%m-%d %H:%M')}"
        tooltip += f"\nИзменён: {self.modified_date.strftime('%Y-%m-%d %H:%M')}"
        
        return tooltip
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'group': self.group,
            'tvg_id': self.tvg_id,
            'tvg_logo': self.tvg_logo,
            'url': self.url,
            'extinf': self.extinf,
            'user_agent': self.user_agent,
            'extvlcopt_lines': self.extvlcopt_lines,
            'extra_headers': self.extra_headers,
            'has_url': self.has_url,
            'url_status': self.url_status,
            'url_check_time': self.url_check_time.isoformat() if self.url_check_time else None,
            'link_source': self.link_source,
            'link_quality': self.link_quality.value,
            'link_response_time': self.link_response_time,
            'alternative_urls': self.alternative_urls,
            'url_history': self.url_history,
            'last_link_replacement': self.last_link_replacement.isoformat() if self.last_link_replacement else None,
            'created_date': self.created_date.isoformat(),
            'modified_date': self.modified_date.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChannelData':
        channel = cls()
        channel.name = data.get('name', '')
        channel.group = data.get('group', 'Без группы')
        channel.tvg_id = data.get('tvg_id', '')
        channel.tvg_logo = data.get('tvg_logo', '')
        channel.url = data.get('url', '')
        channel.extinf = data.get('extinf', '')
        channel.user_agent = data.get('user_agent', '')
        channel.extvlcopt_lines = data.get('extvlcopt_lines', [])
        channel.extra_headers = data.get('extra_headers', {})
        channel.has_url = data.get('has_url', True)
        channel.url_status = data.get('url_status')
        channel.link_source = data.get('link_source', '')
        
        check_time = data.get('url_check_time')
        if check_time:
            try:
                channel.url_check_time = datetime.fromisoformat(check_time)
            except (ValueError, TypeError):
                channel.url_check_time = None
        
        quality_value = data.get('link_quality', 0)
        try:
            channel.link_quality = LinkQuality(quality_value)
        except ValueError:
            channel.link_quality = LinkQuality.UNKNOWN
        
        channel.link_response_time = data.get('link_response_time')
        channel.alternative_urls = data.get('alternative_urls', [])
        channel.url_history = data.get('url_history', [])
        
        replacement_time = data.get('last_link_replacement')
        if replacement_time:
            try:
                channel.last_link_replacement = datetime.fromisoformat(replacement_time)
            except (ValueError, TypeError):
                channel.last_link_replacement = None
        
        created_date = data.get('created_date')
        if created_date:
            try:
                channel.created_date = datetime.fromisoformat(created_date)
            except (ValueError, TypeError):
                channel.created_date = datetime.now()
        
        modified_date = data.get('modified_date')
        if modified_date:
            try:
                channel.modified_date = datetime.fromisoformat(modified_date)
            except (ValueError, TypeError):
                channel.modified_date = datetime.now()
        
        return channel
    
    def match_by_name(self, other_channel: 'ChannelData') -> bool:
        return self.name.lower() == other_channel.name.lower()
    
    def match_by_name_and_group(self, other_channel: 'ChannelData') -> bool:
        return (self.name.lower() == other_channel.name.lower() and 
                self.group.lower() == other_channel.group.lower())
    
    def get_similarity_score(self, other: 'ChannelData') -> float:
        scores = []
        
        if self.name and other.name:
            name_similarity = SequenceMatcher(None, self.name.lower(), other.name.lower()).ratio()
            scores.append(name_similarity * 0.4)
        
        if self.group and other.group:
            group_similarity = SequenceMatcher(None, self.group.lower(), other.group.lower()).ratio()
            scores.append(group_similarity * 0.3)
        
        if self.url and other.url:
            url_similarity = 1.0 if self.url == other.url else 0.0
            scores.append(url_similarity * 0.3)
        
        return sum(scores) / len(scores) if scores else 0.0


class LinkReplacementSettings:
    
    def __init__(self):
        self.match_threshold_percent: float = 80.0
        self.use_fuzzy_matching: bool = True
        self.min_name_similarity: float = 0.7
        self.search_type: str = "exact"
        self.filter_temporary_links: bool = True
        self.filter_unsafe_links: bool = True
        self.temporary_domains: List[str] = ["tmp.", "temp.", "short."]
        self.unsafe_domains: List[str] = ["malware.", "phishing.", "spam."]
        self.check_timeout: int = 5
        self.max_workers: int = 5
        self.max_retries: int = 2
        self.retry_delay: float = 0.5
        self.auto_replace_broken: bool = True
        self.auto_replace_missing: bool = True
        self.keep_backup_links: bool = True
        self.max_alternative_urls: int = 5
        self.use_ip_filtering: bool = True
        self.blacklisted_ips: List[str] = []
        self.whitelisted_ips: List[str] = []
        self.blacklisted_domains: List[str] = []
        self.whitelisted_domains: List[str] = []
        self.prioritize_whitelisted: bool = True
        self.blacklist_priority: int = -10
        self.whitelist_priority: int = 10
        self.ignore_special_chars_in_names: bool = True
        self.remove_parentheses_in_names: bool = True
        self.remove_brackets_in_names: bool = True
        self.remove_emojis_in_names: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'match_threshold_percent': self.match_threshold_percent,
            'use_fuzzy_matching': self.use_fuzzy_matching,
            'min_name_similarity': self.min_name_similarity,
            'search_type': self.search_type,
            'filter_temporary_links': self.filter_temporary_links,
            'filter_unsafe_links': self.filter_unsafe_links,
            'temporary_domains': self.temporary_domains,
            'unsafe_domains': self.unsafe_domains,
            'check_timeout': self.check_timeout,
            'max_workers': self.max_workers,
            'max_retries': self.max_retries,
            'retry_delay': self.retry_delay,
            'auto_replace_broken': self.auto_replace_broken,
            'auto_replace_missing': self.auto_replace_missing,
            'keep_backup_links': self.keep_backup_links,
            'max_alternative_urls': self.max_alternative_urls,
            'use_ip_filtering': self.use_ip_filtering,
            'blacklisted_ips': self.blacklisted_ips,
            'whitelisted_ips': self.whitelisted_ips,
            'blacklisted_domains': self.blacklisted_domains,
            'whitelisted_domains': self.whitelisted_domains,
            'prioritize_whitelisted': self.prioritize_whitelisted,
            'blacklist_priority': self.blacklist_priority,
            'whitelist_priority': self.whitelist_priority,
            'ignore_special_chars_in_names': self.ignore_special_chars_in_names,
            'remove_parentheses_in_names': self.remove_parentheses_in_names,
            'remove_brackets_in_names': self.remove_brackets_in_names,
            'remove_emojis_in_names': self.remove_emojis_in_names
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LinkReplacementSettings':
        settings = cls()
        
        settings.match_threshold_percent = data.get('match_threshold_percent', 80.0)
        settings.use_fuzzy_matching = data.get('use_fuzzy_matching', True)
        settings.min_name_similarity = data.get('min_name_similarity', 0.7)
        settings.search_type = data.get('search_type', 'exact')
        settings.filter_temporary_links = data.get('filter_temporary_links', True)
        settings.filter_unsafe_links = data.get('filter_unsafe_links', True)
        settings.temporary_domains = data.get('temporary_domains', ["tmp.", "temp.", "short."])
        settings.unsafe_domains = data.get('unsafe_domains', ["malware.", "phishing.", "spam."])
        settings.check_timeout = data.get('check_timeout', 5)
        settings.max_workers = data.get('max_workers', 5)
        settings.max_retries = data.get('max_retries', 2)
        settings.retry_delay = data.get('retry_delay', 0.5)
        settings.auto_replace_broken = data.get('auto_replace_broken', True)
        settings.auto_replace_missing = data.get('auto_replace_missing', True)
        settings.keep_backup_links = data.get('keep_backup_links', True)
        settings.max_alternative_urls = data.get('max_alternative_urls', 5)
        
        settings.use_ip_filtering = data.get('use_ip_filtering', True)
        settings.blacklisted_ips = data.get('blacklisted_ips', [])
        settings.whitelisted_ips = data.get('whitelisted_ips', [])
        settings.blacklisted_domains = data.get('blacklisted_domains', [])
        settings.whitelisted_domains = data.get('whitelisted_domains', [])
        settings.prioritize_whitelisted = data.get('prioritize_whitelisted', True)
        settings.blacklist_priority = data.get('blacklist_priority', -10)
        settings.whitelist_priority = data.get('whitelist_priority', 10)
        
        settings.ignore_special_chars_in_names = data.get('ignore_special_chars_in_names', True)
        settings.remove_parentheses_in_names = data.get('remove_parentheses_in_names', True)
        settings.remove_brackets_in_names = data.get('remove_brackets_in_names', True)
        settings.remove_emojis_in_names = data.get('remove_emojis_in_names', True)
        
        return settings
    
    def is_blacklisted(self, url: str) -> bool:
        if not self.use_ip_filtering:
            return False
        
        url_lower = url.lower()
        
        for ip in self.blacklisted_ips:
            if ip.lower() in url_lower:
                return True
        
        for domain in self.blacklisted_domains:
            if domain.lower() in url_lower:
                return True
        
        return False
    
    def is_whitelisted(self, url: str) -> bool:
        if not self.use_ip_filtering:
            return False
        
        url_lower = url.lower()
        
        for ip in self.whitelisted_ips:
            if ip.lower() in url_lower:
                return True
        
        for domain in self.whitelisted_domains:
            if domain.lower() in url_lower:
                return True
        
        return False
    
    def get_url_priority(self, url: str) -> int:
        if self.is_blacklisted(url):
            return self.blacklist_priority
        elif self.is_whitelisted(url):
            return self.whitelist_priority
        else:
            return 0


class LinkSourceManager:
    
    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = SystemThemeManager.get_config_dir()
        
        self.config_dir = config_dir
        self.sources_file = os.path.join(config_dir, "link_sources.json")
        self.sources: List[LinkSource] = []
        self.cache_dir = os.path.join(config_dir, "link_cache")
        
        self._ensure_config_dir()
        self._load_sources()
    
    def _ensure_config_dir(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            os.makedirs(self.cache_dir, exist_ok=True)
        except (OSError, PermissionError) as e:
            logger.error(f"Ошибка создания директории: {e}")
            raise
    
    def _load_sources(self):
        try:
            if os.path.exists(self.sources_file):
                with open(self.sources_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                if isinstance(data, list):
                    self.sources = [LinkSource.from_dict(item) for item in data]
                else:
                    self.sources = []
            else:
                self.sources = []
                self._save_sources()
                
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(f"Ошибка загрузки источников: {e}")
            self.sources = []
    
    def _save_sources(self):
        try:
            data = [source.to_dict() for source in self.sources]
            with open(self.sources_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except (IOError, OSError) as e:
            logger.error(f"Ошибка сохранения источников: {e}")
            return False
    
    def add_source(self, source: LinkSource) -> bool:
        self.sources.append(source)
        return self._save_sources()
    
    def remove_source(self, source_name: str) -> bool:
        self.sources = [s for s in self.sources if s.name != source_name]
        return self._save_sources()
    
    def update_source(self, old_name: str, new_source: LinkSource) -> bool:
        for i, source in enumerate(self.sources):
            if source.name == old_name:
                self.sources[i] = new_source
                return self._save_sources()
        return False
    
    def get_all_sources(self) -> List[LinkSource]:
        return self.sources.copy()
    
    def get_enabled_sources(self) -> List[LinkSource]:
        return [source for source in self.sources if source.enabled]
    
    def get_source_by_name(self, name: str) -> Optional[LinkSource]:
        for source in self.sources:
            if source.name == name:
                return source
        return None
    
    def _detect_encoding(self, content: bytes) -> str:
        """Определение кодировки содержимого"""
        try:
            content.decode('utf-8')
            return 'utf-8'
        except UnicodeDecodeError:
            pass
        
        try:
            content.decode('windows-1251')
            return 'windows-1251'
        except UnicodeDecodeError:
            pass
        
        try:
            content.decode('cp1251')
            return 'cp1251'
        except UnicodeDecodeError:
            pass
        
        return 'utf-8'
    
    def _fix_broken_cyrillic(self, text: str) -> str:
        """Исправление битой кириллицы в тексте"""
        if not text:
            return text
        
        try:
            if '–' in text or '—' in text or '∏' in text or 'ø' in text:
                bytes_data = text.encode('latin1')
                decoded = bytes_data.decode('utf-8', errors='replace')
                if any(c in decoded for c in ['а', 'б', 'в', 'г', 'д']):
                    return decoded
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        
        return text
    
    def load_links_from_source(self, source: LinkSource) -> List[ChannelData]:
        channels = []
        
        try:
            if source.source_type == "local":
                if os.path.exists(source.path):
                    with open(source.path, 'rb') as f:
                        raw_content = f.read()
                    
                    detected_encoding = self._detect_encoding(raw_content)
                    content = raw_content.decode(detected_encoding, errors='replace')
                    channels = self._parse_content(content, source.name)
                    
            elif source.source_type == "online":
                try:
                    response = requests.get(source.path, timeout=10, verify=False)
                    if response.status_code == 200:
                        content = response.text
                        channels = self._parse_content(content, source.name)
                except Exception as e:
                    logger.error(f"Ошибка загрузки онлайн источника {source.name}: {e}")
            
            source.total_links = len(channels)
            
        except Exception as e:
            logger.error(f"Ошибка загрузки источника {source.name}: {e}")
            return []
        
        source.last_updated = datetime.now()
        self._save_sources()
        
        return channels
    
    def _parse_content(self, content: str, source_name: str) -> List[ChannelData]:
        channels = []
        lines = content.split('\n')
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            if line.startswith('#EXTINF:'):
                channel = ChannelData()
                fixed_line = ChannelNameNormalizer.fix_encoding(line)
                channel.extinf = fixed_line
                channel.link_source = source_name
                
                if ',' in fixed_line:
                    parts = fixed_line.split(',', 1)
                    channel.name = ChannelNameNormalizer.fix_encoding(parts[1].strip())
                
                attrs_part = fixed_line.split(',')[0] if ',' in fixed_line else fixed_line
                
                tvg_id_match = re.search(r'tvg-id="([^"]*)"', attrs_part)
                if tvg_id_match:
                    channel.tvg_id = ChannelNameNormalizer.fix_encoding(tvg_id_match.group(1))
                
                logo_match = re.search(r'tvg-logo="([^"]*)"', attrs_part)
                if logo_match:
                    channel.tvg_logo = ChannelNameNormalizer.fix_encoding(logo_match.group(1))
                
                group_match = re.search(r'group-title="([^"]*)"', attrs_part)
                if group_match:
                    channel.group = ChannelNameNormalizer.fix_encoding(group_match.group(1))
                else:
                    channel.group = "Без группы"
                
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if not next_line:
                        j += 1
                        continue
                    
                    if next_line.startswith('#EXTINF:'):
                        break
                    
                    if next_line.startswith('#'):
                        fixed_next_line = ChannelNameNormalizer.fix_encoding(next_line)
                        channel.extvlcopt_lines.append(fixed_next_line)
                    else:
                        channel.url = ChannelNameNormalizer.fix_encoding(next_line.strip())
                        channel.has_url = bool(channel.url)
                        break
                    
                    j += 1
                
                channel.parse_extvlcopt_headers()
                channels.append(channel)
                i = j
            else:
                i += 1
        
        return channels
    
    def cache_links(self, source: LinkSource, channels: List[ChannelData]):
        try:
            cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(source.name.encode()).hexdigest()}.json")
            
            cache_data = {
                'channels': [ch.to_dict() for ch in channels],
                'timestamp': datetime.now().isoformat()
            }
            
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            
            source.last_cache_update = datetime.now()
            source.link_cache = {ch.name: [ch.url] for ch in channels if ch.url}
            
        except Exception as e:
            logger.error(f"Ошибка кэширования ссылок: {e}")
    
    def load_cached_links(self, source: LinkSource) -> Optional[List[ChannelData]]:
        try:
            cache_file = os.path.join(self.cache_dir, f"{hashlib.md5(source.name.encode()).hexdigest()}.json")
            
            if os.path.exists(cache_file):
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                
                channels = [ChannelData.from_dict(ch) for ch in cache_data.get('channels', [])]
                return channels
                
        except Exception as e:
            logger.error(f"Ошибка загрузки кэша: {e}")
        
        return None
    
    def search_channel(self, channel_name: str, settings: LinkReplacementSettings) -> List[ChannelData]:
        results = []
        enabled_sources = self.get_enabled_sources()
        
        if settings.ignore_special_chars_in_names:
            normalized_search_name = ChannelNameNormalizer.normalize(
                channel_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names
            )
        else:
            normalized_search_name = channel_name.lower()
        
        for source in enabled_sources:
            cached_channels = self.load_cached_links(source)
            if cached_channels is None:
                cached_channels = self.load_links_from_source(source)
                if cached_channels:
                    self.cache_links(source, cached_channels)
            
            if not cached_channels:
                continue
            
            for cached_channel in cached_channels:
                if self._is_match(normalized_search_name, cached_channel.name, settings):
                    results.append(cached_channel)
        
        results.sort(key=lambda x: (
            -self._get_source_priority(x.link_source),
            -self._get_match_score(channel_name, x.name, settings),
            -settings.get_url_priority(x.url) if x.url else 0
        ))
        
        return results[:settings.max_alternative_urls]
    
    def _is_match(self, search_name: str, channel_name: str, settings: LinkReplacementSettings) -> bool:
        if settings.ignore_special_chars_in_names:
            normalized_channel = ChannelNameNormalizer.normalize(
                channel_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names
            )
        else:
            normalized_channel = channel_name.lower()
        
        if settings.search_type == "exact":
            return search_name == normalized_channel
        
        elif settings.search_type == "similar":
            similarity = SequenceMatcher(None, search_name, normalized_channel).ratio()
            return similarity >= settings.min_name_similarity
        
        elif settings.search_type == "fuzzy":
            words1 = set(re.findall(r'\w+', search_name))
            words2 = set(re.findall(r'\w+', normalized_channel))
            
            if not words1 or not words2:
                return False
            
            common_words = words1.intersection(words2)
            similarity = len(common_words) / max(len(words1), len(words2))
            
            return similarity >= settings.min_name_similarity
        
        return False
    
    def _get_match_score(self, original_name: str, channel_name: str, settings: LinkReplacementSettings) -> float:
        if settings.ignore_special_chars_in_names:
            norm1 = ChannelNameNormalizer.normalize(
                original_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names
            )
            norm2 = ChannelNameNormalizer.normalize(
                channel_name,
                remove_parentheses=settings.remove_parentheses_in_names,
                remove_brackets=settings.remove_brackets_in_names,
                remove_emojis=settings.remove_emojis_in_names
            )
        else:
            norm1 = original_name.lower()
            norm2 = channel_name.lower()
        
        return SequenceMatcher(None, norm1, norm2).ratio()
    
    def _calculate_similarity(self, str1: str, str2: str) -> float:
        str1 = str1.lower()
        str2 = str2.lower()
        
        if str1 == str2:
            return 1.0
        
        if not str1 or not str2:
            return 0.0
        
        common = 0
        for char in set(str1):
            common += min(str1.count(char), str2.count(char))
        
        return common / max(len(str1), len(str2))
    
    def _get_source_priority(self, source_name: str) -> int:
        source = self.get_source_by_name(source_name)
        if source:
            return source.priority
        return 5


class URLUtils:
    
    @staticmethod
    def check_url_availability(url: str, timeout: int = 5, 
                              verify_ssl: bool = False) -> Tuple[bool, Optional[float], str]:
        try:
            if not url or not url.strip():
                return False, None, "Пустой URL"
            
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return False, None, "Некорректный URL"
            
            if len(url) > 2000:
                return False, None, "URL слишком длинный"
            
            if parsed.scheme not in ['http', 'https']:
                return False, None, f"Неподдерживаемый протокол: {parsed.scheme}"
            
            start_time = time.time()
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            try:
                response = requests.get(
                    url,
                    timeout=(timeout, timeout),
                    verify=verify_ssl,
                    headers=headers,
                    allow_redirects=True,
                    stream=True
                )
                
                content = b''
                for chunk in response.iter_content(chunk_size=1024):
                    content += chunk
                    if len(content) >= 1024 or len(chunk) == 0:
                        break
                
                response_time = time.time() - start_time
                
                if 200 <= response.status_code < 400:
                    return True, response_time, f"HTTP {response.status_code}"
                else:
                    return False, response_time, f"HTTP {response.status_code}"
                
            except requests.exceptions.Timeout:
                return False, timeout, "Таймаут"
            except requests.exceptions.ConnectionError:
                return False, None, "Ошибка соединения"
            except requests.exceptions.SSLError:
                try:
                    response = requests.get(
                        url,
                        timeout=timeout,
                        verify=False,
                        headers=headers,
                        allow_redirects=True,
                        stream=True
                    )
                    response_time = time.time() - start_time
                    if 200 <= response.status_code < 400:
                        return True, response_time, f"HTTP {response.status_code} (SSL ignored)"
                    else:
                        return False, response_time, f"HTTP {response.status_code}"
                except:
                    return False, None, "SSL ошибка"
            except Exception as e:
                return False, None, f"Ошибка: {str(e)[:50]}"
                
        except Exception as e:
            logger.error(f"Неожиданная ошибка проверки URL: {e}")
            return False, None, f"Ошибка: {str(e)[:50]}"
    
    @staticmethod
    def estimate_quality(response_time: float, status_code: int) -> LinkQuality:
        if status_code >= 400:
            return LinkQuality.NOT_WORKING
        return LinkQuality.WORKING
    
    @staticmethod
    def should_filter_url(url: str, temporary_domains: List[str], unsafe_domains: List[str]) -> bool:
        url_lower = url.lower()
        
        for domain in temporary_domains:
            if domain.lower() in url_lower:
                return True
        
        for domain in unsafe_domains:
            if domain.lower() in url_lower:
                return True
        
        return False


class SafeWorker(QRunnable):
    
    class WorkerSignals(QObject):
        progress = pyqtSignal(int, int, str)
        error = pyqtSignal(str)
        finished = pyqtSignal()
        result = pyqtSignal(object)
    
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = self.WorkerSignals()
        self._stop_requested = False
    
    def run(self):
        try:
            if self._stop_requested:
                return
            
            result = self.fn(*self.args, **self.kwargs)
            if not self._stop_requested:
                self.signals.result.emit(result)
                self.signals.finished.emit()
                
        except Exception as e:
            if not self._stop_requested:
                self.signals.error.emit(str(e))
                logger.error(f"Ошибка в рабочем потоке: {e}")
    
    def stop(self):
        self._stop_requested = True


class BaseWorker(QThread):
    
    progress = pyqtSignal(int, int, str)
    error = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self._stop_requested = False
        self._lock = threading.RLock()
    
    def stop(self):
        self._stop_requested = True
    
    def is_stopped(self) -> bool:
        return self._stop_requested


class LinkReplacementWorker(BaseWorker):
    
    channel_updated = pyqtSignal(ChannelData, str, str)
    
    def __init__(self, 
                 channels: List[ChannelData],
                 source_manager: LinkSourceManager,
                 settings: LinkReplacementSettings):
        super().__init__()
        self.channels = channels.copy()
        self.source_manager = source_manager
        self.settings = settings
    
    def run(self):
        try:
            total = len(self.channels)
            processed = 0
            
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.settings.max_workers, 5)
            ) as executor:
                futures = []
                for i, channel in enumerate(self.channels):
                    if self.is_stopped():
                        break
                    
                    future = executor.submit(self._process_channel, channel, i)
                    futures.append(future)
                
                for future in concurrent.futures.as_completed(futures):
                    if self.is_stopped():
                        break
                    
                    try:
                        result = future.result(timeout=30)
                        if result:
                            channel, old_url, new_url = result
                            if new_url:
                                self.channel_updated.emit(channel, old_url, new_url)
                    except concurrent.futures.TimeoutError:
                        logger.warning("Таймаут обработки канала")
                    except Exception as e:
                        logger.error(f"Ошибка обработки канала: {e}")
                    
                    processed += 1
                    progress = int((processed / total) * 100) if total > 0 else 0
                    self.progress.emit(processed, total, f"Обработано: {processed}/{total}")
            
            self.finished.emit()
            
        except Exception as e:
            self.error.emit(f"Ошибка при замене ссылок: {str(e)}")
            logger.error(f"LinkReplacementWorker ошибка: {e}")
    
    def _process_channel(self, channel: ChannelData, index: int) -> Optional[Tuple[ChannelData, str, str]]:
        if self.is_stopped():
            return None
        
        should_replace = False
        reason = ""
        
        if not channel.has_url or not channel.url or not channel.url.strip():
            should_replace = self.settings.auto_replace_missing
            reason = "Отсутствует ссылка"
        elif channel.url_status is False:
            should_replace = self.settings.auto_replace_broken
            reason = "Битая ссылка"
        
        if should_replace:
            new_url = self._find_replacement(channel, reason)
            if new_url:
                old_url = channel.url
                channel.url = new_url
                channel.add_url_to_history(old_url, new_url, reason, "auto_replacement")
                channel.last_link_replacement = datetime.now()
                return channel, old_url, new_url
        
        return None
    
    def _find_replacement(self, channel: ChannelData, reason: str) -> Optional[str]:
        try:
            alternative_channels = self.source_manager.search_channel(channel.name, self.settings)
            
            if not alternative_channels:
                return None
            
            if self.settings.prioritize_whitelisted:
                for alt_channel in alternative_channels:
                    if not alt_channel.url or not alt_channel.url.strip():
                        continue
                    
                    if self.settings.is_blacklisted(alt_channel.url):
                        continue
                    
                    if self.settings.is_whitelisted(alt_channel.url):
                        is_available, response_time, _ = URLUtils.check_url_availability(
                            alt_channel.url, 
                            self.settings.check_timeout,
                            False
                        )
                        
                        if is_available:
                            return alt_channel.url
            
            for alt_channel in alternative_channels:
                if not alt_channel.url or not alt_channel.url.strip():
                    continue
                
                if self.settings.is_blacklisted(alt_channel.url):
                    continue
                
                if URLUtils.should_filter_url(alt_channel.url, 
                                            self.settings.temporary_domains,
                                            self.settings.unsafe_domains):
                    continue
                
                is_available, response_time, _ = URLUtils.check_url_availability(
                    alt_channel.url, 
                    self.settings.check_timeout,
                    False
                )
                
                if is_available:
                    return alt_channel.url
            
            return None
            
        except Exception as e:
            logger.error(f"Ошибка поиска замены для {channel.name}: {e}")
            return None


class PlaylistHeaderManager:
    
    def __init__(self):
        self.header_lines: List[str] = []
        self.epg_sources: List[str] = []
        self.custom_attributes: Dict[str, str] = {}
        self.playlist_name: str = ""
        self.has_extm3u: bool = False
    
    def parse_header(self, content: str):
        self.header_lines = []
        self.epg_sources = []
        self.custom_attributes = {}
        self.playlist_name = ""
        self.has_extm3u = False
        
        lines = content.split('\n')
        header_end = 0
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#EXTINF:'):
                break
                
            if line.startswith('#EXTM3U'):
                self.has_extm3u = True
                self.header_lines.append(line)
                if ' ' in line:
                    attrs_line = line[8:]
                    attrs = re.findall(r'(\S+?)=["\']?([^"\'\s]+)["\']?', attrs_line)
                    for key, value in attrs:
                        if key.lower() == 'url-tvg':
                            self.epg_sources.append(value)
                        else:
                            self.custom_attributes[key] = value
                            
            elif line.startswith('#PLAYLIST:'):
                self.playlist_name = line[10:]
                self.header_lines.append(line)
            elif line.startswith('#'):
                self.header_lines.append(line)
            
            header_end = i + 1
    
    def update_epg_sources(self, sources: List[str]):
        self.epg_sources = sources
        self._update_extm3u_line()
    
    def set_playlist_name(self, name: str):
        self.playlist_name = name
        self._update_playlist_name_line()
    
    def add_custom_attribute(self, key: str, value: str):
        self.custom_attributes[key] = value
        self._update_extm3u_line()
    
    def remove_custom_attribute(self, key: str):
        if key in self.custom_attributes:
            del self.custom_attributes[key]
            self._update_extm3u_line()
    
    def _update_extm3u_line(self):
        self.header_lines = [line for line in self.header_lines if not line.startswith('#EXTM3U')]
        
        parts = ["#EXTM3U"]
        
        for epg_source in self.epg_sources:
            parts.append(f'url-tvg="{epg_source}"')
        
        for key, value in self.custom_attributes.items():
            parts.append(f'{key}="{value}"')
        
        new_line = ' '.join(parts)
        self.header_lines.insert(0, new_line)
    
    def _update_playlist_name_line(self):
        self.header_lines = [line for line in self.header_lines if not line.startswith('#PLAYLIST:')]
        
        if self.playlist_name:
            extm3u_index = -1
            for i, line in enumerate(self.header_lines):
                if line.startswith('#EXTM3U'):
                    extm3u_index = i
                    break
            
            if extm3u_index >= 0:
                self.header_lines.insert(extm3u_index + 1, f'#PLAYLIST:{self.playlist_name}')
            else:
                self.header_lines.append(f'#PLAYLIST:{self.playlist_name}')
    
    def get_header_text(self) -> str:
        if not self.header_lines:
            return "#EXTM3U\n\n"
        
        header_text = '\n'.join(self.header_lines)
        if header_text:
            header_text = header_text.rstrip('\n')
            header_text += '\n\n'
        return header_text


class BlacklistManager:
    
    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = SystemThemeManager.get_config_dir()
        
        self.config_dir = config_dir
        self.blacklist_file = os.path.join(config_dir, "blacklist.json")
        self.blacklist: List[Dict[str, str]] = []
        
        self._ensure_config_dir()
        self._load_blacklist()
    
    def _ensure_config_dir(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
        except (OSError, PermissionError) as e:
            logger.error(f"Ошибка создания директории: {e}")
            raise
    
    def _load_blacklist(self):
        try:
            if os.path.exists(self.blacklist_file):
                with open(self.blacklist_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.blacklist = data
                    else:
                        self.blacklist = []
            else:
                self.blacklist = []
                self._save_blacklist()
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.error(f"Ошибка загрузки чёрного списка: {e}")
            self.blacklist = []
    
    def _save_blacklist(self):
        try:
            with open(self.blacklist_file, 'w', encoding='utf-8') as f:
                json.dump(self.blacklist, f, ensure_ascii=False, indent=2)
            return True
        except (IOError, OSError) as e:
            logger.error(f"Ошибка сохранения чёрного списка: {e}")
            return False
    
    def add_channel(self, name: str, tvg_id: str = ""):
        for item in self.blacklist:
            if (item.get('name', '').lower() == name.lower() and
                item.get('tvg_id', '').lower() == tvg_id.lower()):
                return False
        
        self.blacklist.append({
            'name': name,
            'tvg_id': tvg_id,
            'added_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        
        return self._save_blacklist()
    
    def remove_channel(self, name: str, tvg_id: str = ""):
        for i, item in enumerate(self.blacklist):
            if (item.get('name', '').lower() == name.lower() and
                item.get('tvg_id', '').lower() == tvg_id.lower()):
                del self.blacklist[i]
                self._save_blacklist()
                return True
        return False
    
    def get_all(self) -> List[Dict[str, str]]:
        return self.blacklist.copy()
    
    def clear(self):
        self.blacklist.clear()
        self._save_blacklist()
    
    def filter_channels(self, channels: List[ChannelData]) -> Tuple[List[ChannelData], int]:
        filtered_channels = []
        removed_count = 0
        
        for channel in channels:
            should_remove = False
            
            for black_item in self.blacklist:
                name_match = black_item.get('name', '').lower() in channel.name.lower()
                tvg_id_match = (black_item.get('tvg_id', '') and 
                               black_item.get('tvg_id', '').lower() == channel.tvg_id.lower())
                
                if name_match or tvg_id_match:
                    should_remove = True
                    break
            
            if not should_remove:
                filtered_channels.append(channel)
            else:
                removed_count += 1
        
        return filtered_channels, removed_count


class URLCheckerWorker(BaseWorker):
    
    url_checked = pyqtSignal(int, bool, str, object, LinkQuality, str)
    
    def __init__(self, urls: List[str], timeout: int = 5, max_workers: int = 5):
        super().__init__()
        self.urls = urls.copy()
        self.timeout = timeout
        self.max_workers = min(max_workers, 5)
        self._results = {}
        self._processed_count = 0
        self._total_count = len(urls)
    
    def run(self):
        try:
            if self._total_count == 0:
                self.finished.emit()
                return
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {}
                
                for i, url in enumerate(self.urls):
                    if self.is_stopped():
                        break
                    
                    future = executor.submit(self.check_single_url, url, i)
                    futures[future] = i
                
                for future in concurrent.futures.as_completed(futures.keys()):
                    if self.is_stopped():
                        break
                    
                    idx = futures[future]
                    try:
                        result = future.result(timeout=self.timeout + 2)
                        
                        with self._lock:
                            self._results[idx] = result
                        
                        success = result['success'] if result['success'] is not None else False
                        response_time = result['response_time']
                        quality = result['quality']
                        
                        self.url_checked.emit(
                            result['index'], 
                            success, 
                            result['message'], 
                            response_time, 
                            quality,
                            result.get('url', '')
                        )
                        
                        with self._lock:
                            self._processed_count += 1
                            processed = self._processed_count
                        
                        self.progress.emit(processed, self._total_count, 
                                          f"Проверено: {processed}/{self._total_count}")
                        
                    except concurrent.futures.TimeoutError:
                        error_result = {
                            'index': idx, 
                            'success': False, 
                            'message': 'Таймаут проверки',
                            'response_time': None,
                            'quality': LinkQuality.NOT_WORKING,
                            'url': self.urls[idx] if idx < len(self.urls) else ''
                        }
                        
                        with self._lock:
                            self._results[idx] = error_result
                        
                        self.url_checked.emit(idx, False, 'Таймаут проверки', None, LinkQuality.NOT_WORKING, self.urls[idx] if idx < len(self.urls) else '')
                        
                        with self._lock:
                            self._processed_count += 1
                            processed = self._processed_count
                        
                        self.progress.emit(processed, self._total_count, 
                                          f"Проверено: {processed}/{self._total_count}")
                    
            self.finished.emit()
            
        except Exception as e:
            self.error.emit(f"Ошибка при проверке URL: {str(e)}")
            logger.error(f"URLCheckerWorker ошибка: {e}")
            self.finished.emit()
    
    def check_single_url(self, url: str, index: int) -> Dict[str, Any]:
        if self.is_stopped():
            return {
                'index': index, 
                'success': None, 
                'message': 'Проверка отменена',
                'response_time': None,
                'quality': LinkQuality.UNKNOWN,
                'url': url
            }
        
        if not url or not url.strip():
            return {
                'index': index, 
                'success': False, 
                'message': 'Пустой URL',
                'response_time': None,
                'quality': LinkQuality.NOT_WORKING,
                'url': url
            }
        
        try:
            parsed = urlparse(url)
            
            if not parsed.scheme or not parsed.netloc:
                return {
                    'index': index, 
                    'success': False, 
                    'message': 'Некорректный URL',
                    'response_time': None,
                    'quality': LinkQuality.NOT_WORKING,
                    'url': url
                }
            
            if len(url) > 2000:
                return {
                    'index': index,
                    'success': False,
                    'message': 'URL слишком длинный',
                    'response_time': None,
                    'quality': LinkQuality.NOT_WORKING,
                    'url': url
                }
            
            if parsed.scheme in ['http', 'https']:
                is_available, response_time, message = URLUtils.check_url_availability(
                    url, self.timeout, False
                )
                
                quality = LinkQuality.WORKING if is_available else LinkQuality.NOT_WORKING
                
                return {
                    'index': index, 
                    'success': is_available, 
                    'message': message,
                    'response_time': response_time,
                    'quality': quality,
                    'url': url
                }
            
            elif parsed.scheme in ['rtmp', 'rtsp', 'udp', 'tcp', 'rtp']:
                return {
                    'index': index, 
                    'success': True,
                    'message': f'{parsed.scheme.upper()} поток',
                    'response_time': None,
                    'quality': LinkQuality.WORKING,
                    'url': url
                }
            
            elif parsed.scheme in ['file', 'ftp']:
                return {
                    'index': index,
                    'success': None,
                    'message': f'{parsed.scheme.upper()} протокол',
                    'response_time': None,
                    'quality': LinkQuality.UNKNOWN,
                    'url': url
                }
            
            else:
                return {
                    'index': index, 
                    'success': False, 
                    'message': f'Неподдерживаемый протокол: {parsed.scheme}',
                    'response_time': None,
                    'quality': LinkQuality.NOT_WORKING,
                    'url': url
                }
                
        except Exception as e:
            logger.error(f"Ошибка проверки URL {url}: {e}")
            return {
                'index': index, 
                'success': False, 
                'message': f'Ошибка: {str(e)[:50]}',
                'response_time': None,
                'quality': LinkQuality.NOT_WORKING,
                'url': url
            }
    
    def get_results(self) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            return self._results.copy()


class M3USyntaxHighlighter(QSyntaxHighlighter):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlighting_rules = []
        
        extinf_format = QTextCharFormat()
        extinf_format.setForeground(QColor("#FF6B6B"))
        extinf_format.setFontWeight(QFont.Weight.Bold)
        self.highlighting_rules.append((r'^#EXTINF.*', extinf_format))
        
        extvlcopt_format = QTextCharFormat()
        extvlcopt_format.setForeground(QColor("#4ECDC4"))
        extvlcopt_format.setFontItalic(True)
        self.highlighting_rules.append((r'^#EXTVLCOPT.*', extvlcopt_format))
        
        extx_format = QTextCharFormat()
        extx_format.setForeground(QColor("#9B59B6"))
        self.highlighting_rules.append((r'^#EXT.*', extx_format))
        
        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#95A5A6"))
        comment_format.setFontItalic(True)
        self.highlighting_rules.append((r'^#.*', comment_format))
        
        attribute_format = QTextCharFormat()
        attribute_format.setForeground(QColor("#3498DB"))
        self.highlighting_rules.append((r'\b(tvg-id|tvg-logo|group-title)=\"[^"]*\"', attribute_format))
        
        url_format = QTextCharFormat()
        url_format.setForeground(QColor("#2ECC71"))
        url_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
        self.highlighting_rules.append((r'^(?!\s*#).*://.*', url_format))
        
        http_format = QTextCharFormat()
        http_format.setForeground(QColor("#E67E22"))
        self.highlighting_rules.append((r'^https?://[^\s]+', http_format))
        
        rtmp_format = QTextCharFormat()
        rtmp_format.setForeground(QColor("#E74C3C"))
        self.highlighting_rules.append((r'^rtmp://[^\s]+', rtmp_format))
        
        udp_format = QTextCharFormat()
        udp_format.setForeground(QColor("#F39C12"))
        self.highlighting_rules.append((r'^udp://[^\s]+', udp_format))
    
    def highlightBlock(self, text):
        for pattern, format in self.highlighting_rules:
            expression = re.compile(pattern)
            for match in expression.finditer(text):
                start = match.start()
                length = match.end() - start
                self.setFormat(start, length, format)


class EnhancedTextEdit(QPlainTextEdit):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Courier New", 10))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = M3USyntaxHighlighter(self.document())
        
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
    
    def _show_context_menu(self, position: QPoint):
        menu = QMenu(self)
        
        undo_action = QAction("Отменить", menu)
        undo_action.triggered.connect(self.undo)
        menu.addAction(undo_action)
        
        redo_action = QAction("Повторить", menu)
        redo_action.triggered.connect(self.redo)
        menu.addAction(redo_action)
        
        menu.addSeparator()
        
        cut_action = QAction("Вырезать", menu)
        cut_action.triggered.connect(self.cut)
        menu.addAction(cut_action)
        
        copy_action = QAction("Копировать", menu)
        copy_action.triggered.connect(self.copy)
        menu.addAction(copy_action)
        
        paste_action = QAction("Вставить", menu)
        paste_action.triggered.connect(self.paste)
        menu.addAction(paste_action)
        
        menu.addSeparator()
        
        select_all_action = QAction("Выделить всё", menu)
        select_all_action.triggered.connect(self.selectAll)
        menu.addAction(select_all_action)
        
        menu.addSeparator()
        
        clear_action = QAction("Очистить", menu)
        clear_action.triggered.connect(self.clear)
        menu.addAction(clear_action)
        
        menu.exec(self.mapToGlobal(position))
    
    def format_m3u(self):
        text = self.toPlainText()
        lines = text.split('\n')
        
        formatted_lines = []
        for line in lines:
            line = line.strip()
            if line:
                if line.startswith('#EXTINF'):
                    if ',' in line:
                        parts = line.split(',', 1)
                        attrs = parts[0]
                        name = parts[1].strip()
                        attrs = re.sub(r'\s+', ' ', attrs)
                        formatted_lines.append(f'{attrs},{name}')
                    else:
                        formatted_lines.append(line)
                elif line.startswith('#EXTVLCOPT'):
                    line = re.sub(r'\s+', ' ', line)
                    formatted_lines.append(line)
                elif not line.startswith('#'):
                    formatted_lines.append(line.strip())
                else:
                    formatted_lines.append(line)
        
        self.setPlainText('\n'.join(formatted_lines))


class LineEditWithContextMenu(QLineEdit):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
    
    def _show_context_menu(self, position: QPoint):
        menu = QMenu(self)
        
        undo_action = QAction("Отменить", menu)
        undo_action.triggered.connect(self.undo)
        menu.addAction(undo_action)
        
        redo_action = QAction("Повторить", menu)
        redo_action.triggered.connect(self.redo)
        menu.addAction(redo_action)
        
        menu.addSeparator()
        
        cut_action = QAction("Вырезать", menu)
        cut_action.triggered.connect(self.cut)
        menu.addAction(cut_action)
        
        copy_action = QAction("Копировать", menu)
        copy_action.triggered.connect(self.copy)
        menu.addAction(copy_action)
        
        paste_action = QAction("Вставить", menu)
        paste_action.triggered.connect(self.paste)
        menu.addAction(paste_action)
        
        menu.addSeparator()
        
        select_all_action = QAction("Выделить всё", menu)
        select_all_action.triggered.connect(self.selectAll)
        menu.addAction(select_all_action)
        
        menu.addSeparator()
        
        clear_action = QAction("Очистить", menu)
        clear_action.triggered.connect(self.clear)
        menu.addAction(clear_action)
        
        menu.exec(self.mapToGlobal(position))


class ChannelTableWidget(QTableWidget):
    
    cell_edited = pyqtSignal(int, int, str)
    url_check_requested = pyqtSignal(int)
    edit_user_agent_requested = pyqtSignal(int)
    remove_broken_url_requested = pyqtSignal(int)
    
    def __init__(self, playlist_tab=None, parent=None):
        super().__init__(parent)
        self.playlist_tab = playlist_tab
        self._block_item_changed = False
        
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.itemChanged.connect(self._on_item_changed)
        
        self.setSortingEnabled(True)
        
    def _on_item_changed(self, item):
        if self._block_item_changed:
            return
            
        row = item.row()
        column = item.column()
        new_value = item.text()
        
        self.cell_edited.emit(row, column, new_value)
    
    def _show_context_menu(self, position: QPoint):
        menu = QMenu(self)
        
        selected_rows = set()
        for item in self.selectedItems():
            selected_rows.add(item.row())
        
        if selected_rows:
            if len(selected_rows) == 1:
                row = list(selected_rows)[0]
                
                channel = None
                if self.playlist_tab and hasattr(self.playlist_tab, 'filtered_channels'):
                    if 0 <= row < len(self.playlist_tab.filtered_channels):
                        channel = self.playlist_tab.filtered_channels[row]
                
                if channel:
                    edit_ua_action = QAction("Редактировать User Agent...", menu)
                    edit_ua_action.triggered.connect(lambda: self._edit_user_agent(row))
                    menu.addAction(edit_ua_action)
                    
                    if channel.user_agent:
                        current_ua_action = QAction(f"Текущий: {channel.user_agent[:50]}...", menu)
                        current_ua_action.setEnabled(False)
                        menu.addAction(current_ua_action)
                    
                    menu.addSeparator()
                    
                    if channel.url_status is False:
                        remove_broken_action = QAction("Удалить битую ссылку", menu)
                        remove_broken_action.triggered.connect(lambda: self._remove_broken_url(row))
                        menu.addAction(remove_broken_action)
                        
                        menu.addSeparator()
                
                new_channel_action = QAction("Новый канал", menu)
                new_channel_action.triggered.connect(lambda: self._new_channel())
                menu.addAction(new_channel_action)
                
                copy_channel_action = QAction("Копировать канал", menu)
                copy_channel_action.triggered.connect(lambda: self._copy_channel(row))
                menu.addAction(copy_channel_action)
                
                cut_channel_action = QAction("Вырезать канал", menu)
                cut_channel_action.triggered.connect(lambda: self._cut_channel(row))
                menu.addAction(cut_channel_action)
                
                paste_channel_action = QAction("Вставить канал", menu)
                paste_channel_action.triggered.connect(lambda: self._paste_channel(row))
                menu.addAction(paste_channel_action)
                
                menu.addSeparator()
                
                copy_metadata_action = QAction("Копировать метаданные", menu)
                copy_metadata_action.triggered.connect(lambda: self._copy_metadata(row))
                menu.addAction(copy_metadata_action)
                
                paste_metadata_action = QAction("Вставить метаданные", menu)
                paste_metadata_action.triggered.connect(lambda: self._paste_metadata(row))
                menu.addAction(paste_metadata_action)
                
                menu.addSeparator()
                
                rename_groups_action = QAction("Пакетное переименование групп", menu)
                rename_groups_action.triggered.connect(lambda: self._rename_groups())
                menu.addAction(rename_groups_action)
                
                menu.addSeparator()
                
                add_to_blacklist_action = QAction("Добавить в чёрный список", menu)
                add_to_blacklist_action.triggered.connect(lambda: self._add_to_blacklist(row))
                menu.addAction(add_to_blacklist_action)
                
                menu.addSeparator()
                
                check_url_action = QAction("Проверить ссылку", menu)
                check_url_action.triggered.connect(lambda: self._check_single_url(row))
                menu.addAction(check_url_action)
                
                replace_link_action = QAction("Заменить ссылку...", menu)
                replace_link_action.triggered.connect(lambda: self._replace_link(row))
                menu.addAction(replace_link_action)
                
                show_link_history_action = QAction("Показать историю ссылок", menu)
                show_link_history_action.triggered.connect(lambda: self._show_link_history(row))
                menu.addAction(show_link_history_action)
                
                menu.addSeparator()
                
                delete_action = QAction("Удалить канал", menu)
                delete_action.triggered.connect(lambda: self._delete_channel(row))
                menu.addAction(delete_action)
            else:
                count = len(selected_rows)
                menu.addAction(QAction(f"Выбрано каналов: {count}", menu))
                menu.addSeparator()
                
                new_channel_action = QAction("Новый канал", menu)
                new_channel_action.triggered.connect(lambda: self._new_channel())
                menu.addAction(new_channel_action)
                
                copy_channels_action = QAction(f"Копировать каналы ({count})", menu)
                copy_channels_action.triggered.connect(self._copy_selected_channels)
                menu.addAction(copy_channels_action)
                
                cut_channels_action = QAction(f"Вырезать каналы ({count})", menu)
                cut_channels_action.triggered.connect(self._cut_selected_channels)
                menu.addAction(cut_channels_action)
                
                paste_channels_action = QAction(f"Вставить каналы ({count})", menu)
                paste_channels_action.triggered.connect(self._paste_selected_channels)
                menu.addAction(paste_channels_action)
                
                menu.addSeparator()
                
                copy_metadata_selected_action = QAction(f"Копировать метаданные ({count})", menu)
                copy_metadata_selected_action.triggered.connect(self._copy_selected_metadata)
                menu.addAction(copy_metadata_selected_action)
                
                paste_metadata_selected_action = QAction(f"Вставить метаданные ({count})", menu)
                paste_metadata_selected_action.triggered.connect(self._paste_selected_metadata)
                menu.addAction(paste_metadata_selected_action)
                
                menu.addSeparator()
                
                rename_groups_action = QAction("Пакетное переименование групп", menu)
                rename_groups_action.triggered.connect(lambda: self._rename_groups())
                menu.addAction(rename_groups_action)
                
                menu.addSeparator()
                
                delete_selected_action = QAction(f"Удалить выбранные ({count})", menu)
                delete_selected_action.triggered.connect(self._delete_selected_channels)
                menu.addAction(delete_selected_action)
                
                menu.addSeparator()
                
                check_selected_urls_action = QAction(f"Проверить ссылки ({count})", menu)
                check_selected_urls_action.triggered.connect(self._check_selected_urls)
                menu.addAction(check_selected_urls_action)
                
                replace_selected_links_action = QAction(f"Заменить ссылки ({count})...", menu)
                replace_selected_links_action.triggered.connect(self._replace_selected_links)
                menu.addAction(replace_selected_links_action)
                
                add_selected_to_blacklist_action = QAction(f"Добавить в чёрный список ({count})", menu)
                add_selected_to_blacklist_action.triggered.connect(self._add_selected_to_blacklist)
                menu.addAction(add_selected_to_blacklist_action)
                
                menu.addSeparator()
                
                remove_selected_broken_action = QAction(f"Удалить битые ссылки ({count})", menu)
                remove_selected_broken_action.triggered.connect(self._remove_selected_broken_urls)
                menu.addAction(remove_selected_broken_action)
        else:
            new_channel_action = QAction("Новый канал", menu)
            new_channel_action.triggered.connect(lambda: self._new_channel())
            menu.addAction(new_channel_action)
            
            paste_channel_action = QAction("Вставить канал", menu)
            paste_channel_action.triggered.connect(lambda: self._paste_channel(-1))
            menu.addAction(paste_channel_action)
            
            paste_metadata_action = QAction("Вставить метаданные", menu)
            paste_metadata_action.triggered.connect(lambda: self._paste_metadata(-1))
            menu.addAction(paste_metadata_action)
            
            menu.addSeparator()
            
            rename_groups_action = QAction("Пакетное переименование групп", menu)
            rename_groups_action.triggered.connect(lambda: self._rename_groups())
            menu.addAction(rename_groups_action)
        
        menu.exec(self.mapToGlobal(position))
    
    def _cut_channel(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_cut_channel'):
            self.playlist_tab._cut_channel()
    
    def _cut_selected_channels(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_cut_selected_channels'):
            self.playlist_tab._cut_selected_channels()
    
    def _remove_broken_url(self, row: int):
        self.remove_broken_url_requested.emit(row)
    
    def _remove_selected_broken_urls(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_remove_selected_broken_urls'):
            self.playlist_tab._remove_selected_broken_urls()
    
    def _edit_user_agent(self, row: int):
        self.edit_user_agent_requested.emit(row)
    
    def _replace_link(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, 'replace_single_link'):
            self.playlist_tab.replace_single_link(row)
    
    def _replace_selected_links(self):
        if self.playlist_tab and hasattr(self.playlist_tab, 'replace_selected_links'):
            self.playlist_tab.replace_selected_links()
    
    def _show_link_history(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, 'show_link_history'):
            self.playlist_tab.show_link_history(row)
    
    def _new_channel(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_new_channel'):
            self.playlist_tab._new_channel()
    
    def _copy_channel(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_copy_channel'):
            self.playlist_tab._copy_channel()
    
    def _copy_selected_channels(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_copy_selected_channels'):
            self.playlist_tab._copy_selected_channels()
    
    def _copy_metadata(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_copy_metadata'):
            self.playlist_tab._copy_metadata()
    
    def _copy_selected_metadata(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_copy_selected_metadata'):
            self.playlist_tab._copy_selected_metadata()
    
    def _paste_channel(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_paste_channel'):
            self.playlist_tab._paste_channel()
    
    def _paste_selected_channels(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_paste_selected_channels'):
            self.playlist_tab._paste_selected_channels()
    
    def _paste_metadata(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_paste_metadata'):
            self.playlist_tab._paste_metadata()
    
    def _paste_selected_metadata(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_paste_selected_metadata'):
            self.playlist_tab._paste_selected_metadata()
    
    def _rename_groups(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_rename_groups'):
            self.playlist_tab._rename_groups()
    
    def _add_to_blacklist(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_add_to_blacklist'):
            self.playlist_tab._add_to_blacklist(row)
    
    def _add_selected_to_blacklist(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_add_selected_to_blacklist'):
            self.playlist_tab._add_selected_to_blacklist()
    
    def _check_single_url(self, row: int):
        self.url_check_requested.emit(row)
    
    def _check_selected_urls(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_check_selected_urls'):
            self.playlist_tab._check_selected_urls()
    
    def _delete_channel(self, row: int):
        if self.playlist_tab and hasattr(self.playlist_tab, '_delete_channel'):
            self.playlist_tab._delete_channel(row)
    
    def _delete_selected_channels(self):
        if self.playlist_tab and hasattr(self.playlist_tab, '_delete_selected_channels'):
            self.playlist_tab._delete_selected_channels()


class URLCheckDialog(QDialog):
    
    url_check_completed = pyqtSignal(dict)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Проверка ссылок каналов")
        self.resize(500, 400)
        
        self.urls_to_check: List[str] = []
        self.results: Dict[int, Dict[str, Any]] = {}
        self.checker: Optional[URLCheckerWorker] = None
        
        self._setup_ui()
        
        self._closed_by_user = False
        self._check_started = False
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        self.info_label = QLabel("Подготовка к проверке...")
        layout.addWidget(self.info_label)
        
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        
        self.results_list = QListWidget()
        layout.addWidget(self.results_list)
        
        button_box = QDialogButtonBox()
        self.start_btn = QPushButton("Начать проверку")
        self.start_btn.clicked.connect(self.start_checking)
        
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.clicked.connect(self.stop_checking)
        self.stop_btn.setEnabled(False)
        
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.reject)
        
        self.apply_btn = QPushButton("Применить результаты")
        self.apply_btn.clicked.connect(self.apply_results)
        self.apply_btn.setEnabled(False)
        
        button_box.addButton(self.start_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(self.stop_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton(self.close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        
        layout.addWidget(button_box)
    
    def set_urls(self, urls: List[str]):
        self.urls_to_check = urls
        self.info_label.setText(f"Готово к проверке {len(urls)} ссылок")
    
    def start_checking(self):
        if not self.urls_to_check:
            QMessageBox.warning(self, "Предупреждение", "Нет ссылок для проверки")
            return
        
        self.results_list.clear()
        self.results = {}
        self.apply_btn.setEnabled(False)
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.close_btn.setEnabled(False)
        
        self._check_started = True
        self._closed_by_user = False
        
        self.checker = URLCheckerWorker(self.urls_to_check, timeout=5, max_workers=5)
        self.checker.progress.connect(self.update_progress)
        self.checker.url_checked.connect(self.on_url_checked)
        self.checker.finished.connect(self.on_checking_finished)
        self.checker.error.connect(self.on_checking_error)
        
        self.checker.start()
    
    def stop_checking(self):
        if self.checker:
            self.checker.stop()
            self.checker.wait(1000)
            self.checker = None
        
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.apply_btn.setEnabled(bool(self.results))
        self.info_label.setText("Проверка остановлена")
    
    def update_progress(self, current: int, total: int, status: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.info_label.setText(f"{status} - {current}/{total}")
    
    def on_url_checked(self, index: int, success: bool, message: str, response_time: Optional[float], quality: LinkQuality, url: str):
        if index < len(self.urls_to_check):
            url_short = url[:50] + "..." if len(url) > 50 else url
            
            if success is None:
                item = QListWidgetItem(f"⚠ {url_short}")
                item.setForeground(QColor("orange"))
                tooltip = f"{url}\n{message}"
                if response_time:
                    tooltip += f"\nВремя ответа: {response_time:.2f} сек"
                item.setToolTip(tooltip)
            elif success:
                item = QListWidgetItem(f"✓ {url_short}")
                item.setForeground(QColor("green"))
                
                tooltip = f"{url}\n{message}"
                if response_time:
                    tooltip += f"\nВремя ответа: {response_time:.2f} сек"
                item.setToolTip(tooltip)
            else:
                item = QListWidgetItem(f"✗ {url_short}")
                item.setForeground(QColor("red"))
                item.setToolTip(f"{url}\n{message}")
            
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.results_list.addItem(item)
    
    def on_checking_finished(self):
        if self._closed_by_user:
            return
            
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        
        if self.checker:
            self.results = self.checker.get_results()
            self.checker = None
        
        self.apply_btn.setEnabled(bool(self.results))
        
        working = sum(1 for r in self.results.values() if r.get('success') is True)
        not_working = sum(1 for r in self.results.values() if r.get('success') is False)
        unknown = sum(1 for r in self.results.values() if r.get('success') is None)
        
        self.info_label.setText(
            f"Проверка завершена. "
            f"Работают: {working}, не работают: {not_working}, неизвестно: {unknown}"
        )
        
        self._check_started = False
    
    def on_checking_error(self, error_message: str):
        QMessageBox.critical(self, "Ошибка", error_message)
        self.on_checking_finished()
    
    def get_results(self) -> Dict[int, Dict[str, Any]]:
        return self.results.copy()
    
    def apply_results(self):
        if self.results:
            self.url_check_completed.emit(self.results.copy())
            self.accept()
        else:
            QMessageBox.warning(self, "Предупреждение", "Нет результатов для применения")
    
    def reject(self):
        self._closed_by_user = True
        if self.checker:
            self.stop_checking()
        super().reject()
    
    def closeEvent(self, event):
        self._closed_by_user = True
        if self.checker:
            self.stop_checking()
        event.accept()


class UndoRedoManager:
    
    def __init__(self, max_steps: int = 50):
        self.max_steps = max_steps
        self.undo_stack: List[Dict[str, Any]] = []
        self.redo_stack: List[Dict[str, Any]] = []
    
    def save_state(self, channels: List[ChannelData], description: str = ""):
        if self.redo_stack:
            self.redo_stack.clear()
        
        state = {
            'channels': [ch.to_dict() for ch in channels],
            'description': description,
            'timestamp': datetime.now().strftime("%H:%M:%S")
        }
        
        self.undo_stack.append(state)
        
        if len(self.undo_stack) > self.max_steps:
            self.undo_stack.pop(0)
    
    def can_undo(self) -> bool:
        return len(self.undo_stack) > 0
    
    def can_redo(self) -> bool:
        return len(self.redo_stack) > 0
    
    def undo(self) -> Optional[Dict[str, Any]]:
        if not self.can_undo():
            return None
        
        current_state = self.undo_stack.pop()
        self.redo_stack.append(current_state)
        
        if self.undo_stack:
            return self.undo_stack[-1]
        else:
            return {
                'channels': [],
                'description': 'Начальное состояние',
                'timestamp': datetime.now().strftime("%H:%M:%S")
            }
    
    def redo(self) -> Optional[Dict[str, Any]]:
        if not self.can_redo():
            return None
        
        state = self.redo_stack.pop()
        self.undo_stack.append(state)
        
        return state


class PlaylistHeaderDialog(QDialog):
    
    def __init__(self, header_manager: PlaylistHeaderManager, parent=None):
        super().__init__(parent)
        self.header_manager = header_manager
        self.setWindowTitle("Редактор заголовка плейлиста")
        self.resize(600, 400)
        
        self._setup_ui()
        self._load_current_settings()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        general_group = QGroupBox("Основные настройки")
        general_layout = QFormLayout(general_group)
        
        self.playlist_name_edit = QLineEdit()
        general_layout.addRow("Название плейлиста:", self.playlist_name_edit)
        
        self.include_extm3u_check = QCheckBox("Включить #EXTM3U заголовок")
        self.include_extm3u_check.setChecked(True)
        general_layout.addRow(self.include_extm3u_check)
        
        layout.addWidget(general_group)
        
        epg_group = QGroupBox("Источники EPG (Electronic Program Guide)")
        epg_layout = QVBoxLayout(epg_group)
        
        self.epg_list = QListWidget()
        epg_layout.addWidget(self.epg_list)
        
        epg_btn_layout = QHBoxLayout()
        
        self.add_epg_btn = QPushButton("Добавить")
        self.add_epg_btn.clicked.connect(self._add_epg_source)
        
        self.edit_epg_btn = QPushButton("Редактировать")
        self.edit_epg_btn.clicked.connect(self._edit_epg_source)
        
        self.remove_epg_btn = QPushButton("Удалить")
        self.remove_epg_btn.clicked.connect(self._remove_epg_source)
        
        epg_btn_layout.addWidget(self.add_epg_btn)
        epg_btn_layout.addWidget(self.edit_epg_btn)
        epg_btn_layout.addWidget(self.remove_epg_btn)
        
        epg_layout.addLayout(epg_btn_layout)
        
        layout.addWidget(epg_group)
        
        custom_group = QGroupBox("Пользовательские атрибуты")
        custom_layout = QVBoxLayout(custom_group)
        
        self.custom_attrs_table = QTableWidget()
        self.custom_attrs_table.setColumnCount(2)
        self.custom_attrs_table.setHorizontalHeaderLabels(["Ключ", "Значение"])
        self.custom_attrs_table.horizontalHeader().setStretchLastSection(True)
        custom_layout.addWidget(self.custom_attrs_table)
        
        custom_btn_layout = QHBoxLayout()
        
        self.add_attr_btn = QPushButton("Добавить")
        self.add_attr_btn.clicked.connect(self._add_custom_attribute)
        
        self.edit_attr_btn = QPushButton("Редактировать")
        self.edit_attr_btn.clicked.connect(self._edit_custom_attribute)
        
        self.remove_attr_btn = QPushButton("Удалить")
        self.remove_attr_btn.clicked.connect(self._remove_custom_attribute)
        
        custom_btn_layout.addWidget(self.add_attr_btn)
        custom_btn_layout.addWidget(self.edit_attr_btn)
        custom_btn_layout.addWidget(self.remove_attr_btn)
        
        custom_layout.addLayout(custom_btn_layout)
        
        layout.addWidget(custom_group)
        
        preview_group = QGroupBox("Предпросмотр заголовка")
        preview_layout = QVBoxLayout(preview_group)
        
        self.preview_text = EnhancedTextEdit()
        self.preview_text.setMaximumHeight(100)
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)
        
        layout.addWidget(preview_group)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        
        layout.addWidget(button_box)
    
    def _load_current_settings(self):
        self.playlist_name_edit.setText(self.header_manager.playlist_name)
        
        self.epg_list.clear()
        for epg_source in self.header_manager.epg_sources:
            self.epg_list.addItem(epg_source)
        
        self._update_custom_attrs_table()
        self._update_preview()
    
    def _update_custom_attrs_table(self):
        self.custom_attrs_table.setRowCount(len(self.header_manager.custom_attributes))
        
        for i, (key, value) in enumerate(self.header_manager.custom_attributes.items()):
            self.custom_attrs_table.setItem(i, 0, QTableWidgetItem(key))
            self.custom_attrs_table.setItem(i, 1, QTableWidgetItem(value))
    
    def _update_preview(self):
        preview_text = self.header_manager.get_header_text()
        self.preview_text.setPlainText(preview_text)
    
    def _add_epg_source(self):
        epg_url, ok = QInputDialog.getText(
            self, "Добавить EPG источник",
            "Введите URL EPG источника:",
            QLineEdit.EchoMode.Normal,
            "http://example.com/epg.xml"
        )
        
        if ok and epg_url:
            self.epg_list.addItem(epg_url)
            self._update_from_ui()
    
    def _edit_epg_source(self):
        current_item = self.epg_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Предупреждение", "Выберите EPG источник для редактирования")
            return
        
        epg_url, ok = QInputDialog.getText(
            self, "Редактировать EPG источник",
            "Введите новый URL EPG источника:",
            QLineEdit.EchoMode.Normal,
            current_item.text()
        )
        
        if ok and epg_url:
            current_item.setText(epg_url)
            self._update_from_ui()
    
    def _remove_epg_source(self):
        current_item = self.epg_list.currentItem()
        if current_item:
            reply = QMessageBox.question(
                self, "Подтверждение",
                f"Удалить EPG источник '{current_item.text()}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                row = self.epg_list.row(current_item)
                self.epg_list.takeItem(row)
                self._update_from_ui()
    
    def _add_custom_attribute(self):
        key, ok1 = QInputDialog.getText(
            self, "Добавить атрибут",
            "Введите ключ атрибута:",
            QLineEdit.EchoMode.Normal
        )
        
        if ok1 and key:
            value, ok2 = QInputDialog.getText(
                self, "Добавить атрибут",
                f"Введите значение для атрибута '{key}':",
                QLineEdit.EchoMode.Normal
            )
            
            if ok2:
                self.header_manager.add_custom_attribute(key, value)
                self._update_custom_attrs_table()
                self._update_preview()
    
    def _edit_custom_attribute(self):
        current_row = self.custom_attrs_table.currentRow()
        if current_row < 0:
            QMessageBox.warning(self, "Предупреждение", "Выберите атрибут для редактирования")
            return
        
        key_item = self.custom_attrs_table.item(current_row, 0)
        value_item = self.custom_attrs_table.item(current_row, 1)
        
        if key_item and value_item:
            old_key = key_item.text()
            old_value = value_item.text()
            
            new_key, ok1 = QInputDialog.getText(
                self, "Редактировать атрибут",
                "Введите новый ключ атрибута:",
                QLineEdit.EchoMode.Normal,
                old_key
            )
            
            if ok1 and new_key:
                new_value, ok2 = QInputDialog.getText(
                    self, "Редактировать атрибут",
                    f"Введите новое значение для атрибута '{new_key}':",
                    QLineEdit.EchoMode.Normal,
                    old_value
                )
                
                if ok2:
                    self.header_manager.remove_custom_attribute(old_key)
                    self.header_manager.add_custom_attribute(new_key, new_value)
                    self._update_custom_attrs_table()
                    self._update_preview()
    
    def _remove_custom_attribute(self):
        current_row = self.custom_attrs_table.currentRow()
        if current_row < 0:
            QMessageBox.warning(self, "Предупреждение", "Выберите атрибут для удаления")
            return
        
        key_item = self.custom_attrs_table.item(current_row, 0)
        if key_item:
            key = key_item.text()
            reply = QMessageBox.question(
                self, "Подтверждение",
                f"Удалить атрибут '{key}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                self.header_manager.remove_custom_attribute(key)
                self._update_custom_attrs_table()
                self._update_preview()
    
    def _update_from_ui(self):
        epg_sources = []
        for i in range(self.epg_list.count()):
            epg_sources.append(self.epg_list.item(i).text())
        self.header_manager.update_epg_sources(epg_sources)
        
        self.header_manager.set_playlist_name(self.playlist_name_edit.text())
        
        self._update_preview()
    
    def accept(self):
        self._update_from_ui()
        super().accept()


class CopyMetadataDialog(QDialog):
    
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setWindowTitle("Копирование метаданных между плейлистами")
        self.resize(600, 400)
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        info_label = QLabel("Копирование метаданных из одного плейлиста в другой")
        info_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(info_label)
        
        source_group = QGroupBox("Исходный плейлист (откуда копировать)")
        source_layout = QVBoxLayout(source_group)
        
        self.source_tab_combo = QComboBox()
        source_layout.addWidget(QLabel("Выберите вкладку:"))
        source_layout.addWidget(self.source_tab_combo)
        
        self.source_channels_list = QListWidget()
        self.source_channels_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        source_layout.addWidget(QLabel("Выберите каналы:"))
        source_layout.addWidget(self.source_channels_list)
        
        layout.addWidget(source_group)
        
        target_group = QGroupBox("Целевой плейлист (куда вставить)")
        target_layout = QVBoxLayout(target_group)
        
        self.target_tab_combo = QComboBox()
        target_layout.addWidget(QLabel("Выберите вкладку:"))
        target_layout.addWidget(self.target_tab_combo)
        
        self.target_channels_list = QListWidget()
        self.target_channels_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        target_layout.addWidget(QLabel("Выберите каналы для обновления:"))
        target_layout.addWidget(self.target_channels_list)
        
        layout.addWidget(target_group)
        
        options_group = QGroupBox("Параметры копирования")
        options_layout = QVBoxLayout(options_group)
        
        self.copy_tvg_id_check = QCheckBox("Копировать TVG-ID")
        self.copy_tvg_id_check.setChecked(True)
        options_layout.addWidget(self.copy_tvg_id_check)
        
        self.copy_logo_check = QCheckBox("Копировать логотип")
        self.copy_logo_check.setChecked(True)
        options_layout.addWidget(self.copy_logo_check)
        
        self.copy_group_check = QCheckBox("Копировать группу")
        self.copy_group_check.setChecked(True)
        options_layout.addWidget(self.copy_group_check)
        
        self.copy_user_agent_check = QCheckBox("Копировать User-Agent")
        self.copy_user_agent_check.setChecked(True)
        options_layout.addWidget(self.copy_user_agent_check)
        
        self.copy_headers_check = QCheckBox("Копировать заголовки HTTP")
        self.copy_headers_check.setChecked(True)
        options_layout.addWidget(self.copy_headers_check)
        
        self.match_by_name_radio = QRadioButton("Сопоставлять по названию канала")
        self.match_by_name_radio.setChecked(True)
        options_layout.addWidget(self.match_by_name_radio)
        
        self.match_by_name_group_radio = QRadioButton("Сопоставлять по названию и группе")
        options_layout.addWidget(self.match_by_name_group_radio)
        
        layout.addWidget(options_group)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        
        preview_btn = QPushButton("Предпросмотр изменений")
        preview_btn.clicked.connect(self._preview_changes)
        button_box.addButton(preview_btn, QDialogButtonBox.ButtonRole.ActionRole)
        
        layout.addWidget(button_box)
        
        self._populate_tab_lists()
        
        self.source_tab_combo.currentIndexChanged.connect(self._update_source_channels)
        self.target_tab_combo.currentIndexChanged.connect(self._update_target_channels)
    
    def _populate_tab_lists(self):
        self.source_tab_combo.clear()
        self.target_tab_combo.clear()
        
        for i in range(self.main_window.tab_widget.count()):
            widget = self.main_window.tab_widget.widget(i)
            tab_name = self.main_window.tab_widget.tabText(i)
            
            if widget in self.main_window.tabs and self.main_window.tabs[widget] is not None:
                self.source_tab_combo.addItem(tab_name, i)
                self.target_tab_combo.addItem(tab_name, i)
        
        if self.source_tab_combo.count() > 0:
            self._update_source_channels()
            self._update_target_channels()
    
    def _update_source_channels(self):
        self.source_channels_list.clear()
        
        index = self.source_tab_combo.currentData()
        if index is None:
            return
        
        widget = self.main_window.tab_widget.widget(index)
        if widget not in self.main_window.tabs:
            return
        
        tab = self.main_window.tabs[widget]
        if tab is None:
            return
        
        for channel in tab.all_channels:
            item = QListWidgetItem(channel.name)
            item.setData(Qt.ItemDataRole.UserRole, channel)
            self.source_channels_list.addItem(item)
    
    def _update_target_channels(self):
        self.target_channels_list.clear()
        
        index = self.target_tab_combo.currentData()
        if index is None:
            return
        
        widget = self.main_window.tab_widget.widget(index)
        if widget not in self.main_window.tabs:
            return
        
        tab = self.main_window.tabs[widget]
        if tab is None:
            return
        
        for channel in tab.all_channels:
            item = QListWidgetItem(channel.name)
            item.setData(Qt.ItemDataRole.UserRole, channel)
            self.target_channels_list.addItem(item)
    
    def _preview_changes(self):
        source_tab_index = self.source_tab_combo.currentData()
        target_tab_index = self.target_tab_combo.currentData()
        
        if source_tab_index is None or target_tab_index is None:
            QMessageBox.warning(self, "Предупреждение", "Выберите исходную и целевую вкладки")
            return
        
        if source_tab_index == target_tab_index:
            QMessageBox.warning(self, "Предупреждение", "Исходная и целевая вкладки не должны быть одинаковыми")
            return
        
        source_channels = []
        for item in self.source_channels_list.selectedItems():
            channel = item.data(Qt.ItemDataRole.UserRole)
            if channel:
                source_channels.append(channel)
        
        target_channels = []
        for item in self.target_channels_list.selectedItems():
            channel = item.data(Qt.ItemDataRole.UserRole)
            if channel:
                target_channels.append(channel)
        
        if not source_channels:
            QMessageBox.warning(self, "Предупреждение", "Выберите каналы в исходном плейлисте")
            return
        
        if not target_channels:
            QMessageBox.warning(self, "Предупреждение", "Выберите каналы в целевом плейлисте")
            return
        
        preview_dialog = QDialog(self)
        preview_dialog.setWindowTitle("Предпросмотр изменений")
        preview_dialog.resize(800, 600)
        
        layout = QVBoxLayout(preview_dialog)
        
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["Название", "TVG-ID", "Логотип", "Группа", "User-Agent"])
        table.setRowCount(len(target_channels))
        
        match_func = None
        if self.match_by_name_radio.isChecked():
            match_func = lambda s, t: s.match_by_name(t)
        else:
            match_func = lambda s, t: s.match_by_name_and_group(t)
        
        for i, target_channel in enumerate(target_channels):
            source_channel = None
            for src_ch in source_channels:
                if match_func(src_ch, target_channel):
                    source_channel = src_ch
                    break
            
            table.setItem(i, 0, QTableWidgetItem(target_channel.name))
            
            if source_channel:
                old_tvg_id = target_channel.tvg_id or ""
                new_tvg_id = source_channel.tvg_id or ""
                if self.copy_tvg_id_check.isChecked() and new_tvg_id and old_tvg_id != new_tvg_id:
                    table.setItem(i, 1, QTableWidgetItem(f"{old_tvg_id} → {new_tvg_id}"))
                else:
                    table.setItem(i, 1, QTableWidgetItem(old_tvg_id))
                
                old_logo = target_channel.tvg_logo or ""
                new_logo = source_channel.tvg_logo or ""
                if self.copy_logo_check.isChecked() and new_logo and old_logo != new_logo:
                    table.setItem(i, 2, QTableWidgetItem(f"{old_logo} → {new_logo}"))
                else:
                    table.setItem(i, 2, QTableWidgetItem(old_logo))
                
                old_group = target_channel.group or ""
                new_group = source_channel.group or ""
                if self.copy_group_check.isChecked() and new_group and old_group != new_group:
                    table.setItem(i, 3, QTableWidgetItem(f"{old_group} → {new_group}"))
                else:
                    table.setItem(i, 3, QTableWidgetItem(old_group))
                
                old_ua = target_channel.user_agent or ""
                new_ua = source_channel.user_agent or ""
                if self.copy_user_agent_check.isChecked() and new_ua and old_ua != new_ua:
                    table.setItem(i, 4, QTableWidgetItem(f"{old_ua} → {new_ua}"))
                else:
                    table.setItem(i, 4, QTableWidgetItem(old_ua))
            else:
                table.setItem(i, 1, QTableWidgetItem(target_channel.tvg_id or ""))
                table.setItem(i, 2, QTableWidgetItem(target_channel.tvg_logo or ""))
                table.setItem(i, 3, QTableWidgetItem(target_channel.group or ""))
                table.setItem(i, 4, QTableWidgetItem(target_channel.user_agent or ""))
        
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(preview_dialog.reject)
        layout.addWidget(button_box)
        
        preview_dialog.exec()
    
    def get_selected_data(self):
        source_tab_index = self.source_tab_combo.currentData()
        target_tab_index = self.target_tab_combo.currentData()
        
        if source_tab_index is None or target_tab_index is None:
            return None
        
        source_channels = []
        for item in self.source_channels_list.selectedItems():
            channel = item.data(Qt.ItemDataRole.UserRole)
            if channel:
                source_channels.append(channel)
        
        target_channels = []
        for item in self.target_channels_list.selectedItems():
            channel = item.data(Qt.ItemDataRole.UserRole)
            if channel:
                target_channels.append(channel)
        
        return {
            'source_tab_index': source_tab_index,
            'target_tab_index': target_tab_index,
            'source_channels': source_channels,
            'target_channels': target_channels,
            'copy_tvg_id': self.copy_tvg_id_check.isChecked(),
            'copy_logo': self.copy_logo_check.isChecked(),
            'copy_group': self.copy_group_check.isChecked(),
            'copy_user_agent': self.copy_user_agent_check.isChecked(),
            'copy_headers': self.copy_headers_check.isChecked(),
            'match_by_name': self.match_by_name_radio.isChecked(),
            'match_by_name_and_group': self.match_by_name_group_radio.isChecked()
        }


class LinkReplacementDialog(QDialog):
    
    replacement_completed = pyqtSignal(list)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Замена ссылок")
        self.resize(700, 500)
        
        self.channels_to_process: List[ChannelData] = []
        self.source_manager: Optional[LinkSourceManager] = None
        self.settings: Optional[LinkReplacementSettings] = None
        self.worker: Optional[LinkReplacementWorker] = None
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        self.info_label = QLabel("Подготовка к замене ссылок...")
        layout.addWidget(self.info_label)
        
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        
        self.results_list = QListWidget()
        layout.addWidget(self.results_list)
        
        stats_layout = QHBoxLayout()
        
        self.stats_label = QLabel("Статистика: ")
        stats_layout.addWidget(self.stats_label)
        
        stats_layout.addStretch()
        
        layout.addLayout(stats_layout)
        
        button_box = QDialogButtonBox()
        
        self.start_btn = QPushButton("Начать замену")
        self.start_btn.clicked.connect(self.start_replacement)
        
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.clicked.connect(self.stop_replacement)
        self.stop_btn.setEnabled(False)
        
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.reject)
        
        self.apply_btn = QPushButton("Применить изменения")
        self.apply_btn.clicked.connect(self.accept)
        self.apply_btn.setEnabled(False)
        
        button_box.addButton(self.start_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(self.stop_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton(self.close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        
        layout.addWidget(button_box)
    
    def set_data(self, 
                 channels: List[ChannelData], 
                 source_manager: LinkSourceManager, 
                 settings: LinkReplacementSettings):
        self.channels_to_process = channels.copy()
        self.source_manager = source_manager
        self.settings = settings
        
        broken_count = sum(1 for ch in channels if ch.url_status is False)
        missing_count = sum(1 for ch in channels if not ch.has_url or not ch.url or not ch.url.strip())
        
        self.info_label.setText(
            f"Готово к обработке {len(channels)} каналов\n"
            f"Битых: {broken_count}, отсутствующих: {missing_count}"
        )
    
    def start_replacement(self):
        if not self.channels_to_process or not self.source_manager or not self.settings:
            QMessageBox.warning(self, "Предупреждение", "Нет данных для обработки")
            return
        
        self.results_list.clear()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.close_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        
        self.worker = LinkReplacementWorker(
            self.channels_to_process,
            self.source_manager,
            self.settings
        )
        
        self.worker.progress.connect(self.update_progress)
        self.worker.channel_updated.connect(self.on_channel_updated)
        self.worker.finished.connect(self.on_replacement_finished)
        self.worker.error.connect(self.on_replacement_error)
        
        self.worker.start()
    
    def stop_replacement(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None
        
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)
        
        self.info_label.setText("Замена остановлена")
    
    def update_progress(self, current: int, total: int, status: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.info_label.setText(f"{status} - {current}/{total}")
    
    def on_channel_updated(self, channel: ChannelData, old_url: str, new_url: str):
        item_text = f"✓ {channel.name}: заменена ссылка"
        
        if len(old_url) > 30:
            old_url_display = old_url[:27] + "..."
        else:
            old_url_display = old_url
        
        if len(new_url) > 30:
            new_url_display = new_url[:27] + "..."
        else:
            new_url_display = new_url
        
        tooltip = f"Старая: {old_url}\nНовая: {new_url}\nПричина: {channel.url_history[-1]['reason'] if channel.url_history else 'Неизвестно'}"
        
        item = QListWidgetItem(item_text)
        item.setForeground(QColor("green"))
        item.setToolTip(tooltip)
        item.setData(Qt.ItemDataRole.UserRole, channel)
        
        self.results_list.addItem(item)
        
        replaced_count = self.results_list.count()
        total = len(self.channels_to_process)
        self.stats_label.setText(f"Статистика: заменено {replaced_count}/{total}")
    
    def on_replacement_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)
        
        replaced_count = self.results_list.count()
        total = len(self.channels_to_process)
        
        self.info_label.setText(f"Замена завершена. Заменено {replaced_count}/{total} ссылок")
        self.stats_label.setText(f"Статистика: заменено {replaced_count}/{total}")
    
    def on_replacement_error(self, error_message: str):
        QMessageBox.critical(self, "Ошибка", error_message)
        self.on_replacement_finished()
    
    def get_replaced_channels(self) -> List[ChannelData]:
        replaced_channels = []
        
        for i in range(self.results_list.count()):
            item = self.results_list.item(i)
            channel = item.data(Qt.ItemDataRole.UserRole)
            if channel:
                replaced_channels.append(channel)
        
        return replaced_channels
    
    def accept(self):
        replaced_channels = self.get_replaced_channels()
        if replaced_channels:
            self.replacement_completed.emit(replaced_channels)
            super().accept()
        else:
            QMessageBox.warning(self, "Предупреждение", "Нет заменённых ссылок для применения")
    
    def reject(self):
        if self.worker:
            self.stop_replacement()
        super().reject()
    
    def closeEvent(self, event):
        if self.worker:
            self.stop_replacement()
        event.accept()


class LinkSourceManagerDialog(QDialog):
    
    sources_updated = pyqtSignal()
    
    def __init__(self, source_manager: LinkSourceManager, parent=None):
        super().__init__(parent)
        self.source_manager = source_manager
        self.setWindowTitle("Менеджер источников ссылок")
        self.resize(900, 600)
        
        self._setup_ui()
        self._load_sources()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        self.sources_table = QTableWidget()
        self.sources_table.setColumnCount(8)
        self.sources_table.setHorizontalHeaderLabels([
            "Вкл", "Название", "Тип", "Путь/URL", "Ссылок", "Приоритет", "Теги", "Обновлено"
        ])
        self.sources_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sources_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        
        header = self.sources_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        
        layout.addWidget(self.sources_table)
        
        btn_layout = QHBoxLayout()
        
        self.add_btn = QPushButton("Добавить")
        self.add_btn.clicked.connect(self._add_source)
        
        self.edit_btn = QPushButton("Редактировать")
        self.edit_btn.clicked.connect(self._edit_source)
        
        self.remove_btn = QPushButton("Удалить")
        self.remove_btn.clicked.connect(self._remove_source)
        
        self.refresh_btn = QPushButton("Обновить всё")
        self.refresh_btn.clicked.connect(self._refresh_all)
        
        self.import_btn = QPushButton("Импорт")
        self.import_btn.clicked.connect(self._import_sources)
        
        self.export_btn = QPushButton("Экспорт")
        self.export_btn.clicked.connect(self._export_sources)
        
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.edit_btn)
        btn_layout.addWidget(self.remove_btn)
        btn_layout.addWidget(self.refresh_btn)
        btn_layout.addWidget(self.import_btn)
        btn_layout.addWidget(self.export_btn)
        
        layout.addLayout(btn_layout)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def _load_sources(self):
        sources = self.source_manager.get_all_sources()
        self.sources_table.setRowCount(len(sources))
        
        for i, source in enumerate(sources):
            enabled_item = QTableWidgetItem()
            enabled_item.setCheckState(Qt.CheckState.Checked if source.enabled else Qt.CheckState.Unchecked)
            self.sources_table.setItem(i, 0, enabled_item)
            
            self.sources_table.setItem(i, 1, QTableWidgetItem(source.name))
            
            type_text = "Локальный" if source.source_type == "local" else "Онлайн"
            self.sources_table.setItem(i, 2, QTableWidgetItem(type_text))
            
            path_item = QTableWidgetItem(source.path)
            path_item.setToolTip(source.path)
            self.sources_table.setItem(i, 3, path_item)
            
            self.sources_table.setItem(i, 4, QTableWidgetItem(str(source.total_links)))
            
            self.sources_table.setItem(i, 5, QTableWidgetItem(str(source.priority)))
            
            tags_text = ", ".join(source.tags)
            self.sources_table.setItem(i, 6, QTableWidgetItem(tags_text))
            
            date_text = source.last_updated.strftime("%Y-%m-%d %H:%M") if source.last_updated else "Никогда"
            self.sources_table.setItem(i, 7, QTableWidgetItem(date_text))
    
    def _add_source(self):
        dialog = LinkSourceEditDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            source = dialog.get_source()
            if source:
                if self.source_manager.add_source(source):
                    self._load_sources()
                    self.sources_updated.emit()
                    QMessageBox.information(self, "Успех", "Источник добавлен")
                else:
                    QMessageBox.warning(self, "Ошибка", "Не удалось добавить источник")
    
    def _edit_source(self):
        selected_row = self.sources_table.currentRow()
        if selected_row < 0:
            QMessageBox.warning(self, "Предупреждение", "Выберите источник для редактирования")
            return
        
        source_name = self.sources_table.item(selected_row, 1).text()
        source = self.source_manager.get_source_by_name(source_name)
        
        if not source:
            QMessageBox.warning(self, "Ошибка", "Источник не найден")
            return
        
        dialog = LinkSourceEditDialog(self, source)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_source = dialog.get_source()
            if new_source:
                if self.source_manager.update_source(source.name, new_source):
                    self._load_sources()
                    self.sources_updated.emit()
                    QMessageBox.information(self, "Успех", "Источник обновлен")
                else:
                    QMessageBox.warning(self, "Ошибка", "Не удалось обновить источник")
    
    def _remove_source(self):
        selected_row = self.sources_table.currentRow()
        if selected_row < 0:
            QMessageBox.warning(self, "Предупреждение", "Выберите источник для удаления")
            return
        
        source_name = self.sources_table.item(selected_row, 1).text()
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Удалить источник '{source_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.source_manager.remove_source(source_name):
                self._load_sources()
                self.sources_updated.emit()
                QMessageBox.information(self, "Успех", "Источник удален")
            else:
                QMessageBox.warning(self, "Ошибка", "Не удалось удалить источник")
    
    def _refresh_all(self):
        sources = self.source_manager.get_enabled_sources()
        
        if not sources:
            QMessageBox.information(self, "Информация", "Нет включенных источников для обновления")
            return
        
        progress_dialog = QProgressDialog("Обновление источников...", "Отмена", 0, len(sources), self)
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        
        for i, source in enumerate(sources):
            progress_dialog.setValue(i)
            progress_dialog.setLabelText(f"Обновление: {source.name}")
            
            if progress_dialog.wasCanceled():
                break
            
            try:
                self.source_manager.load_links_from_source(source)
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Ошибка обновления источника {source.name}: {e}")
        
        progress_dialog.setValue(len(sources))
        
        self._load_sources()
        self.sources_updated.emit()
        QMessageBox.information(self, "Успех", "Источники обновлены")
    
    def _import_sources(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Импорт источников", "",
            "JSON файлы (*.json);;Все файлы (*.*)"
        )
        
        if not filepath:
            return
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if isinstance(data, list):
                imported = 0
                for item in data:
                    source = LinkSource.from_dict(item)
                    if source.name:
                        self.source_manager.add_source(source)
                        imported += 1
                
                self._load_sources()
                self.sources_updated.emit()
                QMessageBox.information(self, "Успех", f"Импортировано {imported} источников")
            else:
                QMessageBox.warning(self, "Ошибка", "Некорректный формат файла")
        
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось импортировать файл:\n{str(e)}")
    
    def _export_sources(self):
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Экспорт источников", "link_sources.json",
            "JSON файлы (*.json);;Все файлы (*.*)"
        )
        
        if not filepath:
            return
        
        try:
            sources = self.source_manager.get_all_sources()
            data = [source.to_dict() for source in sources]
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            QMessageBox.information(self, "Успех", f"Экспортировано {len(sources)} источников")
        
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось экспортировать файл:\n{str(e)}")
    
    def closeEvent(self, event):
        sources = self.source_manager.get_all_sources()
        
        for i in range(self.sources_table.rowCount()):
            if i < len(sources):
                enabled_item = self.sources_table.item(i, 0)
                if enabled_item:
                    sources[i].enabled = enabled_item.checkState() == Qt.CheckState.Checked
        
        self.source_manager._save_sources()
        event.accept()


class LinkSourceEditDialog(QDialog):
    
    def __init__(self, parent=None, source: Optional[LinkSource] = None):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle("Правка источника" if source else "Добавление источника")
        self.resize(500, 400)
        
        self._setup_ui()
        if source:
            self._load_source_data()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        form_layout = QFormLayout()
        
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Введите название источника")
        form_layout.addRow("Название:", self.name_edit)
        
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Локальный файл", "Онлайн источник"])
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form_layout.addRow("Тип источника:", self.type_combo)
        
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Введите путь к файлу или URL")
        form_layout.addRow("Путь/URL:", self.path_edit)
        
        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.clicked.connect(self._browse_file)
        form_layout.addRow("", self.browse_btn)
        
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 10)
        self.priority_spin.setValue(5)
        self.priority_spin.setToolTip("1 - низкий приоритет, 10 - высокий приоритет")
        form_layout.addRow("Приоритет (1-10):", self.priority_spin)
        
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("через запятую, например: спортивные, новости, фильмы")
        form_layout.addRow("Теги:", self.tags_edit)
        
        self.description_edit = QTextEdit()
        self.description_edit.setMaximumHeight(80)
        self.description_edit.setPlaceholderText("Введите описание источника")
        form_layout.addRow("Описание:", self.description_edit)
        
        options_group = QGroupBox("Дополнительные настройки")
        options_layout = QVBoxLayout(options_group)
        
        self.enabled_check = QCheckBox("Включен")
        self.enabled_check.setChecked(True)
        options_layout.addWidget(self.enabled_check)
        
        self.auto_update_check = QCheckBox("Автоматическое обновление")
        options_layout.addWidget(self.auto_update_check)
        
        update_layout = QHBoxLayout()
        update_layout.addWidget(QLabel("Интервал обновления (часы):"))
        
        self.update_interval_spin = QSpinBox()
        self.update_interval_spin.setRange(1, 168)
        self.update_interval_spin.setValue(24)
        update_layout.addWidget(self.update_interval_spin)
        update_layout.addStretch()
        
        options_layout.addLayout(update_layout)
        layout.addLayout(form_layout)
        layout.addWidget(options_group)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        
        if self.source:
            test_btn = QPushButton("Тестировать")
            test_btn.clicked.connect(self._test_source)
            button_box.addButton(test_btn, QDialogButtonBox.ButtonRole.ActionRole)
        
        layout.addWidget(button_box)
    
    def _load_source_data(self):
        if not self.source:
            return
        
        self.name_edit.setText(self.source.name)
        
        if self.source.source_type == "local":
            self.type_combo.setCurrentIndex(0)
        else:
            self.type_combo.setCurrentIndex(1)
        
        self.path_edit.setText(self.source.path)
        self.priority_spin.setValue(self.source.priority)
        self.tags_edit.setText(", ".join(self.source.tags))
        self.description_edit.setText(self.source.description)
        self.enabled_check.setChecked(self.source.enabled)
        self.auto_update_check.setChecked(self.source.auto_update)
        self.update_interval_spin.setValue(self.source.update_interval_hours)
    
    def _on_type_changed(self, index: int):
        if index == 0:
            self.browse_btn.setEnabled(True)
            self.path_edit.setPlaceholderText("Введите путь к файлу")
        else:
            self.browse_btn.setEnabled(False)
            self.path_edit.setPlaceholderText("Введите URL источника")
    
    def _browse_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл плейлиста", "",
            "M3U файлы (*.m3u *.m3u8);;Все файлы (*.*)"
        )
        
        if filepath:
            self.path_edit.setText(filepath)
    
    def _test_source(self):
        if not self._validate_form():
            return
        
        source = self._create_source_from_form()
        
        progress_dialog = QProgressDialog("Тестирование источника...", "Отмена", 0, 0, self)
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        progress_dialog.show()
        
        try:
            channels = self.parent().source_manager.load_links_from_source(source)
            progress_dialog.close()
            
            if channels:
                QMessageBox.information(
                    self, "Успех", 
                    f"Источник успешно загружен.\n"
                    f"Найдено каналов: {len(channels)}\n"
                    f"Рабочих ссылок: {sum(1 for ch in channels if ch.has_url and ch.url)}"
                )
            else:
                QMessageBox.warning(self, "Предупреждение", "Не удалось загрузить каналы из источника")
        
        except Exception as e:
            progress_dialog.close()
            QMessageBox.critical(self, "Ошибка", f"Ошибка тестирования источника:\n{str(e)}")
    
    def _validate_form(self) -> bool:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Предупреждение", "Введите название источника")
            self.name_edit.setFocus()
            return False
        
        if not self.path_edit.text().strip():
            QMessageBox.warning(self, "Предупреждение", "Введите путь или URL источника")
            self.path_edit.setFocus()
            return False
        
        if self.type_combo.currentIndex() == 0:
            if not os.path.exists(self.path_edit.text().strip()):
                reply = QMessageBox.question(
                    self, "Предупреждение",
                    "Указанный файл не существует. Продолжить?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply == QMessageBox.StandardButton.No:
                    self.path_edit.setFocus()
                    return False
        
        return True
    
    def _create_source_from_form(self) -> LinkSource:
        source = LinkSource() if not self.source else self.source.copy()
        
        source.name = self.name_edit.text().strip()
        source.source_type = "local" if self.type_combo.currentIndex() == 0 else "online"
        source.path = self.path_edit.text().strip()
        source.priority = self.priority_spin.value()
        source.tags = [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]
        source.description = self.description_edit.toPlainText().strip()
        source.enabled = self.enabled_check.isChecked()
        source.auto_update = self.auto_update_check.isChecked()
        source.update_interval_hours = self.update_interval_spin.value()
        
        return source
    
    def get_source(self) -> Optional[LinkSource]:
        if not self._validate_form():
            return None
        
        return self._create_source_from_form()
    
    def accept(self):
        if self._validate_form():
            super().accept()


class LinkReplacementSettingsDialog(QDialog):
    
    settings_changed = pyqtSignal(LinkReplacementSettings)
    
    def __init__(self, settings: LinkReplacementSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Настройки замены ссылок")
        self.resize(800, 600)
        
        self._setup_ui()
        self._load_settings()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        self.tab_widget = QTabWidget()
        
        basic_tab = QWidget()
        basic_layout = QVBoxLayout(basic_tab)
        
        search_group = QGroupBox("Настройки поиска")
        search_layout = QFormLayout(search_group)
        
        self.search_type_combo = QComboBox()
        self.search_type_combo.addItems(["Точный поиск", "Похожий поиск", "Нечеткий поиск"])
        self.search_type_combo.currentIndexChanged.connect(self._on_search_type_changed)
        search_layout.addRow("Тип поиска:", self.search_type_combo)
        
        self.match_threshold_spin = QDoubleSpinBox()
        self.match_threshold_spin.setRange(0, 100)
        self.match_threshold_spin.setDecimals(1)
        self.match_threshold_spin.setSuffix("%")
        search_layout.addRow("Порог совпадения:", self.match_threshold_spin)
        
        self.min_similarity_spin = QDoubleSpinBox()
        self.min_similarity_spin.setRange(0, 1)
        self.min_similarity_spin.setDecimals(2)
        self.min_similarity_spin.setSingleStep(0.05)
        self.min_similarity_spin.setEnabled(False)
        search_layout.addRow("Минимальная схожесть:", self.min_similarity_spin)
        
        self.use_fuzzy_check = QCheckBox("Использовать нечеткий поиск")
        self.use_fuzzy_check.setChecked(True)
        search_layout.addRow(self.use_fuzzy_check)
        
        basic_layout.addWidget(search_group)
        
        normalization_group = QGroupBox("Нормализация названий каналов")
        normalization_layout = QVBoxLayout(normalization_group)
        
        self.ignore_special_chars_check = QCheckBox("Игнорировать специальные символы при поиске")
        self.ignore_special_chars_check.setChecked(True)
        normalization_layout.addWidget(self.ignore_special_chars_check)
        
        self.remove_parentheses_check = QCheckBox("Игнорировать информацию о качестве в круглых скобках (720p, 1080p и т.д.)")
        self.remove_parentheses_check.setChecked(True)
        normalization_layout.addWidget(self.remove_parentheses_check)
        
        self.remove_brackets_check = QCheckBox("Игнорировать определенные теги в квадратных скобках ([Geo-blocked], [Not 24/7])")
        self.remove_brackets_check.setChecked(True)
        normalization_layout.addWidget(self.remove_brackets_check)
        
        self.remove_emojis_check = QCheckBox("Игнорировать эмодзи и специальные символы")
        self.remove_emojis_check.setChecked(True)
        normalization_layout.addWidget(self.remove_emojis_check)
        
        info_label = QLabel(
            "Пример: 'A24 Ⓨ (1080p) [PL]' → 'A24 [PL]'\n"
            "Сохраняется информация о странах ([PL], [LV], [AM] и т.д.) и региональных каналах (Астрахань, Тюмень)"
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: gray; font-style: italic;")
        normalization_layout.addWidget(info_label)
        
        basic_layout.addWidget(normalization_group)
        
        replace_group = QGroupBox("Настройки замены")
        replace_layout = QVBoxLayout(replace_group)
        
        self.auto_broken_check = QCheckBox("Автозамена битых ссылок")
        self.auto_broken_check.setChecked(True)
        replace_layout.addWidget(self.auto_broken_check)
        
        self.auto_missing_check = QCheckBox("Автозамена отсутствующих ссылок")
        self.auto_missing_check.setChecked(True)
        replace_layout.addWidget(self.auto_missing_check)
        
        self.keep_backup_check = QCheckBox("Сохранять резервные ссылки")
        self.keep_backup_check.setChecked(True)
        replace_layout.addWidget(self.keep_backup_check)
        
        self.max_alt_spin = QSpinBox()
        self.max_alt_spin.setRange(1, 20)
        replace_layout.addWidget(QLabel("Максимум альтернативных ссылок:"))
        replace_layout.addWidget(self.max_alt_spin)
        
        basic_layout.addWidget(replace_group)
        basic_layout.addStretch()
        
        self.tab_widget.addTab(basic_tab, "Основные")
        
        filters_tab = QWidget()
        filters_layout = QVBoxLayout(filters_tab)
        
        filter_group = QGroupBox("Фильтры ссылок")
        filter_layout = QFormLayout(filter_group)
        
        self.filter_temp_check = QCheckBox("Фильтровать временные ссылки")
        filter_layout.addRow(self.filter_temp_check)
        
        self.temp_domains_edit = QLineEdit()
        self.temp_domains_edit.setPlaceholderText("tmp., temp., short.")
        filter_layout.addRow("Временные домены:", self.temp_domains_edit)
        
        self.filter_unsafe_check = QCheckBox("Фильтровать небезопасные ссылки")
        filter_layout.addRow(self.filter_unsafe_check)
        
        self.unsafe_domains_edit = QLineEdit()
        self.unsafe_domains_edit.setPlaceholderText("malware., phishing., spam.")
        filter_layout.addRow("Небезопасные домены:", self.unsafe_domains_edit)
        
        filters_layout.addWidget(filter_group)
        
        ip_filter_group = QGroupBox("Фильтрация по IP/доменам")
        ip_filter_layout = QFormLayout(ip_filter_group)
        
        self.use_ip_filter_check = QCheckBox("Использовать фильтрацию по IP/доменам")
        ip_filter_layout.addRow(self.use_ip_filter_check)
        
        self.prioritize_whitelist_check = QCheckBox("Приоритет ссылок из белого списка")
        ip_filter_layout.addRow(self.prioritize_whitelist_check)
        
        blacklist_label = QLabel("Чёрный список IP/доменов:")
        blacklist_label.setStyleSheet("font-weight: bold; color: red;")
        ip_filter_layout.addRow(blacklist_label)
        
        self.blacklist_ips_edit = QTextEdit()
        self.blacklist_ips_edit.setMaximumHeight(80)
        self.blacklist_ips_edit.setPlaceholderText("Введите IP адреса или домены (каждый с новой строки)\nПример:\n176.107.219.20\n95.66.188.74\niam-profi.ru")
        ip_filter_layout.addRow("Чёрный список:", self.blacklist_ips_edit)
        
        whitelist_label = QLabel("Белый список IP/доменов:")
        whitelist_label.setStyleSheet("font-weight: bold; color: green;")
        ip_filter_layout.addRow(whitelist_label)
        
        self.whitelist_ips_edit = QTextEdit()
        self.whitelist_ips_edit.setMaximumHeight(80)
        self.whitelist_ips_edit.setPlaceholderText("Введите IP адреса или домены (каждый с новой строки)\nПример:\n158.101.222.193\ncdn.ntv.ru\norigin5.mediacdn.ru")
        ip_filter_layout.addRow("Белый список:", self.whitelist_ips_edit)
        
        priority_layout = QHBoxLayout()
        priority_layout.addWidget(QLabel("Приоритет белого списка:"))
        
        self.whitelist_priority_spin = QSpinBox()
        self.whitelist_priority_spin.setRange(-100, 100)
        self.whitelist_priority_spin.setValue(10)
        priority_layout.addWidget(self.whitelist_priority_spin)
        
        priority_layout.addWidget(QLabel("Приоритет черного списка:"))
        
        self.blacklist_priority_spin = QSpinBox()
        self.blacklist_priority_spin.setRange(-100, 100)
        self.blacklist_priority_spin.setValue(-10)
        priority_layout.addWidget(self.blacklist_priority_spin)
        
        priority_layout.addStretch()
        ip_filter_layout.addRow(priority_layout)
        
        filters_layout.addWidget(ip_filter_group)
        filters_layout.addStretch()
        
        self.tab_widget.addTab(filters_tab, "Фильтры")
        
        check_tab = QWidget()
        check_layout = QVBoxLayout(check_tab)
        
        timeout_group = QGroupBox("Таймауты и повторные попытки")
        timeout_layout = QFormLayout(timeout_group)
        
        self.check_timeout_spin = QSpinBox()
        self.check_timeout_spin.setRange(1, 10)
        self.check_timeout_spin.setSuffix(" сек")
        timeout_layout.addRow("Таймаут проверки:", self.check_timeout_spin)
        
        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(0, 3)
        timeout_layout.addRow("Повторных попыток:", self.max_retries_spin)
        
        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0, 2)
        self.retry_delay_spin.setDecimals(1)
        self.retry_delay_spin.setSuffix(" сек")
        timeout_layout.addRow("Задержка повтора:", self.retry_delay_spin)
        
        check_layout.addWidget(timeout_group)
        
        workers_group = QGroupBox("Параметры многопоточности")
        workers_layout = QFormLayout(workers_group)
        
        self.max_workers_spin = QSpinBox()
        self.max_workers_spin.setRange(1, 10)
        workers_layout.addRow("Максимум потоков:", self.max_workers_spin)
        
        check_layout.addWidget(workers_group)
        check_layout.addStretch()
        
        self.tab_widget.addTab(check_tab, "Проверка")
        
        layout.addWidget(self.tab_widget)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.Reset
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_box.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(self._reset_to_default)
        
        layout.addWidget(button_box)
    
    def _on_search_type_changed(self, index):
        self.min_similarity_spin.setEnabled(index == 1)
    
    def _load_settings(self):
        self.match_threshold_spin.setValue(self.settings.match_threshold_percent)
        self.use_fuzzy_check.setChecked(self.settings.use_fuzzy_matching)
        self.min_similarity_spin.setValue(self.settings.min_name_similarity)
        
        if self.settings.search_type == "exact":
            self.search_type_combo.setCurrentIndex(0)
        elif self.settings.search_type == "similar":
            self.search_type_combo.setCurrentIndex(1)
        else:
            self.search_type_combo.setCurrentIndex(2)
        
        self.ignore_special_chars_check.setChecked(self.settings.ignore_special_chars_in_names)
        self.remove_parentheses_check.setChecked(self.settings.remove_parentheses_in_names)
        self.remove_brackets_check.setChecked(self.settings.remove_brackets_in_names)
        self.remove_emojis_check.setChecked(self.settings.remove_emojis_in_names)
        
        self.filter_temp_check.setChecked(self.settings.filter_temporary_links)
        self.temp_domains_edit.setText(", ".join(self.settings.temporary_domains))
        self.filter_unsafe_check.setChecked(self.settings.filter_unsafe_links)
        self.unsafe_domains_edit.setText(", ".join(self.settings.unsafe_domains))
        
        self.check_timeout_spin.setValue(self.settings.check_timeout)
        self.max_workers_spin.setValue(self.settings.max_workers)
        self.max_retries_spin.setValue(self.settings.max_retries)
        self.retry_delay_spin.setValue(self.settings.retry_delay)
        
        self.auto_broken_check.setChecked(self.settings.auto_replace_broken)
        self.auto_missing_check.setChecked(self.settings.auto_replace_missing)
        self.keep_backup_check.setChecked(self.settings.keep_backup_links)
        self.max_alt_spin.setValue(self.settings.max_alternative_urls)
        
        self.use_ip_filter_check.setChecked(self.settings.use_ip_filtering)
        self.prioritize_whitelist_check.setChecked(self.settings.prioritize_whitelisted)
        self.blacklist_ips_edit.setPlainText("\n".join(self.settings.blacklisted_ips + self.settings.blacklisted_domains))
        self.whitelist_ips_edit.setPlainText("\n".join(self.settings.whitelisted_ips + self.settings.whitelisted_domains))
        self.whitelist_priority_spin.setValue(self.settings.whitelist_priority)
        self.blacklist_priority_spin.setValue(self.settings.blacklist_priority)
    
    def _reset_to_default(self):
        reply = QMessageBox.question(
            self, "Подтверждение",
            "Сбросить все настройки к значениям по умолчанию?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            default_settings = LinkReplacementSettings()
            self.settings = default_settings
            self._load_settings()
    
    def _save_settings_from_form(self):
        self.settings.match_threshold_percent = self.match_threshold_spin.value()
        self.settings.use_fuzzy_matching = self.use_fuzzy_check.isChecked()
        self.settings.min_name_similarity = self.min_similarity_spin.value()
        
        search_index = self.search_type_combo.currentIndex()
        if search_index == 0:
            self.settings.search_type = "exact"
        elif search_index == 1:
            self.settings.search_type = "similar"
        else:
            self.settings.search_type = "fuzzy"
        
        self.settings.ignore_special_chars_in_names = self.ignore_special_chars_check.isChecked()
        self.settings.remove_parentheses_in_names = self.remove_parentheses_check.isChecked()
        self.settings.remove_brackets_in_names = self.remove_brackets_check.isChecked()
        self.settings.remove_emojis_in_names = self.remove_emojis_check.isChecked()
        
        self.settings.filter_temporary_links = self.filter_temp_check.isChecked()
        self.settings.temporary_domains = [d.strip() for d in self.temp_domains_edit.text().split(",") if d.strip()]
        self.settings.filter_unsafe_links = self.filter_unsafe_check.isChecked()
        self.settings.unsafe_domains = [d.strip() for d in self.unsafe_domains_edit.text().split(",") if d.strip()]
        
        self.settings.check_timeout = self.check_timeout_spin.value()
        self.settings.max_workers = self.max_workers_spin.value()
        self.settings.max_retries = self.max_retries_spin.value()
        self.settings.retry_delay = self.retry_delay_spin.value()
        
        self.settings.auto_replace_broken = self.auto_broken_check.isChecked()
        self.settings.auto_replace_missing = self.auto_missing_check.isChecked()
        self.settings.keep_backup_links = self.keep_backup_check.isChecked()
        self.settings.max_alternative_urls = self.max_alt_spin.value()
        
        self.settings.use_ip_filtering = self.use_ip_filter_check.isChecked()
        self.settings.prioritize_whitelisted = self.prioritize_whitelist_check.isChecked()
        
        blacklist_text = self.blacklist_ips_edit.toPlainText().strip()
        blacklist_items = [item.strip() for item in blacklist_text.split("\n") if item.strip()]
        self.settings.blacklisted_ips = []
        self.settings.blacklisted_domains = []
        
        for item in blacklist_items:
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$', item):
                self.settings.blacklisted_ips.append(item)
            else:
                self.settings.blacklisted_domains.append(item)
        
        whitelist_text = self.whitelist_ips_edit.toPlainText().strip()
        whitelist_items = [item.strip() for item in whitelist_text.split("\n") if item.strip()]
        self.settings.whitelisted_ips = []
        self.settings.whitelisted_domains = []
        
        for item in whitelist_items:
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$', item):
                self.settings.whitelisted_ips.append(item)
            else:
                self.settings.whitelisted_domains.append(item)
        
        self.settings.whitelist_priority = self.whitelist_priority_spin.value()
        self.settings.blacklist_priority = self.blacklist_priority_spin.value()
    
    def accept(self):
        self._save_settings_from_form()
        self.settings_changed.emit(self.settings)
        super().accept()


class LinkHistoryDialog(QDialog):
    
    def __init__(self, channel: ChannelData, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.setWindowTitle(f"История ссылок: {channel.name}")
        self.resize(700, 400)
        
        self._setup_ui()
        self._load_history()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        info_group = QGroupBox("Информация о канале")
        info_layout = QFormLayout(info_group)
        
        info_layout.addRow("Название:", QLabel(self.channel.name))
        info_layout.addRow("Группа:", QLabel(self.channel.group))
        info_layout.addRow("Текущая ссылка:", QLabel(self.channel.url[:100] + "..." if len(self.channel.url) > 100 else self.channel.url))
        
        layout.addWidget(info_group)
        
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels([
            "Дата", "Старая ссылка", "Новая ссылка", "Причина", "Источник"
        ])
        
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        
        layout.addWidget(self.history_table)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        
        if self.channel.url_history:
            restore_btn = QPushButton("Восстановить старую ссылку")
            restore_btn.clicked.connect(self._restore_old_link)
            button_box.addButton(restore_btn, QDialogButtonBox.ButtonRole.ActionRole)
        
        layout.addWidget(button_box)
    
    def _load_history(self):
        history = self.channel.url_history
        
        if not history:
            self.history_table.setRowCount(1)
            self.history_table.setItem(0, 0, QTableWidgetItem("Нет истории"))
            return
        
        self.history_table.setRowCount(len(history))
        
        for i, record in enumerate(reversed(history)):
            try:
                timestamp = datetime.fromisoformat(record.get('timestamp', ''))
                date_text = timestamp.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                date_text = "Неизвестно"
            
            self.history_table.setItem(i, 0, QTableWidgetItem(date_text))
            
            old_url = record.get('old_url', '')
            old_url_item = QTableWidgetItem(old_url[:80] + "..." if len(old_url) > 80 else old_url)
            old_url_item.setToolTip(old_url)
            self.history_table.setItem(i, 1, old_url_item)
            
            new_url = record.get('new_url', '')
            new_url_item = QTableWidgetItem(new_url[:80] + "..." if len(new_url) > 80 else new_url)
            new_url_item.setToolTip(new_url)
            self.history_table.setItem(i, 2, new_url_item)
            
            reason = record.get('reason', 'Неизвестно')
            self.history_table.setItem(i, 3, QTableWidgetItem(reason))
            
            source = record.get('source', 'Неизвестно')
            self.history_table.setItem(i, 4, QTableWidgetItem(source))
    
    def _restore_old_link(self):
        selected_row = self.history_table.currentRow()
        if selected_row < 0:
            QMessageBox.warning(self, "Предупреждение", "Выберите запись для восстановления")
            return
        
        history_index = len(self.channel.url_history) - 1 - selected_row
        
        if 0 <= history_index < len(self.channel.url_history):
            record = self.channel.url_history[history_index]
            old_url = record.get('old_url', '')
            
            if not old_url:
                QMessageBox.warning(self, "Предупреждение", "Не удалось получить старую ссылку")
                return
            
            reply = QMessageBox.question(
                self, "Подтверждение",
                f"Восстановить старую ссылку?\n\n"
                f"Текущая: {self.channel.url[:100]}...\n"
                f"Старая: {old_url[:100]}...",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                current_url = self.channel.url
                self.channel.url = old_url
                self.channel.add_url_to_history(current_url, old_url, "Восстановление из истории", "manual")
                
                QMessageBox.information(self, "Успех", "Ссылка восстановлена")
                self.accept()


class RemoveMetadataDialog(QDialog):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Удаление метаданных из каналов")
        self.resize(400, 300)
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        info_label = QLabel("Выберите метаданные для удаления:")
        layout.addWidget(info_label)
        
        options_group = QGroupBox("Параметры удаления")
        options_layout = QVBoxLayout(options_group)
        
        self.tvg_id_check = QCheckBox("Удалить tvg-id")
        options_layout.addWidget(self.tvg_id_check)
        
        self.tvg_logo_check = QCheckBox("Удалить tvg-logo (логотипы)")
        options_layout.addWidget(self.tvg_logo_check)
        
        self.group_title_check = QCheckBox("Удалить group-title (группы)")
        options_layout.addWidget(self.group_title_check)
        
        self.user_agent_check = QCheckBox("Удалить User Agent")
        options_layout.addWidget(self.user_agent_check)
        
        self.catchup_check = QCheckBox("Удалить catchup атрибуты (catchup, catchup-days, catchup-source)")
        options_layout.addWidget(self.catchup_check)
        
        layout.addWidget(options_group)
        
        selection_group = QGroupBox("Область применения")
        selection_layout = QVBoxLayout(selection_group)
        
        self.current_channel_radio = QRadioButton("Только текущий канал")
        self.current_channel_radio.setChecked(True)
        selection_layout.addWidget(self.current_channel_radio)
        
        self.selected_channels_radio = QRadioButton("Выбранные каналы")
        selection_layout.addWidget(self.selected_channels_radio)
        
        self.all_channels_radio = QRadioButton("Все каналы в плейлисте")
        selection_layout.addWidget(self.all_channels_radio)
        
        layout.addWidget(selection_group)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def get_metadata_options(self) -> Dict[str, bool]:
        return {
            'tvg_id': self.tvg_id_check.isChecked(),
            'tvg_logo': self.tvg_logo_check.isChecked(),
            'group_title': self.group_title_check.isChecked(),
            'user_agent': self.user_agent_check.isChecked(),
            'catchup': self.catchup_check.isChecked()
        }
    
    def get_selection_scope(self) -> str:
        if self.current_channel_radio.isChecked():
            return "current"
        elif self.selected_channels_radio.isChecked():
            return "selected"
        else:
            return "all"


class DuplicateManagerDialog(QDialog):
    
    def __init__(self, tab, parent=None):
        super().__init__(parent)
        self.tab = tab
        self.all_channels = tab.all_channels.copy()
        self.duplicates = {}
        self.selected_duplicates = {}
        
        self.setWindowTitle("Управление дубликатами")
        self.resize(900, 700)
        
        self._setup_ui()
        self._find_duplicates()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        settings_group = QGroupBox("Настройки поиска дубликатов")
        settings_layout = QFormLayout(settings_group)
        
        self.search_type_combo = QComboBox()
        self.search_type_combo.addItems([
            "По точному URL",
            "По похожим названиям",
            "По URL и названию одновременно"
        ])
        settings_layout.addRow("Тип поиска:", self.search_type_combo)
        
        self.similarity_spin = QDoubleSpinBox()
        self.similarity_spin.setRange(0.5, 1.0)
        self.similarity_spin.setValue(0.8)
        self.similarity_spin.setSingleStep(0.05)
        self.similarity_spin.setSuffix("%")
        self.similarity_spin.setEnabled(False)
        settings_layout.addRow("Порог схожести:", self.similarity_spin)
        
        self.action_combo = QComboBox()
        self.action_combo.addItems([
            "Только показать дубликаты",
            "Удалить дубликаты",
            "Объединить дубликаты"
        ])
        settings_layout.addRow("Действие:", self.action_combo)
        
        layout.addWidget(settings_group)
        
        self.search_btn = QPushButton("Найти дубликаты")
        self.search_btn.clicked.connect(self._find_duplicates)
        layout.addWidget(self.search_btn)
        
        stats_layout = QHBoxLayout()
        self.stats_label = QLabel("Дубликаты не найдены")
        stats_layout.addWidget(self.stats_label)
        stats_layout.addStretch()
        
        layout.addLayout(stats_layout)
        
        preview_group = QGroupBox("Предпросмотр дубликатов")
        preview_layout = QVBoxLayout(preview_group)
        
        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(7)
        self.preview_table.setHorizontalHeaderLabels([
            "✓", "Название", "Группа", "URL", "Статус", "Создан", "Выбрать"
        ])
        
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        
        preview_layout.addWidget(self.preview_table)
        
        quick_select_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("Выбрать всё")
        select_all_btn.clicked.connect(lambda: self._select_quick("all"))
        quick_select_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("Снять всё")
        deselect_all_btn.clicked.connect(lambda: self._select_quick("none"))
        quick_select_layout.addWidget(deselect_all_btn)
        
        preview_layout.addLayout(quick_select_layout)
        
        layout.addWidget(preview_group)
        
        button_box = QDialogButtonBox()
        
        self.apply_btn = QPushButton("Применить")
        self.apply_btn.clicked.connect(self._apply_action)
        self.apply_btn.setEnabled(False)
        
        self.preview_btn = QPushButton("Предпросмотр изменений")
        self.preview_btn.clicked.connect(self._show_preview)
        self.preview_btn.setEnabled(False)
        
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.reject)
        
        button_box.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton(self.preview_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        
        layout.addWidget(button_box)
        
        self.search_type_combo.currentIndexChanged.connect(self._on_search_type_changed)
        self.action_combo.currentIndexChanged.connect(self._on_action_changed)
    
    def _on_search_type_changed(self, index):
        self.similarity_spin.setEnabled(index == 1)
    
    def _on_action_changed(self, index):
        self.apply_btn.setEnabled(index > 0)
        self.preview_btn.setEnabled(index > 0)
    
    def _find_duplicates(self):
        search_type = self.search_type_combo.currentIndex()
        self.duplicates.clear()
        
        if search_type == 0:
            self._find_by_exact_url()
        elif search_type == 1:
            similarity = self.similarity_spin.value()
            self._find_by_similar_names(similarity)
        elif search_type == 2:
            self._find_by_url_and_name()
        
        self._update_preview()
        self._update_stats()
    
    def _find_by_exact_url(self):
        url_map = {}
        
        for i, channel in enumerate(self.all_channels):
            if channel.url and channel.url.strip():
                url = channel.url.strip()
                if url not in url_map:
                    url_map[url] = []
                url_map[url].append((i, channel))
        
        for url, channels in url_map.items():
            if len(channels) > 1:
                self.duplicates[url] = channels
    
    def _find_by_similar_names(self, threshold: float):
        processed = set()
        
        for i, channel1 in enumerate(self.all_channels):
            if i in processed:
                continue
            
            similar_channels = [(i, channel1)]
            
            for j, channel2 in enumerate(self.all_channels[i+1:], start=i+1):
                if j in processed:
                    continue
                
                similarity = channel1.get_similarity_score(channel2)
                if similarity >= threshold:
                    similar_channels.append((j, channel2))
                    processed.add(j)
            
            if len(similar_channels) > 1:
                key = f"similar_{channel1.name[:20]}"
                self.duplicates[key] = similar_channels
                processed.add(i)
    
    def _find_by_url_and_name(self):
        key_map = {}
        
        for i, channel in enumerate(self.all_channels):
            key = f"{channel.name}|{channel.url}".strip().lower()
            if not key:
                continue
                
            if key not in key_map:
                key_map[key] = []
            key_map[key].append((i, channel))
        
        for key, channels in key_map.items():
            if len(channels) > 1:
                self.duplicates[key] = channels
    
    def _update_preview(self):
        self.preview_table.clearContents()
        self.preview_table.setRowCount(0)
        
        if not self.duplicates:
            self.preview_table.setRowCount(1)
            self.preview_table.setItem(0, 0, QTableWidgetItem("Дубликаты не найдены"))
            return
        
        row = 0
        for key, channels in self.duplicates.items():
            self.preview_table.insertRow(row)
            header_item = QTableWidgetItem(f"Группа дубликатов ({len(channels)} записей)")
            header_item.setBackground(QColor("#3498DB"))
            header_item.setForeground(QColor("white"))
            header_item.setFont(QFont("Arial", 10, QFont.Weight.Bold))
            self.preview_table.setItem(row, 0, header_item)
            self.preview_table.setSpan(row, 0, 1, 7)
            row += 1
            
            for idx, channel in channels:
                self.preview_table.insertRow(row)
                
                checkbox_item = QTableWidgetItem()
                checkbox_item.setCheckState(Qt.CheckState.Unchecked)
                self.preview_table.setItem(row, 0, checkbox_item)
                
                name_item = QTableWidgetItem(channel.name)
                name_item.setData(Qt.ItemDataRole.UserRole, (key, idx))
                self.preview_table.setItem(row, 1, name_item)
                
                self.preview_table.setItem(row, 2, QTableWidgetItem(channel.group))
                
                url_display = channel.url[:50] + "..." if len(channel.url) > 50 else channel.url
                url_item = QTableWidgetItem(url_display)
                url_item.setToolTip(channel.url)
                self.preview_table.setItem(row, 3, url_item)
                
                status_item = QTableWidgetItem(channel.get_status_text())
                status_item.setForeground(channel.get_quality_color())
                self.preview_table.setItem(row, 4, status_item)
                
                date_item = QTableWidgetItem(channel.created_date.strftime("%Y-%m-%d"))
                self.preview_table.setItem(row, 5, date_item)
                
                select_btn = QPushButton("Выбрать")
                select_btn.clicked.connect(lambda checked, r=row: self._select_channel(r))
                self.preview_table.setCellWidget(row, 6, select_btn)
                
                row += 1
        
        self.preview_table.resizeRowsToContents()
    
    def _select_channel(self, row: int):
        checkbox_item = self.preview_table.item(row, 0)
        if checkbox_item:
            current_state = checkbox_item.checkState()
            new_state = Qt.CheckState.Checked if current_state == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
            checkbox_item.setCheckState(new_state)
    
    def _select_quick(self, mode: str):
        if not self.duplicates:
            return
        
        for row in range(self.preview_table.rowCount()):
            checkbox_item = self.preview_table.item(row, 0)
            if not checkbox_item or not checkbox_item.data(Qt.ItemDataRole.UserRole):
                continue
            
            name_item = self.preview_table.item(row, 1)
            if not name_item:
                continue
            
            key, idx = name_item.data(Qt.ItemDataRole.UserRole)
            channel = self.all_channels[idx]
            
            should_select = False
            
            if mode == "all":
                should_select = True
            elif mode == "none":
                should_select = False
            
            checkbox_item.setCheckState(Qt.CheckState.Checked if should_select else Qt.CheckState.Unchecked)
    
    def _update_stats(self):
        if not self.duplicates:
            self.stats_label.setText("Дубликаты не найдены")
            return
        
        total_duplicates = sum(len(channels) for channels in self.duplicates.values())
        total_groups = len(self.duplicates)
        
        self.stats_label.setText(
            f"Найдено: {total_groups} групп дубликатов, всего записей: {total_duplicates}"
        )
    
    def _show_preview(self):
        if not self._get_selected_channels():
            QMessageBox.warning(self, "Предупреждение", "Выберите каналы для обработки")
            return
        
        action = self.action_combo.currentIndex()
        if action == 1:
            self._show_delete_preview()
        elif action == 2:
            self._show_merge_preview()
    
    def _show_delete_preview(self):
        selected = self._get_selected_channels()
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Предпросмотр удаления дубликатов")
        dialog.resize(800, 600)
        
        layout = QVBoxLayout(dialog)
        
        info_label = QLabel(f"Будет удалено {len(selected)} дубликатов. Будут сохранены:")
        layout.addWidget(info_label)
        
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Группа", "Сохранённый канал", "URL", "Будет удалено"])
        
        row = 0
        for key, channels in selected.items():
            selected_channel = None
            to_delete = []
            
            for idx, channel in self.duplicates[key]:
                if (key, idx) in channels:
                    selected_channel = channel
                else:
                    to_delete.append(channel)
            
            if selected_channel:
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(key[:30] + "..." if len(key) > 30 else key))
                table.setItem(row, 1, QTableWidgetItem(selected_channel.name))
                table.setItem(row, 2, QTableWidgetItem(selected_channel.url[:50] + "..." if len(selected_channel.url) > 50 else selected_channel.url))
                table.setItem(row, 3, QTableWidgetItem(f"{len(to_delete)} зап."))
                row += 1
        
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        dialog.exec()
    
    def _show_merge_preview(self):
        selected = self._get_selected_channels()
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Предпросмотр объединения дубликатов")
        dialog.resize(800, 600)
        
        layout = QVBoxLayout(dialog)
        
        info_label = QLabel(f"Будет создано {len(selected)} объединённых записей:")
        layout.addWidget(info_label)
        
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["Группа", "Название", "Группа", "URL", "Объединено"])
        
        row = 0
        for key, channels in selected.items():
            merged_channel = self._merge_channels([self.all_channels[idx] for _, idx in channels])
            
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(key[:30] + "..." if len(key) > 30 else key))
            table.setItem(row, 1, QTableWidgetItem(merged_channel.name))
            table.setItem(row, 2, QTableWidgetItem(merged_channel.group))
            
            url_text = merged_channel.url[:50] + "..." if len(merged_channel.url) > 50 else merged_channel.url
            url_item = QTableWidgetItem(url_text)
            url_item.setToolTip(merged_channel.url)
            table.setItem(row, 3, url_item)
            
            table.setItem(row, 4, QTableWidgetItem(f"{len(channels)} зап."))
            row += 1
        
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        dialog.exec()
    
    def _get_selected_channels(self) -> Dict[str, Set[Tuple[str, int]]]:
        selected = {}
        
        for row in range(self.preview_table.rowCount()):
            checkbox_item = self.preview_table.item(row, 0)
            if not checkbox_item or checkbox_item.checkState() != Qt.CheckState.Checked:
                continue
            
            name_item = self.preview_table.item(row, 1)
            if not name_item:
                continue
            
            key, idx = name_item.data(Qt.ItemDataRole.UserRole)
            if key not in selected:
                selected[key] = set()
            selected[key].add((key, idx))
        
        return selected
    
    def _merge_channels(self, channels: List[ChannelData]) -> ChannelData:
        if not channels:
            return ChannelData()
        
        merged = channels[0].copy()
        
        for channel in channels[1:]:
            if channel.tvg_id and not merged.tvg_id:
                merged.tvg_id = channel.tvg_id
            
            if channel.tvg_logo and not merged.tvg_logo:
                merged.tvg_logo = channel.tvg_logo
            
            if channel.group and not merged.group:
                merged.group = channel.group
            
            if channel.user_agent and not merged.user_agent:
                merged.user_agent = channel.user_agent
                merged.extra_headers['User-Agent'] = channel.user_agent
            
            merged.extra_headers.update(channel.extra_headers)
            
            if channel.url and channel.url != merged.url:
                if channel.url not in merged.alternative_urls:
                    merged.alternative_urls.append(channel.url)
            
            merged.url_history.extend(channel.url_history)
        
        merged.update_extinf()
        merged.update_extvlcopt_from_headers()
        
        return merged
    
    def _apply_action(self):
        selected = self._get_selected_channels()
        if not selected:
            QMessageBox.warning(self, "Предупреждение", "Выберите каналы для обработки")
            return
        
        action = self.action_combo.currentIndex()
        
        if action == 1:
            self._apply_delete(selected)
        elif action == 2:
            self._apply_merge(selected)
        
        self.accept()
    
    def _apply_delete(self, selected: Dict[str, Set[Tuple[str, int]]]):
        channels_to_keep = set()
        channels_to_remove = set()
        
        for key, channels in selected.items():
            group_channels = {idx for _, idx in self.duplicates[key]}
            selected_indices = {idx for _, idx in channels}
            
            channels_to_keep.update(selected_indices)
            channels_to_remove.update(group_channels - selected_indices)
        
        new_channels = []
        for i, channel in enumerate(self.all_channels):
            if i in channels_to_keep or i not in channels_to_remove:
                new_channels.append(channel)
        
        self.tab._save_state("Удаление дубликатов")
        self.tab.all_channels = new_channels
        self.tab._apply_filter()
        self.tab.modified = True
        self.tab._update_modified_status()
        
        QMessageBox.information(
            self, "Успех",
            f"Удалено {len(channels_to_remove)} дубликатов\n"
            f"Сохранено {len(channels_to_keep)} записей"
        )
    
    def _apply_merge(self, selected: Dict[str, Set[Tuple[str, int]]]):
        channels_to_keep = []
        channels_to_remove = set()
        
        for key, channels in selected.items():
            channel_indices = [idx for _, idx in channels]
            channels_list = [self.all_channels[idx] for idx in channel_indices]
            
            merged_channel = self._merge_channels(channels_list)
            channels_to_keep.append(merged_channel)
            channels_to_remove.update(channel_indices)
        
        new_channels = []
        for i, channel in enumerate(self.all_channels):
            if i not in channels_to_remove:
                new_channels.append(channel)
        
        new_channels.extend(channels_to_keep)
        
        self.tab._save_state("Объединение дубликатов")
        self.tab.all_channels = new_channels
        self.tab._apply_filter()
        self.tab.modified = True
        self.tab._update_modified_status()
        
        QMessageBox.information(
            self, "Успех",
            f"Объединено {len(channels_to_remove)} записей в {len(channels_to_keep)}\n"
            f"Создано {len(channels_to_keep)} объединённых записей"
        )


class PlaylistTab(QWidget):
    
    channel_selected = pyqtSignal(ChannelData)
    undo_state_changed = pyqtSignal(bool, bool)
    info_changed = pyqtSignal(str)
    
    def __init__(self, filepath: str = None, parent=None, blacklist_manager: BlacklistManager = None):
        super().__init__(parent)
        self.filepath = filepath
        self.all_channels: List[ChannelData] = []
        self.filtered_channels: List[ChannelData] = []
        self.selected_channels: List[ChannelData] = []
        self.current_channel: Optional[ChannelData] = None
        self.modified = False
        self.blacklist_manager = blacklist_manager
        self.parent_window = None
        
        self.header_manager = PlaylistHeaderManager()
        
        parent_widget = parent
        while parent_widget and not isinstance(parent_widget, QMainWindow):
            parent_widget = parent_widget.parent()
        self.parent_window = parent_widget
        
        self.undo_manager = UndoRedoManager()
        
        self._setup_ui()
        self._setup_shortcuts()
        
        if filepath and os.path.exists(filepath):
            self._load_file(filepath)
        else:
            self._save_state("Инициализация")
            self._update_info()
    
    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        self.table = ChannelTableWidget(playlist_tab=self)
        self._setup_table()
        
        main_layout.addWidget(self.table)
        
        self.table.cell_edited.connect(self._on_cell_edited)
        self.table.url_check_requested.connect(self._check_single_url)
        self.table.edit_user_agent_requested.connect(self._edit_user_agent)
        self.table.remove_broken_url_requested.connect(self._remove_broken_url)
    
    def _setup_table(self):
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["№", "Название", "Группа", "TVG-ID", "Логотип", "URL/Статус"])
        
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 250)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 200)
        
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_double_click)
        
        self.table.setSortingEnabled(True)
    
    def _on_cell_edited(self, row: int, column: int, new_value: str):
        if 0 <= row < len(self.filtered_channels):
            channel = self.filtered_channels[row]
            
            self.table.blockSignals(True)
            try:
                if column == 1:
                    channel.name = new_value.strip()
                    channel.update_extinf()
                elif column == 2:
                    channel.group = new_value.strip() or "Без группы"
                    channel.update_extinf()
                elif column == 3:
                    channel.tvg_id = new_value.strip()
                    channel.update_extinf()
                elif column == 4:
                    channel.tvg_logo = new_value.strip()
                    channel.update_extinf()
                elif column == 5:
                    old_url = channel.url
                    channel.url = new_value.strip()
                    channel.has_url = bool(channel.url)
                    channel.url_status = None
                    channel.url_check_time = None
                    channel.link_quality = LinkQuality.UNKNOWN
                    channel.link_response_time = None
                    
                    if old_url != channel.url:
                        channel.add_url_to_history(old_url, channel.url, "Ручное Правка", "manual")
                
                self._update_table_row(row, channel)
            finally:
                self.table.blockSignals(False)
            
            self._save_state("Правка в таблице")
            
            self.modified = True
            self._update_modified_status()
            
            self._update_info()
    
    def _update_table_row(self, row: int, channel: ChannelData):
        if row >= self.table.rowCount():
            return
        
        self.table.blockSignals(True)
        try:
            number_item = QTableWidgetItem(str(row + 1))
            number_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            number_item.setFlags(number_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, number_item)
            
            name_item = QTableWidgetItem(channel.name)
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, name_item)
            
            group_item = QTableWidgetItem(channel.group)
            group_item.setFlags(group_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 2, group_item)
            
            tvg_id_item = QTableWidgetItem(channel.tvg_id)
            tvg_id_item.setFlags(tvg_id_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 3, tvg_id_item)
            
            logo_item = QTableWidgetItem(channel.tvg_logo)
            logo_item.setFlags(logo_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 4, logo_item)
            
            status_item = QTableWidgetItem(channel.url)
            status_item.setFlags(status_item.flags() | Qt.ItemFlag.ItemIsEditable)
            status_item.setForeground(channel.get_quality_color())
            status_item.setToolTip(channel.get_status_tooltip())
            
            if channel.user_agent:
                status_item.setBackground(QColor(220, 255, 220))
            
            if channel.url_history:
                status_item.setBackground(QColor(255, 255, 200))
            
            self.table.setItem(row, 5, status_item)
        finally:
            self.table.blockSignals(False)
    
    def _remove_broken_url(self, row: int):
        if 0 <= row < len(self.filtered_channels):
            channel = self.filtered_channels[row]
            if channel.url_status is False:
                self._save_state("Удаление битой ссылки")
                old_url = channel.url
                channel.url = ""
                channel.has_url = False
                channel.url_status = None
                channel.url_check_time = None
                channel.link_quality = LinkQuality.UNKNOWN
                channel.link_response_time = None
                channel.add_url_to_history(old_url, "", "Удаление битой ссылки", "manual")
                
                self._update_table_row(row, channel)
                self.modified = True
                self._update_modified_status()
                self._update_info()
    
    def _remove_selected_broken_urls(self):
        if not self.selected_channels:
            return
        
        count = 0
        for channel in self.selected_channels:
            if channel.url_status is False:
                old_url = channel.url
                channel.url = ""
                channel.has_url = False
                channel.url_status = None
                channel.url_check_time = None
                channel.link_quality = LinkQuality.UNKNOWN
                channel.link_response_time = None
                channel.add_url_to_history(old_url, "", "Удаление битой ссылки", "manual")
                count += 1
        
        if count > 0:
            self._save_state("Удаление битых ссылок")
            self.modified = True
            self._update_modified_status()
            self._update_info()
            self._apply_filter()
            QMessageBox.information(self, "Успех", f"Удалено {count} битых ссылок")
    
    def _remove_all_broken_urls(self):
        channels_with_broken = [ch for ch in self.all_channels if ch.url_status is False]
        
        if not channels_with_broken:
            QMessageBox.information(self, "Информация", "Нет каналов с битыми ссылками")
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Найдено {len(channels_with_broken)} каналов с битыми ссылками. Удалить все битые ссылки?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление всех битых ссылок")
            
            count = 0
            for channel in channels_with_broken:
                old_url = channel.url
                channel.url = ""
                channel.has_url = False
                channel.url_status = None
                channel.url_check_time = None
                channel.link_quality = LinkQuality.UNKNOWN
                channel.link_response_time = None
                channel.add_url_to_history(old_url, "", "Удаление битой ссылки", "manual")
                count += 1
            
            self._apply_filter()
            self.modified = True
            self._update_modified_status()
            self._update_info()
            QMessageBox.information(self, "Успех", f"Удалено {count} битых ссылок")
    
    def _edit_user_agent(self, row: int):
        if 0 <= row < len(self.filtered_channels):
            channel = self.filtered_channels[row]
            
            current_ua = channel.user_agent or ""
            new_ua, ok = QInputDialog.getText(
                self, "Правка User Agent",
                "Введите User Agent для канала:",
                QLineEdit.EchoMode.Normal,
                current_ua
            )
            
            if ok:
                self._save_state("Изменение User Agent")
                
                channel.user_agent = new_ua.strip()
                if channel.user_agent:
                    channel.extra_headers['User-Agent'] = channel.user_agent
                elif 'User-Agent' in channel.extra_headers:
                    del channel.extra_headers['User-Agent']
                channel.update_extvlcopt_from_headers()
                
                self._update_table_row(row, channel)
                self.modified = True
                self._update_modified_status()
    
    def _copy_channel(self):
        if self.current_channel:
            parent = self.parent_window
            if parent and hasattr(parent, 'copied_channel'):
                parent.copied_channel = self.current_channel.copy()
    
    def _cut_channel(self):
        if self.current_channel:
            self._copy_channel()
            self._delete_channel()
    
    def _cut_selected_channels(self):
        if self.selected_channels:
            self._copy_selected_channels()
            self._delete_selected_channels()
    
    def _copy_selected_channels(self):
        if not self.selected_channels:
            return
        
        parent = self.parent_window
        if parent and hasattr(parent, 'copied_channels'):
            parent.copied_channels = [ch.copy() for ch in self.selected_channels]
    
    def _copy_metadata(self):
        if self.current_channel:
            parent = self.parent_window
            if parent and hasattr(parent, 'copied_metadata'):
                parent.copied_metadata = self.current_channel.copy_metadata_only()
    
    def _copy_selected_metadata(self):
        if not self.selected_channels:
            return
        
        parent = self.parent_window
        if parent and hasattr(parent, 'copied_metadata_list'):
            parent.copied_metadata_list = [ch.copy_metadata_only() for ch in self.selected_channels]
    
    def _paste_channel(self):
        parent = self.parent_window
        
        if not parent or not hasattr(parent, 'copied_channel') or not parent.copied_channel:
            return
        
        self._save_state("Вставка канала")
        
        channel = parent.copied_channel.copy()
        
        if self.current_channel:
            try:
                idx = self.all_channels.index(self.current_channel) + 1
                self.all_channels.insert(idx, channel)
            except ValueError:
                self.all_channels.append(channel)
        else:
            self.all_channels.append(channel)
        
        self._apply_filter()
        
        if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
            self.parent_window._update_group_filter()
        
        self.modified = True
        self._update_modified_status()
    
    def _paste_selected_channels(self):
        parent = self.parent_window
        
        if not parent or not hasattr(parent, 'copied_channels') or not parent.copied_channels:
            return
        
        self._save_state("Вставка нескольких каналов")
        
        for channel in parent.copied_channels:
            new_channel = channel.copy()
            
            if self.current_channel:
                try:
                    idx = self.all_channels.index(self.current_channel) + 1
                    self.all_channels.insert(idx, new_channel)
                except ValueError:
                    self.all_channels.append(new_channel)
            else:
                self.all_channels.append(new_channel)
        
        self._apply_filter()
        
        if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
            self.parent_window._update_group_filter()
        
        self.modified = True
        self._update_modified_status()
    
    def _paste_metadata(self):
        parent = self.parent_window
        
        if not parent or not hasattr(parent, 'copied_metadata') or not parent.copied_metadata:
            return
        
        if not self.current_channel:
            return
        
        self._save_state("Вставка метаданных")
        
        self.current_channel.update_metadata_from(parent.copied_metadata)
        self._update_table()
        
        self.modified = True
        self._update_modified_status()
    
    def _paste_selected_metadata(self):
        parent = self.parent_window
        
        if not parent or not hasattr(parent, 'copied_metadata_list') or not parent.copied_metadata_list:
            return
        
        if not self.selected_channels:
            return
        
        if len(parent.copied_metadata_list) != len(self.selected_channels):
            return
        
        self._save_state("Вставка метаданных в выбранные каналы")
        
        for i, channel in enumerate(self.selected_channels):
            if i < len(parent.copied_metadata_list):
                channel.update_metadata_from(parent.copied_metadata_list[i])
        
        self._update_table()
        
        self.modified = True
        self._update_modified_status()
    
    def _rename_groups(self):
        if not self.selected_channels:
            return
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Пакетное переименование групп")
        dialog.resize(400, 200)
        
        layout = QVBoxLayout(dialog)
        
        info_label = QLabel(f"Выбрано каналов для переименования групп: {len(self.selected_channels)}")
        layout.addWidget(info_label)
        
        groups = set(ch.group for ch in self.selected_channels)
        if len(groups) == 1:
            current_group = list(groups)[0]
        else:
            current_group = "РАЗНЫЕ ГРУППЫ"
        
        current_group_value = QLabel(f"<b>{current_group}</b>")
        layout.addWidget(current_group_value)
        
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)
        
        new_group_label = QLabel("Новая группа:")
        layout.addWidget(new_group_label)
        
        new_group_edit = QLineEdit()
        if len(groups) == 1:
            new_group_edit.setText(current_group)
        new_group_edit.setPlaceholderText("Введите новое название группы")
        layout.addWidget(new_group_edit)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_group = new_group_edit.text().strip()
            if not new_group:
                return
            
            if new_group == current_group:
                return
            
            self._save_state("Пакетное переименование групп")
            
            for channel in self.selected_channels:
                channel.group = new_group
                channel.update_extinf()
            
            self._apply_filter()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def _setup_shortcuts(self):
        shortcuts = {
            QKeySequence("Ctrl+S"): self._save_changes,
            QKeySequence("Delete"): self._delete_channel,
            QKeySequence("Ctrl+Shift+Delete"): self._delete_selected_channels,
            QKeySequence("Ctrl+Z"): self._undo,
            QKeySequence("Ctrl+Y"): self._redo,
            QKeySequence("Ctrl+Shift+A"): self._new_channel,
            QKeySequence("Ctrl+C"): self._copy_channel,
            QKeySequence("Ctrl+X"): self._cut_channel,
            QKeySequence("Ctrl+V"): self._paste_channel,
            QKeySequence("Ctrl+Up"): self._move_channel_up,
            QKeySequence("Ctrl+Down"): self._move_channel_down,
            QKeySequence("Ctrl+Shift+Up"): self._move_selected_up,
            QKeySequence("Ctrl+Shift+Down"): self._move_selected_down,
            QKeySequence("F2"): self._edit_current_cell,
        }
        
        for key, slot in shortcuts.items():
            shortcut = QShortcut(key, self)
            shortcut.activated.connect(slot)
    
    def _save_changes(self):
        pass
    
    def _edit_current_cell(self):
        current_item = self.table.currentItem()
        if current_item:
            self.table.edit(current_item.row(), current_item.column())
        self._on_selection_changed()
    
    def _save_state(self, description: str = ""):
        self.undo_manager.save_state(self.all_channels, description)
        self.undo_state_changed.emit(
            self.undo_manager.can_undo(),
            self.undo_manager.can_redo()
        )
    
    def _undo(self):
        state = self.undo_manager.undo()
        if state:
            self.all_channels = [ChannelData.from_dict(ch) for ch in state['channels']]
            
            self.current_channel = None
            self.selected_channels = []
            
            self._apply_filter()
            self._update_info()
            
            self.undo_state_changed.emit(
                self.undo_manager.can_undo(),
                self.undo_manager.can_redo()
            )
            
            self.modified = True
            self._update_modified_status()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
    
    def _redo(self):
        state = self.undo_manager.redo()
        if state:
            self.all_channels = [ChannelData.from_dict(ch) for ch in state['channels']]
            
            self.current_channel = None
            self.selected_channels = []
            
            self._apply_filter()
            self._update_info()
            
            self.undo_state_changed.emit(
                self.undo_manager.can_undo(),
                self.undo_manager.can_redo()
            )
            
            self.modified = True
            self._update_modified_status()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
    
    def _update_modified_status(self):
        self._update_info()
    
    def _check_current_url(self):
        if not self.current_channel or not self.current_channel.url:
            return
        
        url = self.current_channel.url.strip()
        self._check_urls([url], [self.current_channel])
    
    def _check_single_url(self, row: int):
        if 0 <= row < len(self.filtered_channels):
            channel = self.filtered_channels[row]
            if channel and channel.url:
                self._check_urls([channel.url], [channel])
    
    def _check_selected_urls(self):
        if not self.selected_channels:
            return
        
        urls = []
        channels_with_urls = []
        
        for channel in self.selected_channels:
            if channel.url and channel.url.strip():
                urls.append(channel.url)
                channels_with_urls.append(channel)
        
        if not urls:
            return
        
        self._check_urls(urls, channels_with_urls)
    
    def check_all_urls(self):
        urls = []
        channels_with_urls = []
        
        for channel in self.all_channels:
            if channel.url and channel.url.strip():
                urls.append(channel.url)
                channels_with_urls.append(channel)
        
        if not urls:
            return
        
        self._check_urls(urls, channels_with_urls)
    
    def _check_urls(self, urls: List[str], channels: List[ChannelData]):
        if not urls or not channels:
            return
        
        url_to_channels = {}
        for i, channel in enumerate(channels):
            if channel.url:
                url = channel.url.strip()
                if url not in url_to_channels:
                    url_to_channels[url] = []
                url_to_channels[url].append((channel, i))
        
        unique_urls = list(url_to_channels.keys())
        
        if not unique_urls:
            return
        
        dialog = URLCheckDialog(self)
        dialog.set_urls(unique_urls)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        
        def on_check_completed(results):
            url_results = {}
            for idx, result in results.items():
                if idx < len(unique_urls):
                    url = unique_urls[idx]
                    url_results[url] = result
            
            self._save_state("Проверка ссылок")
            
            for url, result in url_results.items():
                if url in url_to_channels:
                    for channel, _ in url_to_channels[url]:
                        channel.url_status = result.get('success')
                        channel.url_check_time = datetime.now()
                        channel.link_response_time = result.get('response_time')
                        channel.link_quality = result.get('quality', LinkQuality.UNKNOWN)
            
            self._apply_filter()
            self._update_info()
            
            self.modified = True
            self._update_modified_status()
            
            self.undo_state_changed.emit(
                self.undo_manager.can_undo(),
                self.undo_manager.can_redo()
            )
        
        dialog.url_check_completed.connect(on_check_completed)
        dialog.exec()
    
    def check_selected_urls(self):
        self._check_selected_urls()
    
    def delete_channels_without_urls(self):
        channels_without_urls = [ch for ch in self.all_channels if not ch.has_url or not ch.url or not ch.url.strip()]
        
        if not channels_without_urls:
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Найдено {len(channels_without_urls)} каналов без ссылок. Удалить их?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление каналов без ссылок")
            
            for channel in channels_without_urls:
                if channel in self.all_channels:
                    self.all_channels.remove(channel)
            
            self._apply_filter()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def _find_duplicate_urls(self) -> Dict[str, List[ChannelData]]:
        url_map = {}
        
        for channel in self.all_channels:
            if channel.url and channel.url.strip():
                url = channel.url.strip()
                if url not in url_map:
                    url_map[url] = []
                url_map[url].append(channel)
        
        return {url: channels for url, channels in url_map.items() if len(channels) > 1}
    
    def find_duplicate_urls(self):
        duplicates = self._find_duplicate_urls()
        
        if not duplicates:
            return
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Поиск дубликатов по URL")
        dialog.resize(800, 600)
        
        layout = QVBoxLayout(dialog)
        
        info_label = QLabel(f"Найдено {len(duplicates)} URL с дубликатами:")
        layout.addWidget(info_label)
        
        tabs = QTabWidget()
        
        for url, channels in duplicates.items():
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            
            table = QTableWidget()
            table.setColumnCount(5)
            table.setHorizontalHeaderLabels(["Название", "Группа", "TVG-ID", "Статус", "Выбрать"])
            table.setRowCount(len(channels))
            
            for i, channel in enumerate(channels):
                table.setItem(i, 0, QTableWidgetItem(channel.name))
                table.setItem(i, 1, QTableWidgetItem(channel.group))
                table.setItem(i, 2, QTableWidgetItem(channel.tvg_id))
                
                status_item = QTableWidgetItem(channel.get_status_text())
                status_item.setForeground(channel.get_quality_color())
                table.setItem(i, 3, status_item)
                
                checkbox_item = QTableWidgetItem()
                checkbox_item.setCheckState(Qt.CheckState.Checked)
                table.setItem(i, 4, checkbox_item)
            
            table.horizontalHeader().setStretchLastSection(True)
            tab_layout.addWidget(table)
            
            btn_layout = QHBoxLayout()
            
            select_all_btn = QPushButton("Выбрать всё")
            select_all_btn.clicked.connect(lambda checked, t=table: self._select_all_in_table(t))
            btn_layout.addWidget(select_all_btn)
            
            deselect_all_btn = QPushButton("Снять выделение")
            deselect_all_btn.clicked.connect(lambda checked, t=table: self._deselect_all_in_table(t))
            btn_layout.addWidget(deselect_all_btn)
            
            delete_selected_btn = QPushButton("Удалить выбранные")
            delete_selected_btn.clicked.connect(lambda checked, ch=channels, t=table, d=dialog: 
                                               self._delete_selected_duplicates(ch, t, d))
            btn_layout.addWidget(delete_selected_btn)
            
            tab_layout.addLayout(btn_layout)
            
            url_display = url[:50] + "..." if len(url) > 50 else url
            tabs.addTab(tab, f"{len(channels)} дубл. ({url_display})")
        
        layout.addWidget(tabs)
        
        delete_all_btn = QPushButton("Удалить все дубликаты (кроме первого в каждой группе)")
        delete_all_btn.clicked.connect(lambda: self._remove_all_duplicates(duplicates, dialog))
        layout.addWidget(delete_all_btn)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        dialog.exec()
    
    def _select_all_in_table(self, table: QTableWidget):
        for i in range(table.rowCount()):
            item = table.item(i, 4)
            if item:
                item.setCheckState(Qt.CheckState.Checked)
    
    def _deselect_all_in_table(self, table: QTableWidget):
        for i in range(table.rowCount()):
            item = table.item(i, 4)
            if item:
                item.setCheckState(Qt.CheckState.Unchecked)
    
    def _delete_selected_duplicates(self, channels: List[ChannelData], table: QTableWidget, dialog: QDialog):
        selected_channels = []
        
        for i in range(table.rowCount()):
            item = table.item(i, 4)
            if item and item.checkState() == Qt.CheckState.Checked:
                if i < len(channels):
                    selected_channels.append(channels[i])
        
        if not selected_channels:
            return
        
        if len(selected_channels) >= len(channels):
            return
        
        reply = QMessageBox.question(
            dialog, "Подтверждение",
            f"Удалить выбранные {len(selected_channels)} дубликатов?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление дубликатов по URL")
            
            for channel in selected_channels:
                if channel in self.all_channels:
                    self.all_channels.remove(channel)
            
            self._apply_filter()
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            self.modified = True
            self._update_modified_status()
            
            dialog.accept()
    
    def _remove_all_duplicates(self, duplicates: Dict[str, List[ChannelData]], dialog: QDialog):
        reply = QMessageBox.question(
            dialog, "Подтверждение",
            "Удалить все дубликаты, оставляя только первый канал в каждой группе?\n"
            "Это действие нельзя отменить!",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление всех дубликатов по URL")
            
            removed_count = 0
            
            for url, channels in duplicates.items():
                for i in range(1, len(channels)):
                    if channels[i] in self.all_channels:
                        self.all_channels.remove(channels[i])
                        removed_count += 1
            
            self._apply_filter()
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            self.modified = True
            self._update_modified_status()
            
            dialog.accept()
    
    def remove_duplicate_urls(self):
        duplicates = self._find_duplicate_urls()
        
        if not duplicates:
            return
        
        total_duplicates = sum(len(channels) - 1 for channels in duplicates.values())
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Найдено {total_duplicates} дубликатов по URL.\n"
            "Удалить дубликаты, оставляя только первый канал в каждой группе?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление дубликатов по URL")
            
            removed_count = 0
            
            for url, channels in duplicates.items():
                for i in range(1, len(channels)):
                    if channels[i] in self.all_channels:
                        self.all_channels.remove(channels[i])
                        removed_count += 1
            
            self._apply_filter()
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def _load_file(self, filepath: str):
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            
            self.header_manager.parse_header(content)
            
            lines = content.split('\n')
            
            start_index = 0
            for i, line in enumerate(lines):
                if line.startswith('#EXTINF:'):
                    start_index = i
                    break
            
            self._parse_m3u('\n'.join(lines[start_index:]))
            
            if self.blacklist_manager:
                original_count = len(self.all_channels)
                filtered, removed = self.blacklist_manager.filter_channels(self.all_channels)
                self.all_channels = filtered
            
            self._apply_filter()
            self._update_info()
            
            self.modified = False
            self._update_modified_status()
            
            self._save_state("Загрузка файла")
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить файл:\n{str(e)}")
    
    def _parse_m3u(self, content: str):
        self.all_channels.clear()
        
        lines = content.splitlines()
        i = 0
        
        try:
            while i < len(lines):
                line = lines[i].strip()
                
                if not line:
                    i += 1
                    continue
                
                if line.startswith('#EXTINF:'):
                    channel = ChannelData()
                    fixed_line = ChannelNameNormalizer.fix_encoding(line)
                    channel.extinf = fixed_line
                    
                    if ',' in fixed_line:
                        parts = fixed_line.split(',', 1)
                        channel.name = ChannelNameNormalizer.fix_encoding(parts[1].strip())
                    
                    attrs_part = fixed_line.split(',')[0] if ',' in fixed_line else fixed_line
                    
                    tvg_id_match = re.search(r'tvg-id="([^"]*)"', attrs_part)
                    if tvg_id_match:
                        channel.tvg_id = ChannelNameNormalizer.fix_encoding(tvg_id_match.group(1))
                    
                    logo_match = re.search(r'tvg-logo="([^"]*)"', attrs_part)
                    if logo_match:
                        channel.tvg_logo = ChannelNameNormalizer.fix_encoding(logo_match.group(1))
                    
                    group_match = re.search(r'group-title="([^"]*)"', attrs_part)
                    if group_match:
                        channel.group = ChannelNameNormalizer.fix_encoding(group_match.group(1))
                    else:
                        channel.group = "Без группы"
                    
                    j = i + 1
                    has_url = False
                    url_lines = []
                    extvlcopt_lines = []
                    
                    while j < len(lines):
                        next_line = lines[j].strip()
                        if not next_line:
                            j += 1
                            continue
                        
                        if next_line.startswith('#EXTINF:'):
                            break
                        
                        if next_line.startswith('#'):
                            fixed_next_line = ChannelNameNormalizer.fix_encoding(next_line)
                            extvlcopt_lines.append(fixed_next_line)
                        else:
                            url_lines.append(ChannelNameNormalizer.fix_encoding(next_line))
                            has_url = True
                            break
                        
                        j += 1
                    
                    if has_url:
                        j += 1
                        
                        while j < len(lines):
                            next_line = lines[j].strip()
                            if not next_line:
                                j += 1
                                continue
                            
                            if next_line.startswith('#EXTINF:'):
                                break
                            
                            if next_line.startswith('#'):
                                fixed_next_line = ChannelNameNormalizer.fix_encoding(next_line)
                                extvlcopt_lines.append(fixed_next_line)
                            else:
                                break
                            
                            j += 1
                    else:
                        while j < len(lines) and not lines[j].strip():
                            j += 1
                    
                    if url_lines:
                        channel.url = '\n'.join(url_lines)
                        channel.has_url = True
                    else:
                        channel.url = ""
                        channel.has_url = False
                    
                    channel.extvlcopt_lines = extvlcopt_lines
                    channel.parse_extvlcopt_headers()
                    if 'User-Agent' in channel.extra_headers:
                        channel.user_agent = channel.extra_headers['User-Agent']
                    
                    self.all_channels.append(channel)
                    
                    i = j
                else:
                    i += 1
        
        except (IndexError, ValueError) as e:
            logger.error(f"Ошибка парсинга M3U в строке {i}: {e}")
            if i < len(lines):
                i += 1
    
    def _apply_filter(self):
        parent = self.parent_window
        
        if parent and hasattr(parent, 'search_edit') and hasattr(parent, 'group_combo'):
            search_text = parent.search_edit.text().lower() if parent.search_edit else ""
            group_filter = parent.group_combo.currentText() if parent.group_combo else "Все группы"
        else:
            search_text = ""
            group_filter = "Все группы"
        
        if group_filter == "Все группы":
            filtered = self.all_channels
        else:
            filtered = [ch for ch in self.all_channels if ch.group == group_filter]
        
        if search_text:
            self.filtered_channels = [
                ch for ch in filtered
                if (search_text in ch.name.lower() or 
                    search_text in ch.group.lower() or
                    search_text in (ch.tvg_id or "").lower() or
                    search_text in ch.url.lower())
            ]
        else:
            self.filtered_channels = filtered
        
        self._update_table()
        self._update_info()
    
    def _update_table(self):
        selected_rows = []
        if self.table.selectedItems():
            selected_rows = list(set(item.row() for item in self.table.selectedItems()))
        
        scroll_value = self.table.verticalScrollBar().value()
        
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        
        try:
            self.table.setSortingEnabled(False)
            
            row_count = len(self.filtered_channels)
            self.table.setRowCount(row_count)
            
            for i, channel in enumerate(self.filtered_channels):
                self._update_table_row(i, channel)
            
            self.table.setSortingEnabled(True)
            
            if selected_rows:
                for row in selected_rows:
                    if row < self.table.rowCount():
                        self.table.selectRow(row)
            
            self.table.verticalScrollBar().setValue(scroll_value)
            
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
        
        self._update_info()
    
    def _update_info(self):
        total = len(self.all_channels)
        with_url = sum(1 for ch in self.all_channels if ch.has_url and ch.url and ch.url.strip())
        without_url = total - with_url
        
        working = sum(1 for ch in self.all_channels if ch.url_status is True)
        not_working = sum(1 for ch in self.all_channels if ch.url_status is False)
        unknown = sum(1 for ch in self.all_channels if (ch.url_status is None) and ch.has_url and ch.url and ch.url.strip())
        
        info_text = (f"Каналов: {total} | С URL: {with_url} | "
                    f"✓: {working} | ✗: {not_working} | ?: {unknown}")
        
        self.info_changed.emit(info_text)
    
    def _on_selection_changed(self):
        selected_items = self.table.selectedItems()
        if not selected_items:
            self.selected_channels = []
            if hasattr(self, 'undo_state_changed'):
                self.undo_state_changed.emit(
                    self.undo_manager.can_undo(),
                    self.undo_manager.can_redo()
                )
            return
        
        rows = set()
        for item in selected_items:
            rows.add(item.row())
        
        self.selected_channels = []
        for row in sorted(rows):
            if 0 <= row < len(self.filtered_channels):
                channel = self.filtered_channels[row]
                self.selected_channels.append(channel)
                self.current_channel = channel
        
        if hasattr(self, 'undo_state_changed'):
            self.undo_state_changed.emit(
                self.undo_manager.can_undo(),
                self.undo_manager.can_redo()
            )
    
    def _on_double_click(self, index):
        if index.isValid():
            self.table.edit(index)
        self._on_selection_changed()
    
    def _new_channel(self):
        self._save_state("Создание нового канала")
        
        channel = ChannelData()
        channel.name = "Новый канал"
        channel.group = "Без группы"
        channel.update_extinf()
        
        self.all_channels.append(channel)
        
        self._apply_filter()
        
        if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
            self.parent_window._update_group_filter()
        
        self.modified = True
        self._update_modified_status()
        
        self.table.setCurrentCell(len(self.filtered_channels) - 1, 1)
        self.table.edit(self.table.currentIndex())
    
    def _select_all_channels(self):
        self.table.selectAll()
        self._on_selection_changed()
    
    def _delete_channel(self, row: int = -1):
        if row == -1:
            if not self.selected_channels:
                if not self.current_channel:
                    return
                channels_to_delete = [self.current_channel]
            else:
                channels_to_delete = self.selected_channels
        else:
            if 0 <= row < len(self.filtered_channels):
                channels_to_delete = [self.filtered_channels[row]]
            else:
                return
        
        if len(channels_to_delete) == 1:
            message = f"Удалить канал '{channels_to_delete[0].name}'?"
        else:
            message = f"Удалить выбранные {len(channels_to_delete)} каналов?"
        
        reply = QMessageBox.question(
            self, "Подтверждение", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление канала")
            
            for channel in channels_to_delete:
                if channel in self.all_channels:
                    self.all_channels.remove(channel)
            
            self._apply_filter()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def _delete_selected_channels(self):
        if not self.selected_channels:
            return
        
        self._delete_channel()
    
    def _add_to_blacklist(self, row: int = -1):
        if row == -1:
            if not self.current_channel:
                return
            channel_to_blacklist = self.current_channel
        else:
            if 0 <= row < len(self.filtered_channels):
                channel_to_blacklist = self.filtered_channels[row]
            else:
                return
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Добавить в чёрный список")
        dialog.resize(400, 200)
        
        layout = QVBoxLayout(dialog)
        
        info_label = QLabel(f"Добавить канал в чёрный список:")
        layout.addWidget(info_label)
        
        name_label = QLabel(f"Название: {channel_to_blacklist.name}")
        layout.addWidget(name_label)
        
        tvg_id_label = QLabel(f"TVG-ID: {channel_to_blacklist.tvg_id}")
        layout.addWidget(tvg_id_label)
        
        options_group = QGroupBox("Параметры фильтрации")
        options_layout = QVBoxLayout(options_group)
        
        self.use_name_check = QCheckBox("Фильтровать по названию")
        self.use_name_check.setChecked(True)
        options_layout.addWidget(self.use_name_check)
        
        self.use_tvg_id_check = QCheckBox("Фильтровать по TVG-ID")
        self.use_tvg_id_check.setChecked(bool(channel_to_blacklist.tvg_id))
        options_layout.addWidget(self.use_tvg_id_check)
        
        layout.addWidget(options_group)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = channel_to_blacklist.name if self.use_name_check.isChecked() else ""
            tvg_id = channel_to_blacklist.tvg_id if self.use_tvg_id_check.isChecked() else ""
            
            if not name and not tvg_id:
                return
            
            if self.blacklist_manager:
                if self.blacklist_manager.add_channel(name, tvg_id):
                    if channel_to_blacklist in self.all_channels:
                        self._save_state("Добавление в чёрный список")
                        self.all_channels.remove(channel_to_blacklist)
                        
                        self._apply_filter()
    
    def _add_selected_to_blacklist(self):
        if not self.selected_channels:
            return
        
        for channel in self.selected_channels:
            self._add_to_blacklist(self.filtered_channels.index(channel) if channel in self.filtered_channels else -1)
    
    def _move_channel_up(self, row: int = -1):
        if row == -1:
            if not self.current_channel:
                return
            channel_to_move = self.current_channel
        else:
            if 0 <= row < len(self.filtered_channels):
                channel_to_move = self.filtered_channels[row]
            else:
                return
        
        try:
            idx = self.all_channels.index(channel_to_move)
            self._move_channel_up_in_list(idx)
        except ValueError:
            pass
    
    def _move_selected_up(self):
        if not self.selected_channels:
            return
        
        indices = []
        for channel in self.selected_channels:
            try:
                idx = self.all_channels.index(channel)
                indices.append(idx)
            except ValueError:
                continue
        
        if not indices:
            return
        
        indices.sort()
        
        moved_count = 0
        for idx in indices:
            if idx > 0:
                if self._move_channel_up_in_list(idx - moved_count):
                    moved_count += 1
    
    def _move_channel_down(self, row: int = -1):
        if row == -1:
            if not self.current_channel:
                return
            channel_to_move = self.current_channel
        else:
            if 0 <= row < len(self.filtered_channels):
                channel_to_move = self.filtered_channels[row]
            else:
                return
        
        try:
            idx = self.all_channels.index(channel_to_move)
            self._move_channel_down_in_list(idx)
        except ValueError:
            pass
    
    def _move_selected_down(self):
        if not self.selected_channels:
            return
        
        indices = []
        for channel in self.selected_channels:
            try:
                idx = self.all_channels.index(channel)
                indices.append(idx)
            except ValueError:
                continue
        
        if not indices:
            return
        
        indices.sort(reverse=True)
        
        for idx in indices:
            if idx < len(self.all_channels) - 1:
                self._move_channel_down_in_list(idx)
    
    def _move_channel_up_in_list(self, idx: int):
        if idx <= 0:
            return False
        
        self._save_state("Перемещение канала вверх")
        
        self.all_channels[idx], self.all_channels[idx - 1] = \
            self.all_channels[idx - 1], self.all_channels[idx]
        
        self._apply_filter()
        
        selected_indices = []
        for channel in self.selected_channels:
            try:
                new_idx = self.all_channels.index(channel)
                selected_indices.append(new_idx)
            except ValueError:
                continue
        
        self.table.clearSelection()
        for new_idx in selected_indices:
            if new_idx < len(self.all_channels):
                channel = self.all_channels[new_idx]
                if channel in self.filtered_channels:
                    table_idx = self.filtered_channels.index(channel)
                    self.table.selectRow(table_idx)
        
        self.modified = True
        self._update_modified_status()
        
        return True
    
    def _move_channel_down_in_list(self, idx: int):
        if idx >= len(self.all_channels) - 1:
            return False
        
        self._save_state("Перемещение канала вниз")
        
        self.all_channels[idx], self.all_channels[idx + 1] = \
            self.all_channels[idx + 1], self.all_channels[idx]
        
        self._apply_filter()
        
        selected_indices = []
        for channel in self.selected_channels:
            try:
                new_idx = self.all_channels.index(channel)
                selected_indices.append(new_idx)
            except ValueError:
                continue
        
        self.table.clearSelection()
        for new_idx in selected_indices:
            if new_idx < len(self.all_channels):
                channel = self.all_channels[new_idx]
                if channel in self.filtered_channels:
                    table_idx = self.filtered_channels.index(channel)
                    self.table.selectRow(table_idx)
        
        self.modified = True
        self._update_modified_status()
        
        return True
    
    def _merge_duplicates(self):
        if not self.all_channels:
            return
        
        duplicates = {}
        for channel in self.all_channels:
            key = (channel.name, channel.group)
            if key not in duplicates:
                duplicates[key] = []
            duplicates[key].append(channel)
        
        dup_count = sum(len(channels) - 1 for channels in duplicates.values() if len(channels) > 1)
        
        if dup_count == 0:
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Найдено {dup_count} дубликатов. Удалить их?\n"
            f"Будет оставлен только первый канал из каждой группы дубликатов.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Объединение дубликатов")
            
            new_list = []
            seen = set()
            
            for channel in self.all_channels:
                key = (channel.name, channel.group)
                if key not in seen:
                    new_list.append(channel)
                    seen.add(key)
            
            removed = len(self.all_channels) - len(new_list)
            self.all_channels = new_list
            
            self._apply_filter()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def edit_playlist_header(self):
        dialog = PlaylistHeaderDialog(self.header_manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.modified = True
            self._update_modified_status()
    
    def apply_blacklist(self):
        if not self.blacklist_manager:
            return 0
        
        original_count = len(self.all_channels)
        filtered, removed = self.blacklist_manager.filter_channels(self.all_channels)
        
        if removed > 0:
            self._save_state("Применение чёрного списка")
            self.all_channels = filtered
            
            self._apply_filter()
            
            self.modified = True
            self._update_modified_status()
        
        return removed
    
    def save_to_file(self, filepath: str = None) -> bool:
        if filepath:
            self.filepath = filepath
        
        if not self.filepath:
            return False
        
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                header_text = self.header_manager.get_header_text()
                if header_text:
                    f.write(header_text)
                else:
                    f.write('#EXTM3U\n\n')
                
                for channel in self.all_channels:
                    f.write(channel.extinf + '\n')
                    
                    for extra_line in channel.extvlcopt_lines:
                        f.write(extra_line + '\n')
                    
                    if channel.url:
                        f.write(channel.url + '\n')
                    else:
                        f.write('\n')
            
            self.modified = False
            self._update_modified_status()
            return True
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить файл:\n{str(e)}")
            return False
    
    def is_empty(self) -> bool:
        return (len(self.all_channels) == 0 and 
                not self.filepath and 
                not self.modified)
    
    def remove_all_urls(self):
        if not self.all_channels:
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Удалить все ссылки из {len(self.all_channels)} каналов?\n"
            "Это действие нельзя отменить!",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление всех ссылок")
            
            for channel in self.all_channels:
                channel.url = ""
                channel.has_url = False
                channel.url_status = None
                channel.url_check_time = None
                channel.link_quality = LinkQuality.UNKNOWN
                channel.link_response_time = None
                channel.update_extinf()
            
            self._apply_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def remove_metadata(self, metadata_options: Dict[str, bool]):
        if not self.all_channels:
            return
        
        channels_to_modify = []
        
        for channel in self.all_channels:
            should_modify = False
            
            if metadata_options.get('tvg_id', False) and channel.tvg_id:
                should_modify = True
            if metadata_options.get('tvg_logo', False) and channel.tvg_logo:
                should_modify = True
            if metadata_options.get('group_title', False) and channel.group:
                should_modify = True
            if metadata_options.get('user_agent', False) and channel.user_agent:
                should_modify = True
            if metadata_options.get('catchup', False) and '#EXTINF' in channel.extinf and ('catchup=' in channel.extinf or 'catchup-days=' in channel.extinf or 'catchup-source=' in channel.extinf):
                should_modify = True
            
            if should_modify:
                channels_to_modify.append(channel)
        
        if not channels_to_modify:
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Удалить выбранные метаданные из {len(channels_to_modify)} каналов?\n"
            "Это действие нельзя отменить!",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление метаданных")
            
            for channel in channels_to_modify:
                if metadata_options.get('tvg_id', False):
                    channel.tvg_id = ""
                if metadata_options.get('tvg_logo', False):
                    channel.tvg_logo = ""
                if metadata_options.get('group_title', False):
                    channel.group = "Без группы"
                if metadata_options.get('user_agent', False):
                    channel.user_agent = ""
                    if 'User-Agent' in channel.extra_headers:
                        del channel.extra_headers['User-Agent']
                    channel.update_extvlcopt_from_headers()
                
                if metadata_options.get('catchup', False) and '#EXTINF' in channel.extinf:
                    extinf_line = channel.extinf
                    extinf_line = re.sub(r'catchup="[^"]*"', '', extinf_line)
                    extinf_line = re.sub(r'catchup-days="[^"]*"', '', extinf_line)
                    extinf_line = re.sub(r'catchup-source="[^"]*"', '', extinf_line)
                    extinf_line = re.sub(r'\s+', ' ', extinf_line).strip()
                    channel.extinf = extinf_line
                
                channel.update_extinf()
            
            self._apply_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def delete_channels_without_metadata(self):
        if not self.all_channels:
            return
        
        channels_without_metadata = []
        
        for channel in self.all_channels:
            has_metadata = (
                bool(channel.tvg_id) or
                bool(channel.tvg_logo) or
                bool(channel.group and channel.group != "Без группы") or
                bool(channel.user_agent) or
                bool('catchup=' in channel.extinf or 'catchup-days=' in channel.extinf or 'catchup-source=' in channel.extinf)
            )
            
            if not has_metadata:
                channels_without_metadata.append(channel)
        
        if not channels_without_metadata:
            return
        
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Найдено {len(channels_without_metadata)} каналов без метаданных. Удалить их?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._save_state("Удаление каналов без метаданных")
            
            for channel in channels_without_metadata:
                if channel in self.all_channels:
                    self.all_channels.remove(channel)
            
            self._apply_filter()
            
            if self.parent_window and hasattr(self.parent_window, '_update_group_filter'):
                self.parent_window._update_group_filter()
            
            self.modified = True
            self._update_modified_status()
    
    def replace_selected_links(self):
        if not self.selected_channels:
            return
        
        parent = self.parent_window
        if not parent or not hasattr(parent, 'link_source_manager'):
            return
        
        dialog = LinkReplacementDialog(self)
        dialog.set_data(
            self.selected_channels,
            parent.link_source_manager,
            parent.link_replacement_settings
        )
        
        def on_replacement_completed(replaced_channels):
            if replaced_channels:
                self._save_state("Замена ссылок выбранных каналов")
                self._apply_filter()
                self.modified = True
                self._update_modified_status()
        
        dialog.replacement_completed.connect(on_replacement_completed)
        dialog.exec()
    
    def replace_single_link(self, row: int):
        if 0 <= row < len(self.filtered_channels):
            channel = self.filtered_channels[row]
            
            parent = self.parent_window
            if not parent or not hasattr(parent, 'link_source_manager'):
                return
            
            dialog = LinkReplacementDialog(self)
            dialog.set_data(
                [channel],
                parent.link_source_manager,
                parent.link_replacement_settings
            )
            
            def on_replacement_completed(replaced_channels):
                if replaced_channels:
                    self._save_state(f"Замена ссылки канала '{channel.name}'")
                    self._apply_filter()
                    self.modified = True
                    self._update_modified_status()
            
            dialog.replacement_completed.connect(on_replacement_completed)
            dialog.exec()
    
    def show_link_history(self, row: int):
        if 0 <= row < len(self.filtered_channels):
            channel = self.filtered_channels[row]
            
            dialog = LinkHistoryDialog(channel, self)
            dialog.exec()


class MainWindow(QMainWindow):
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ksenia M3U Editor")
        self.resize(1400, 900)
        
        self.tabs: Dict[QWidget, PlaylistTab] = {}
        self.current_tab = None
        
        self.copied_channel = None
        self.copied_channels = None
        self.copied_metadata = None
        self.copied_metadata_list = None
        
        self.blacklist_manager = BlacklistManager()
        self.link_source_manager = LinkSourceManager()
        self.link_replacement_settings = LinkReplacementSettings()
        
        self._load_ip_filter_settings()
        
        self.recent_files = []
        
        self._setup_ui()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_statusbar()
        
        self._load_settings()
        self._update_window_title()
        
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    
    def _load_ip_filter_settings(self):
        settings = QSettings("Ksenia", "M3UEditor")
        
        blacklist_ips = settings.value("blacklist_ips", [])
        if isinstance(blacklist_ips, list):
            self.link_replacement_settings.blacklisted_ips = blacklist_ips
        else:
            self.link_replacement_settings.blacklisted_ips = []
        
        blacklist_domains = settings.value("blacklist_domains", [])
        if isinstance(blacklist_domains, list):
            self.link_replacement_settings.blacklisted_domains = blacklist_domains
        else:
            self.link_replacement_settings.blacklisted_domains = []
        
        whitelist_ips = settings.value("whitelist_ips", [])
        if isinstance(whitelist_ips, list):
            self.link_replacement_settings.whitelisted_ips = whitelist_ips
        else:
            self.link_replacement_settings.whitelisted_ips = []
        
        whitelist_domains = settings.value("whitelist_domains", [])
        if isinstance(whitelist_domains, list):
            self.link_replacement_settings.whitelisted_domains = whitelist_domains
        else:
            self.link_replacement_settings.whitelisted_domains = []
        
        self.link_replacement_settings.ignore_special_chars_in_names = settings.value("ignore_special_chars_in_names", True, type=bool)
        self.link_replacement_settings.remove_parentheses_in_names = settings.value("remove_parentheses_in_names", True, type=bool)
        self.link_replacement_settings.remove_brackets_in_names = settings.value("remove_brackets_in_names", True, type=bool)
        self.link_replacement_settings.remove_emojis_in_names = settings.value("remove_emojis_in_names", True, type=bool)
    
    def _save_ip_filter_settings(self):
        settings = QSettings("Ksenia", "M3UEditor")
        
        settings.setValue("blacklist_ips", self.link_replacement_settings.blacklisted_ips)
        settings.setValue("blacklist_domains", self.link_replacement_settings.blacklisted_domains)
        settings.setValue("whitelist_ips", self.link_replacement_settings.whitelisted_ips)
        settings.setValue("whitelist_domains", self.link_replacement_settings.whitelisted_domains)
        
        settings.setValue("ignore_special_chars_in_names", self.link_replacement_settings.ignore_special_chars_in_names)
        settings.setValue("remove_parentheses_in_names", self.link_replacement_settings.remove_parentheses_in_names)
        settings.setValue("remove_brackets_in_names", self.link_replacement_settings.remove_brackets_in_names)
        settings.setValue("remove_emojis_in_names", self.link_replacement_settings.remove_emojis_in_names)
    
    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        
        main_layout.addWidget(self.tab_widget)
        
        filter_layout = QHBoxLayout()
        
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по названию, группе, TVG-ID, URL...")
        self.search_edit.textChanged.connect(self._apply_filters)
        filter_layout.addWidget(self.search_edit)
        
        self.group_combo = QComboBox()
        self.group_combo.addItem("Все группы")
        self.group_combo.currentTextChanged.connect(self._apply_filters)
        filter_layout.addWidget(self.group_combo)
        
        main_layout.addLayout(filter_layout)
    
    def _setup_menu(self):
        menu_bar = self.menuBar()
        
        file_menu = menu_bar.addMenu("Файл")
        
        new_action = QAction("Новый", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._new_file)
        file_menu.addAction(new_action)
        
        open_action = QAction("Открыть", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._open_file)
        file_menu.addAction(open_action)
        
        create_from_sources_action = QAction("Создать плейлист из источников", self)
        create_from_sources_action.triggered.connect(self._create_playlist_from_sources)
        file_menu.addAction(create_from_sources_action)
        
        self.recent_menu = QMenu("Открыть недавние", self)
        file_menu.addMenu(self.recent_menu)
        
        file_menu.addSeparator()
        
        save_action = QAction("Сохранить", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._save_file)
        file_menu.addAction(save_action)
        
        save_as_action = QAction("Сохранить как", self)
        save_as_action.setShortcut("Ctrl+Shift+S")
        save_as_action.triggered.connect(self._save_as)
        file_menu.addAction(save_as_action)
        
        save_all_action = QAction("Сохранить всё", self)
        save_all_action.triggered.connect(self._save_all)
        file_menu.addAction(save_all_action)
        
        file_menu.addSeparator()
        
        import_action = QAction("Импорт", self)
        import_action.triggered.connect(self._import_file)
        file_menu.addAction(import_action)
        
        export_action = QAction("Экспорт", self)
        export_action.triggered.connect(self._export_file)
        file_menu.addAction(export_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("Выход", self)
        exit_action.setShortcut("Alt+F4")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        edit_menu = menu_bar.addMenu("Правка")
        
        undo_action = QAction("Отменить", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self._undo)
        undo_action.setEnabled(False)
        self.undo_action = undo_action
        edit_menu.addAction(undo_action)
        
        redo_action = QAction("Повторить", self)
        redo_action.setShortcut("Ctrl+Y")
        redo_action.triggered.connect(self._redo)
        redo_action.setEnabled(False)
        self.redo_action = redo_action
        edit_menu.addAction(redo_action)
        
        edit_menu.addSeparator()
        
        select_all_action = QAction("Выделить всё", self)
        select_all_action.setShortcut("Ctrl+A")
        select_all_action.triggered.connect(self._select_all)
        edit_menu.addAction(select_all_action)
        
        channels_menu = menu_bar.addMenu("Каналы")
        
        new_channel_action = QAction("Новый канал", self)
        new_channel_action.setShortcut("Ctrl+Shift+A")
        new_channel_action.triggered.connect(self._new_channel)
        channels_menu.addAction(new_channel_action)
        
        cut_channel_action = QAction("Вырезать канал", self)
        cut_channel_action.setShortcut("Ctrl+X")
        cut_channel_action.triggered.connect(self._cut_channel)
        channels_menu.addAction(cut_channel_action)
        
        copy_channel_action = QAction("Копировать канал", self)
        copy_channel_action.setShortcut("Ctrl+C")
        copy_channel_action.triggered.connect(self._copy_channel)
        channels_menu.addAction(copy_channel_action)
        
        paste_channel_action = QAction("Вставить канал", self)
        paste_channel_action.setShortcut("Ctrl+V")
        paste_channel_action.triggered.connect(self._paste_channel)
        channels_menu.addAction(paste_channel_action)
        
        channels_menu.addSeparator()
        
        copy_metadata_action = QAction("Копировать метаданные", self)
        copy_metadata_action.triggered.connect(self._copy_metadata)
        channels_menu.addAction(copy_metadata_action)
        
        paste_metadata_action = QAction("Вставить метаданные", self)
        paste_metadata_action.triggered.connect(self._paste_metadata)
        channels_menu.addAction(paste_metadata_action)
        
        channels_menu.addSeparator()
        
        delete_channels_without_urls_action = QAction("Удалить каналы без ссылок", self)
        delete_channels_without_urls_action.triggered.connect(self._delete_channels_without_urls)
        channels_menu.addAction(delete_channels_without_urls_action)
        
        delete_channels_without_metadata_action = QAction("Удалить каналы без метаданных", self)
        delete_channels_without_metadata_action.triggered.connect(self._delete_channels_without_metadata)
        channels_menu.addAction(delete_channels_without_metadata_action)
        
        channels_menu.addSeparator()
        
        move_up_action = QAction("Переместить вверх", self)
        move_up_action.triggered.connect(self._move_channel_up)
        channels_menu.addAction(move_up_action)
        
        move_down_action = QAction("Переместить вниз", self)
        move_down_action.triggered.connect(self._move_channel_down)
        channels_menu.addAction(move_down_action)
        
        channels_menu.addSeparator()
        
        remove_metadata_action = QAction("Удалить метаданные...", self)
        remove_metadata_action.triggered.connect(self._remove_metadata)
        channels_menu.addAction(remove_metadata_action)
        
        links_menu = menu_bar.addMenu("Ссылки")
        
        remove_all_urls_action = QAction("Удалить все ссылки", self)
        remove_all_urls_action.triggered.connect(self._remove_all_urls)
        links_menu.addAction(remove_all_urls_action)
        
        links_menu.addSeparator()
        
        check_all_urls_action = QAction("Проверить все ссылки", self)
        check_all_urls_action.triggered.connect(self._check_all_urls)
        links_menu.addAction(check_all_urls_action)
        
        check_selected_urls_action = QAction("Проверить выбранные ссылки", self)
        check_selected_urls_action.triggered.connect(self._check_selected_urls)
        links_menu.addAction(check_selected_urls_action)
        
        links_menu.addSeparator()
        
        replace_links_action = QAction("Заменить ссылки...", self)
        replace_links_action.triggered.connect(self._replace_links)
        links_menu.addAction(replace_links_action)
        
        replace_selected_links_action = QAction("Заменить ссылки выбранных", self)
        replace_selected_links_action.triggered.connect(self._replace_selected_links)
        links_menu.addAction(replace_selected_links_action)
        
        links_menu.addSeparator()
        
        remove_selected_broken_urls_action = QAction("Удалить битые ссылки у выбранных", self)
        remove_selected_broken_urls_action.triggered.connect(self._remove_selected_broken_urls)
        links_menu.addAction(remove_selected_broken_urls_action)
        
        remove_all_broken_urls_action = QAction("Удалить все битые ссылки", self)
        remove_all_broken_urls_action.triggered.connect(self._remove_all_broken_urls)
        links_menu.addAction(remove_all_broken_urls_action)
        
        tools_menu = menu_bar.addMenu("Инструменты")
        
        manage_duplicates_action = QAction("Управление дубликатами...", self)
        manage_duplicates_action.triggered.connect(self._manage_duplicates)
        tools_menu.addAction(manage_duplicates_action)
        
        tools_menu.addSeparator()
        
        edit_header_action = QAction("Редактировать заголовок...", self)
        edit_header_action.triggered.connect(self._edit_playlist_header)
        tools_menu.addAction(edit_header_action)
        
        apply_blacklist_action = QAction("Применить чёрный список", self)
        apply_blacklist_action.triggered.connect(self._apply_blacklist)
        tools_menu.addAction(apply_blacklist_action)
        
        tools_menu.addSeparator()
        
        manage_blacklist_action = QAction("Менеджер чёрного списка", self)
        manage_blacklist_action.triggered.connect(self._manage_blacklist)
        tools_menu.addAction(manage_blacklist_action)
        
        manage_link_sources_action = QAction("Источники ссылок", self)
        manage_link_sources_action.triggered.connect(self._manage_link_sources)
        tools_menu.addAction(manage_link_sources_action)
        
        copy_metadata_between_action = QAction("Копирование метаданных между плейлистами...", self)
        copy_metadata_between_action.triggered.connect(self._copy_metadata_between_playlists)
        tools_menu.addAction(copy_metadata_between_action)
        
        settings_menu = menu_bar.addMenu("Настройки")
        
        link_replacement_settings_action = QAction("Настройки замены ссылок", self)
        link_replacement_settings_action.triggered.connect(self._manage_link_replacement_settings)
        settings_menu.addAction(link_replacement_settings_action)
        
        settings_menu.addSeparator()
        
        zoom_in_action = QAction("Увеличить", self)
        zoom_in_action.setShortcut("Ctrl++")
        zoom_in_action.triggered.connect(self._zoom_in)
        settings_menu.addAction(zoom_in_action)
        
        zoom_out_action = QAction("Уменьшить", self)
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(self._zoom_out)
        settings_menu.addAction(zoom_out_action)
        
        reset_zoom_action = QAction("Сбросить масштаб", self)
        reset_zoom_action.setShortcut("Ctrl+0")
        reset_zoom_action.triggered.connect(self._reset_zoom)
        settings_menu.addAction(reset_zoom_action)
        
        settings_menu.addSeparator()
        
        toggle_toolbar_action = QAction("Показать/скрыть панель инструментов", self)
        toggle_toolbar_action.setCheckable(True)
        toggle_toolbar_action.setChecked(True)
        toggle_toolbar_action.triggered.connect(self._toggle_toolbar)
        settings_menu.addAction(toggle_toolbar_action)
        
        toggle_statusbar_action = QAction("Показать/скрыть строку состояния", self)
        toggle_statusbar_action.setCheckable(True)
        toggle_statusbar_action.setChecked(True)
        toggle_statusbar_action.triggered.connect(self._toggle_statusbar)
        settings_menu.addAction(toggle_statusbar_action)
    
    def _setup_toolbar(self):
        self.toolbar = QToolBar("Основная панель")
        self.toolbar.setObjectName("mainToolbar")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)
        
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        
        new_action = QAction(icon, "Новый", self)
        new_action.triggered.connect(self._new_file)
        self.toolbar.addAction(new_action)
        
        open_action = QAction(QIcon.fromTheme("document-open", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)), "Открыть", self)
        open_action.triggered.connect(self._open_file)
        self.toolbar.addAction(open_action)
        
        save_action = QAction(QIcon.fromTheme("document-save", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)), "Сохранить", self)
        save_action.triggered.connect(self._save_file)
        self.toolbar.addAction(save_action)
        
        self.toolbar.addSeparator()
        
        undo_action = QAction(QIcon.fromTheme("edit-undo", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)), "Отменить", self)
        undo_action.triggered.connect(self._undo)
        self.toolbar_undo_action = undo_action
        self.toolbar.addAction(undo_action)
        
        redo_action = QAction(QIcon.fromTheme("edit-redo", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)), "Повторить", self)
        redo_action.triggered.connect(self._redo)
        self.toolbar_redo_action = redo_action
        self.toolbar.addAction(redo_action)
        
        self.toolbar.addSeparator()
        
        move_up_action = QAction(QIcon.fromTheme("go-up", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp)), "Переместить вверх", self)
        move_up_action.triggered.connect(self._move_channel_up)
        self.toolbar.addAction(move_up_action)
        
        move_down_action = QAction(QIcon.fromTheme("go-down", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)), "Переместить вниз", self)
        move_down_action.triggered.connect(self._move_channel_down)
        self.toolbar.addAction(move_down_action)
        
        self.toolbar.addSeparator()
        
        copy_action = QAction(QIcon.fromTheme("edit-copy", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogYesButton)), "Копировать", self)
        copy_action.triggered.connect(self._copy)
        self.toolbar.addAction(copy_action)
        
        cut_action = QAction(QIcon.fromTheme("edit-cut", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton)), "Вырезать", self)
        cut_action.triggered.connect(self._cut)
        self.toolbar.addAction(cut_action)
        
        paste_action = QAction(QIcon.fromTheme("edit-paste", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)), "Вставить", self)
        paste_action.triggered.connect(self._paste)
        self.toolbar.addAction(paste_action)
        
        self.toolbar.addSeparator()
        
        check_urls_action = QAction(QIcon.fromTheme("network-connect", self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOkButton)), "Проверить ссылки", self)
        check_urls_action.triggered.connect(self._check_selected_urls)
        self.toolbar.addAction(check_urls_action)
        
        self.toolbar.addSeparator()
        
        zoom_in_action = QAction(QIcon.fromTheme("zoom-in", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp)), "Увеличить", self)
        zoom_in_action.triggered.connect(self._zoom_in)
        self.toolbar.addAction(zoom_in_action)
        
        zoom_out_action = QAction(QIcon.fromTheme("zoom-out", self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)), "Уменьшить", self)
        zoom_out_action.triggered.connect(self._zoom_out)
        self.toolbar.addAction(zoom_out_action)
    
    def _setup_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        self.status_label = QLabel("Готов")
        self.status_bar.addWidget(self.status_label)
        
        self.modified_label = QLabel("")
        self.status_bar.addWidget(self.modified_label)
        
        self.info_label = QLabel("")
        self.status_bar.addPermanentWidget(self.info_label)
    
    def _load_settings(self):
        settings = QSettings("Ksenia", "M3UEditor")
        
        window_geometry = settings.value("window_geometry")
        if window_geometry:
            self.restoreGeometry(window_geometry)
        
        window_state = settings.value("window_state")
        if window_state:
            self.restoreState(window_state)
        
        recent_files = settings.value("recent_files", [])
        if recent_files:
            if isinstance(recent_files, list):
                self.recent_files = recent_files
            else:
                self.recent_files = []
        else:
            self.recent_files = []
        
        self._update_recent_menu()
    
    def _save_settings(self):
        settings = QSettings("Ksenia", "M3UEditor")
        
        settings.setValue("window_geometry", self.saveGeometry())
        settings.setValue("window_state", self.saveState())
        settings.setValue("recent_files", self.recent_files)
        
        self._save_ip_filter_settings()
    
    def _update_recent_menu(self):
        self.recent_menu.clear()
        
        if not self.recent_files:
            no_files_action = QAction("Нет недавних файлов", self)
            no_files_action.setEnabled(False)
            self.recent_menu.addAction(no_files_action)
            return
        
        for filepath in self.recent_files:
            if os.path.exists(filepath):
                action = QAction(os.path.basename(filepath), self)
                action.setToolTip(filepath)
                action.triggered.connect(lambda checked, fp=filepath: self._open_recent_file(fp))
                self.recent_menu.addAction(action)
        
        self.recent_menu.addSeparator()
        
        clear_action = QAction("Очистить список", self)
        clear_action.triggered.connect(self._clear_recent_files)
        self.recent_menu.addAction(clear_action)
    
    def _add_to_recent(self, filepath):
        if filepath in self.recent_files:
            self.recent_files.remove(filepath)
        
        self.recent_files.insert(0, filepath)
        
        if len(self.recent_files) > 10:
            self.recent_files = self.recent_files[:10]
        
        self._update_recent_menu()
    
    def _open_recent_file(self, filepath):
        if not os.path.exists(filepath):
            QMessageBox.warning(self, "Ошибка", f"Файл не существует:\n{filepath}")
            self.recent_files.remove(filepath)
            self._update_recent_menu()
            return
        
        self._open_file_in_tab(filepath)
    
    def _clear_recent_files(self):
        self.recent_files.clear()
        self._update_recent_menu()
    
    def _update_window_title(self):
        if self.current_tab:
            filename = os.path.basename(self.current_tab.filepath) if self.current_tab.filepath else "Безымянный"
            modified = " *" if self.current_tab.modified else ""
            self.setWindowTitle(f"{filename}{modified} - Ksenia M3U Editor")
        else:
            self.setWindowTitle("Ksenia M3U Editor")
    
    def _new_file(self):
        tab = PlaylistTab(blacklist_manager=self.blacklist_manager)
        tab.parent_window = self
        
        index = self.tab_widget.addTab(tab, "Безымянный")
        self.tabs[tab] = tab
        self.tab_widget.setCurrentIndex(index)
        
        tab.undo_state_changed.connect(self._on_undo_state_changed)
        tab.info_changed.connect(self._on_info_changed)
        
        self.current_tab = tab
        self._update_window_title()
        self._update_group_filter()
    
    def _open_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Открыть M3U файл", "",
            "M3U файлы (*.m3u *.m3u8);;Все файлы (*.*)"
        )
        
        if filepath:
            self._open_file_in_tab(filepath)
    
    def _open_file_in_tab(self, filepath: str):
        for tab in self.tabs.values():
            if tab.filepath == filepath:
                index = self.tab_widget.indexOf(tab)
                self.tab_widget.setCurrentIndex(index)
                return
        
        try:
            tab = PlaylistTab(filepath, blacklist_manager=self.blacklist_manager)
            tab.parent_window = self
            
            filename = os.path.basename(filepath)
            index = self.tab_widget.addTab(tab, filename)
            self.tabs[tab] = tab
            self.tab_widget.setCurrentIndex(index)
            
            tab.undo_state_changed.connect(self._on_undo_state_changed)
            tab.info_changed.connect(self._on_info_changed)
            
            self.current_tab = tab
            self._update_window_title()
            self._update_group_filter()
            
            self._add_to_recent(filepath)
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось открыть файл:\n{str(e)}")
    
    def _create_playlist_from_sources(self):
        sources = self.link_source_manager.get_enabled_sources()
        
        if not sources:
            QMessageBox.warning(self, "Предупреждение", "Нет включенных источников ссылок")
            return
        
        progress_dialog = QProgressDialog("Загрузка плейлистов из источников...", "Отмена", 0, len(sources), self)
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        
        all_channels = []
        failed_sources = []
        
        for i, source in enumerate(sources):
            progress_dialog.setValue(i)
            progress_dialog.setLabelText(f"Загрузка: {source.name}")
            
            if progress_dialog.wasCanceled():
                break
            
            try:
                channels = self.link_source_manager.load_links_from_source(source)
                if channels:
                    for channel in channels:
                        channel.link_source = source.name
                    all_channels.extend(channels)
                else:
                    failed_sources.append(source.name)
                time.sleep(0.1)
            except Exception as e:
                logger.error(f"Ошибка загрузки источника {source.name}: {e}")
                failed_sources.append(source.name)
        
        progress_dialog.setValue(len(sources))
        
        if not all_channels:
            QMessageBox.warning(self, "Предупреждение", "Не удалось загрузить каналы из источников")
            return
        
        self._new_file()
        
        if self.current_tab:
            self.current_tab._save_state("Создание плейлиста из источников")
            self.current_tab.all_channels = all_channels
            self.current_tab._apply_filter()
            self.current_tab.modified = True
            self.current_tab._update_modified_status()
            
            if failed_sources:
                QMessageBox.warning(
                    self, "Частичная загрузка",
                    f"Загружено {len(all_channels)} каналов\n"
                    f"Не удалось загрузить: {', '.join(failed_sources)}"
                )
            else:
                QMessageBox.information(
                    self, "Успех",
                    f"Загружено {len(all_channels)} каналов из {len(sources)} источников"
                )
    
    def _save_file(self):
        if not self.current_tab:
            return
        
        if not self.current_tab.filepath:
            self._save_as()
        else:
            if self.current_tab.save_to_file():
                self.status_bar.showMessage("Файл сохранён", 3000)
                self._update_window_title()
    
    def _save_as(self):
        if not self.current_tab:
            return
        
        default_name = self.current_tab.filepath or "playlist.m3u"
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Сохранить как", default_name,
            "M3U файлы (*.m3u *.m3u8);;Все файлы (*.*)"
        )
        
        if filepath:
            if not filepath.lower().endswith(('.m3u', '.m3u8')):
                filepath += '.m3u'
            
            if self.current_tab.save_to_file(filepath):
                filename = os.path.basename(filepath)
                index = self.tab_widget.currentIndex()
                self.tab_widget.setTabText(index, filename)
                
                self.status_bar.showMessage("Файл сохранён", 3000)
                self._update_window_title()
                self._add_to_recent(filepath)
    
    def _save_all(self):
        for tab in self.tabs.values():
            if tab.modified and tab.filepath:
                if tab.save_to_file():
                    pass
        
        self.status_bar.showMessage("Все файлы сохранены", 3000)
        self._update_window_title()
    
    def _import_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Импорт каналов", "",
            "Все файлы (*.*)"
        )
        
        if filepath:
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                
                if not self.current_tab:
                    self._new_file()
                
                tab = self.current_tab
                tab._parse_m3u(content)
                tab._apply_filter()
                tab._update_info()
                tab.modified = True
                tab._update_modified_status()
                
                self._update_group_filter()
                
                self.status_bar.showMessage(f"Импортировано {len(tab.all_channels)} каналов", 3000)
                
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось импортировать файл:\n{str(e)}")
    
    def _export_file(self):
        if not self.current_tab:
            return
        
        default_name = self.current_tab.filepath or "playlist_export.m3u"
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Экспорт каналов", default_name,
            "M3U файлы (*.m3u *.m3u8);;Текстовые файлы (*.txt);;Все файлы (*.*)"
        )
        
        if filepath:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write('#EXTM3U\n\n')
                    for channel in self.current_tab.all_channels:
                        f.write(channel.extinf + '\n')
                        for extra_line in channel.extvlcopt_lines:
                            f.write(extra_line + '\n')
                        if channel.url:
                            f.write(channel.url + '\n')
                        else:
                            f.write('\n')
                
                self.status_bar.showMessage(f"Экспортировано {len(self.current_tab.all_channels)} каналов", 3000)
                
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось экспортировать файл:\n{str(e)}")
    
    def _close_tab(self, index: int):
        widget = self.tab_widget.widget(index)
        
        if widget in self.tabs:
            tab = self.tabs[widget]
            
            if tab.modified:
                reply = QMessageBox.question(
                    self, "Подтверждение",
                    "Файл был изменён. Сохранить изменения?",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No |
                    QMessageBox.StandardButton.Cancel
                )
                
                if reply == QMessageBox.StandardButton.Yes:
                    if not tab.filepath:
                        default_name = "playlist.m3u"
                        filepath, _ = QFileDialog.getSaveFileName(
                            self, "Сохранить как", default_name,
                            "M3U файлы (*.m3u *.m3u8);;Все файлы (*.*)"
                        )
                        
                        if filepath:
                            if not filepath.lower().endswith(('.m3u', '.m3u8')):
                                filepath += '.m3u'
                            tab.filepath = filepath
                        else:
                            return
                    
                    if not tab.save_to_file():
                        return
                
                elif reply == QMessageBox.StandardButton.Cancel:
                    return
            
            tab.undo_state_changed.disconnect()
            tab.info_changed.disconnect()
            
            del self.tabs[widget]
            self.tab_widget.removeTab(index)
            
            if self.tab_widget.count() == 0:
                self.current_tab = None
                self._update_window_title()
                self._update_group_filter()
                self._on_info_changed("Готов")
                self._on_undo_state_changed(False, False)
    
    def _on_tab_changed(self, index: int):
        if index >= 0:
            widget = self.tab_widget.widget(index)
            if widget in self.tabs:
                self.current_tab = self.tabs[widget]
                self._update_window_title()
                self._update_group_filter()
                self.current_tab._update_info()
                
                if hasattr(self.current_tab, 'undo_manager'):
                    self._on_undo_state_changed(
                        self.current_tab.undo_manager.can_undo(),
                        self.current_tab.undo_manager.can_redo()
                    )
        else:
            self.current_tab = None
            self._update_window_title()
            self._update_group_filter()
            self._on_info_changed("Готов")
            self._on_undo_state_changed(False, False)
    
    def _apply_filters(self):
        if self.current_tab:
            self.current_tab._apply_filter()
    
    def _update_group_filter(self):
        self.group_combo.clear()
        self.group_combo.addItem("Все группы")
        
        if self.current_tab:
            groups = set()
            for channel in self.current_tab.all_channels:
                if channel.group:
                    groups.add(channel.group)
            
            for group in sorted(groups):
                self.group_combo.addItem(group)
    
    def _undo(self):
        if self.current_tab:
            self.current_tab._undo()
    
    def _redo(self):
        if self.current_tab:
            self.current_tab._redo()
    
    def _on_undo_state_changed(self, can_undo: bool, can_redo: bool):
        self.undo_action.setEnabled(can_undo)
        self.toolbar_undo_action.setEnabled(can_undo)
        self.redo_action.setEnabled(can_redo)
        self.toolbar_redo_action.setEnabled(can_redo)
    
    def _cut(self):
        if self.current_tab:
            self.current_tab._cut_channel()
    
    def _copy(self):
        if self.current_tab:
            self.current_tab._copy_channel()
    
    def _paste(self):
        if self.current_tab:
            self.current_tab._paste_channel()
    
    def _delete(self):
        if self.current_tab:
            self.current_tab._delete_channel()
    
    def _select_all(self):
        if self.current_tab:
            self.current_tab._select_all_channels()
    
    def _new_channel(self):
        if self.current_tab:
            self.current_tab._new_channel()
    
    def _cut_channel(self):
        if self.current_tab:
            self.current_tab._cut_channel()
    
    def _copy_channel(self):
        if self.current_tab:
            self.current_tab._copy_channel()
    
    def _copy_metadata(self):
        if self.current_tab:
            self.current_tab._copy_metadata()
    
    def _paste_channel(self):
        if self.current_tab:
            self.current_tab._paste_channel()
    
    def _paste_metadata(self):
        if self.current_tab:
            self.current_tab._paste_metadata()
    
    def _move_channel_up(self):
        if self.current_tab:
            self.current_tab._move_channel_up()
    
    def _move_channel_down(self):
        if self.current_tab:
            self.current_tab._move_channel_down()
    
    def _remove_all_urls(self):
        if self.current_tab:
            self.current_tab.remove_all_urls()
    
    def _remove_metadata(self):
        if not self.current_tab:
            return
        
        dialog = RemoveMetadataDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            metadata_options = dialog.get_metadata_options()
            selection_scope = dialog.get_selection_scope()
            
            if selection_scope == "current":
                if self.current_tab.current_channel:
                    channels_to_modify = [self.current_tab.current_channel]
                else:
                    return
            elif selection_scope == "selected":
                channels_to_modify = self.current_tab.selected_channels.copy()
            else:
                channels_to_modify = self.current_tab.all_channels.copy()
            
            if not channels_to_modify:
                return
            
            original_count = len(channels_to_modify)
            self.current_tab._save_state("Удаление метаданных")
            
            modified_count = 0
            for channel in channels_to_modify:
                modified = False
                
                if metadata_options.get('tvg_id', False) and channel.tvg_id:
                    channel.tvg_id = ""
                    modified = True
                
                if metadata_options.get('tvg_logo', False) and channel.tvg_logo:
                    channel.tvg_logo = ""
                    modified = True
                
                if metadata_options.get('group_title', False) and channel.group:
                    channel.group = "Без группы"
                    modified = True
                
                if metadata_options.get('user_agent', False) and channel.user_agent:
                    channel.user_agent = ""
                    if 'User-Agent' in channel.extra_headers:
                        del channel.extra_headers['User-Agent']
                    channel.update_extvlcopt_from_headers()
                    modified = True
                
                if metadata_options.get('catchup', False) and '#EXTINF' in channel.extinf:
                    extinf_line = channel.extinf
                    extinf_line = re.sub(r'catchup="[^"]*"', '', extinf_line)
                    extinf_line = re.sub(r'catchup-days="[^"]*"', '', extinf_line)
                    extinf_line = re.sub(r'catchup-source="[^"]*"', '', extinf_line)
                    extinf_line = re.sub(r'\s+', ' ', extinf_line).strip()
                    channel.extinf = extinf_line
                    modified = True
                
                if modified:
                    channel.update_extinf()
                    modified_count += 1
            
            self.current_tab._apply_filter()
            self.current_tab.modified = True
            self.current_tab._update_modified_status()
            
            self.status_bar.showMessage(f"Удалены метаданные у {modified_count} каналов", 3000)
    
    def _check_all_urls(self):
        if self.current_tab:
            self.current_tab.check_all_urls()
    
    def _check_selected_urls(self):
        if self.current_tab:
            self.current_tab._check_selected_urls()
    
    def _manage_duplicates(self):
        if not self.current_tab:
            return
        
        dialog = DuplicateManagerDialog(self.current_tab, self)
        dialog.exec()
    
    def _replace_links(self):
        if not self.current_tab:
            return
        
        if not self.current_tab.all_channels:
            return
        
        dialog = LinkReplacementDialog(self)
        dialog.set_data(
            self.current_tab.all_channels,
            self.link_source_manager,
            self.link_replacement_settings
        )
        
        def on_replacement_completed(replaced_channels):
            if replaced_channels:
                self.current_tab._save_state("Массовая замена ссылок")
                self.current_tab._apply_filter()
                self.current_tab.modified = True
                self.current_tab._update_modified_status()
                self.status_bar.showMessage(f"Заменено {len(replaced_channels)} ссылок", 3000)
        
        dialog.replacement_completed.connect(on_replacement_completed)
        dialog.exec()
    
    def _replace_selected_links(self):
        if self.current_tab:
            self.current_tab.replace_selected_links()
    
    def _delete_channels_without_urls(self):
        if self.current_tab:
            self.current_tab.delete_channels_without_urls()
    
    def _delete_channels_without_metadata(self):
        if self.current_tab:
            self.current_tab.delete_channels_without_metadata()
    
    def _remove_selected_broken_urls(self):
        if self.current_tab:
            self.current_tab._remove_selected_broken_urls()
    
    def _remove_all_broken_urls(self):
        if self.current_tab:
            self.current_tab._remove_all_broken_urls()
    
    def _edit_playlist_header(self):
        if self.current_tab:
            self.current_tab.edit_playlist_header()
    
    def _apply_blacklist(self):
        if self.current_tab:
            removed_count = self.current_tab.apply_blacklist()
            if removed_count > 0:
                self.status_bar.showMessage(f"Удалено {removed_count} каналов по чёрному списку", 3000)
    
    def _manage_blacklist(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Менеджер чёрного списка")
        dialog.resize(800, 600)
        
        layout = QVBoxLayout(dialog)
        
        self.blacklist_table = QTableWidget()
        self.blacklist_table.setColumnCount(3)
        self.blacklist_table.setHorizontalHeaderLabels(["Название канала", "TVG-ID", "Дата добавления"])
        self.blacklist_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.blacklist_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        
        header = self.blacklist_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        
        layout.addWidget(self.blacklist_table)
        
        btn_layout = QHBoxLayout()
        
        add_btn = QPushButton("Добавить")
        add_btn.clicked.connect(self._add_to_blacklist_manual)
        btn_layout.addWidget(add_btn)
        
        remove_btn = QPushButton("Удалить выбранное")
        remove_btn.clicked.connect(self._remove_from_blacklist)
        btn_layout.addWidget(remove_btn)
        
        clear_btn = QPushButton("Очистить список")
        clear_btn.clicked.connect(self._clear_blacklist)
        btn_layout.addWidget(clear_btn)
        
        import_btn = QPushButton("Импорт")
        import_btn.clicked.connect(self._import_blacklist)
        btn_layout.addWidget(import_btn)
        
        export_btn = QPushButton("Экспорт")
        export_btn.clicked.connect(self._export_blacklist)
        btn_layout.addWidget(export_btn)
        
        layout.addLayout(btn_layout)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        self._load_blacklist_to_table()
        dialog.exec()
    
    def _load_blacklist_to_table(self):
        blacklist_items = self.blacklist_manager.get_all()
        self.blacklist_table.setRowCount(len(blacklist_items))
        
        for i, item in enumerate(blacklist_items):
            self.blacklist_table.setItem(i, 0, QTableWidgetItem(item.get('name', '')))
            self.blacklist_table.setItem(i, 1, QTableWidgetItem(item.get('tvg_id', '')))
            self.blacklist_table.setItem(i, 2, QTableWidgetItem(item.get('added_date', '')))
    
    def _add_to_blacklist_manual(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Добавить в чёрный список")
        dialog.resize(400, 200)
        
        layout = QVBoxLayout(dialog)
        
        form_layout = QFormLayout()
        
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("Название канала")
        form_layout.addRow("Название:", name_edit)
        
        tvg_id_edit = QLineEdit()
        tvg_id_edit.setPlaceholderText("TVG-ID (опционально)")
        form_layout.addRow("TVG-ID:", tvg_id_edit)
        
        layout.addLayout(form_layout)
        
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        
        layout.addWidget(button_box)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = name_edit.text().strip()
            tvg_id = tvg_id_edit.text().strip()
            
            if name or tvg_id:
                if self.blacklist_manager.add_channel(name, tvg_id):
                    self._load_blacklist_to_table()
    
    def _remove_from_blacklist(self):
        selected_rows = self.blacklist_table.selectionModel().selectedRows()
        if not selected_rows:
            return
        
        rows = sorted([row.row() for row in selected_rows], reverse=True)
        
        for row in rows:
            name_item = self.blacklist_table.item(row, 0)
            tvg_id_item = self.blacklist_table.item(row, 1)
            
            if name_item and tvg_id_item:
                name = name_item.text()
                tvg_id = tvg_id_item.text()
                self.blacklist_manager.remove_channel(name, tvg_id)
        
        self._load_blacklist_to_table()
    
    def _clear_blacklist(self):
        reply = QMessageBox.question(
            self, "Подтверждение",
            "Очистить весь чёрный список?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.blacklist_manager.clear()
            self._load_blacklist_to_table()
    
    def _import_blacklist(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Импорт чёрного списка", "",
            "JSON файлы (*.json);;Текстовые файлы (*.txt);;Все файлы (*.*)"
        )
        
        if not filepath:
            return
        
        try:
            if filepath.lower().endswith('.json'):
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if isinstance(data, list):
                    for item in data:
                        name = item.get('name', '')
                        tvg_id = item.get('tvg_id', '')
                        if name or tvg_id:
                            self.blacklist_manager.add_channel(name, tvg_id)
            else:
                with open(filepath, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        parts = line.split(',')
                        name = parts[0].strip() if len(parts) > 0 else ""
                        tvg_id = parts[1].strip() if len(parts) > 1 else ""
                        if name or tvg_id:
                            self.blacklist_manager.add_channel(name, tvg_id)
            
            self._load_blacklist_to_table()
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось импортировать чёрный список:\n{str(e)}")
    
    def _export_blacklist(self):
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Экспорт чёрного списка", "blacklist.json",
            "JSON файлы (*.json);;Текстовые файлы (*.txt);;Все файлы (*.*)"
        )
        
        if not filepath:
            return
        
        try:
            blacklist_items = self.blacklist_manager.get_all()
            
            if filepath.lower().endswith('.json'):
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(blacklist_items, f, ensure_ascii=False, indent=2)
            else:
                with open(filepath, 'w', encoding='utf-8') as f:
                    for item in blacklist_items:
                        name = item.get('name', '')
                        tvg_id = item.get('tvg_id', '')
                        f.write(f"{name},{tvg_id}\n")
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось экспортировать чёрный список:\n{str(e)}")
    
    def _manage_link_sources(self):
        dialog = LinkSourceManagerDialog(self.link_source_manager, self)
        dialog.sources_updated.connect(self._on_link_sources_updated)
        dialog.exec()
    
    def _on_link_sources_updated(self):
        pass
    
    def _manage_link_replacement_settings(self):
        dialog = LinkReplacementSettingsDialog(self.link_replacement_settings, self)
        dialog.settings_changed.connect(self._on_link_replacement_settings_changed)
        dialog.exec()
    
    def _on_link_replacement_settings_changed(self, settings: LinkReplacementSettings):
        self.link_replacement_settings = settings
        self._save_ip_filter_settings()
    
    def _copy_metadata_between_playlists(self):
        if len(self.tabs) < 2:
            QMessageBox.information(self, "Информация", "Нужно открыть как минимум 2 плейлиста")
            return
        
        dialog = CopyMetadataDialog(self, self)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_selected_data()
            if not data:
                return
            
            source_tab_widget = self.tab_widget.widget(data['source_tab_index'])
            target_tab_widget = self.tab_widget.widget(data['target_tab_index'])
            
            if source_tab_widget not in self.tabs or target_tab_widget not in self.tabs:
                return
            
            source_tab = self.tabs[source_tab_widget]
            target_tab = self.tabs[target_tab_widget]
            
            if not data['source_channels'] or not data['target_channels']:
                return
            
            if len(data['source_channels']) != len(data['target_channels']):
                QMessageBox.warning(self, "Предупреждение", "Количество исходных и целевых каналов должно совпадать")
                return
            
            target_tab._save_state("Копирование метаданных между плейлистами")
            
            for i, target_channel in enumerate(data['target_channels']):
                if i < len(data['source_channels']):
                    source_channel = data['source_channels'][i]
                    
                    match_func = None
                    if data['match_by_name']:
                        match_func = lambda s, t: s.match_by_name(t)
                    else:
                        match_func = lambda s, t: s.match_by_name_and_group(t)
                    
                    if match_func(source_channel, target_channel):
                        if data['copy_tvg_id'] and source_channel.tvg_id:
                            target_channel.tvg_id = source_channel.tvg_id
                        
                        if data['copy_logo'] and source_channel.tvg_logo:
                            target_channel.tvg_logo = source_channel.tvg_logo
                        
                        if data['copy_group'] and source_channel.group:
                            target_channel.group = source_channel.group
                        
                        if data['copy_user_agent'] and source_channel.user_agent:
                            target_channel.user_agent = source_channel.user_agent
                            target_channel.extra_headers['User-Agent'] = source_channel.user_agent
                        
                        if data['copy_headers']:
                            target_channel.extra_headers.update(source_channel.extra_headers)
                        
                        target_channel.update_extinf()
                        target_channel.update_extvlcopt_from_headers()
            
            target_tab._apply_filter()
            target_tab.modified = True
            target_tab._update_modified_status()
            
            self.status_bar.showMessage(f"Скопированы метаданные для {len(data['target_channels'])} каналов", 3000)
    
    def _zoom_in(self):
        if self.current_tab:
            font = self.current_tab.table.font()
            current_size = font.pointSize()
            if current_size < 20:
                font.setPointSize(current_size + 1)
                self.current_tab.table.setFont(font)
    
    def _zoom_out(self):
        if self.current_tab:
            font = self.current_tab.table.font()
            current_size = font.pointSize()
            if current_size > 8:
                font.setPointSize(current_size - 1)
                self.current_tab.table.setFont(font)
    
    def _reset_zoom(self):
        if self.current_tab:
            font = self.current_tab.table.font()
            font.setPointSize(10)
            self.current_tab.table.setFont(font)
    
    def _toggle_toolbar(self, visible: bool):
        self.toolbar.setVisible(visible)
    
    def _toggle_statusbar(self, visible: bool):
        self.status_bar.setVisible(visible)
    
    def _on_info_changed(self, info: str):
        self.info_label.setText(info)
    
    def closeEvent(self, event):
        modified_tabs = [tab for tab in self.tabs.values() if tab.modified]
        
        if modified_tabs:
            reply = QMessageBox.question(
                self, "Подтверждение",
                f"Есть {len(modified_tabs)} несохранённых файлов. Сохранить перед выходом?",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No |
                QMessageBox.StandardButton.Cancel
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                for tab in modified_tabs:
                    if not tab.filepath:
                        default_name = "playlist.m3u"
                        filepath, _ = QFileDialog.getSaveFileName(
                            self, "Сохранить как", default_name,
                            "M3U файлы (*.m3u *.m3u8);;Все файлы (*.*)"
                        )
                        
                        if filepath:
                            if not filepath.lower().endswith(('.m3u', '.m3u8')):
                                filepath += '.m3u'
                            tab.filepath = filepath
                        else:
                            event.ignore()
                            return
                    
                    if not tab.save_to_file():
                        event.ignore()
                        return
            
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        
        for tab in self.tabs.values():
            if hasattr(tab, 'checker') and tab.checker:
                try:
                    tab.checker.stop()
                    tab.checker.wait(500)
                except:
                    pass
        
        self._save_settings()
        
        for widget in QApplication.topLevelWidgets():
            if widget != self and isinstance(widget, QDialog):
                widget.close()
        
        event.accept()


def main():
    app = QApplication(sys.argv)
    
    app.setApplicationName("Ksenia M3U Editor")
    app.setOrganizationName("Ksenia")
    
    app.setQuitOnLastWindowClosed(True)
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
