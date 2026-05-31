#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Генератор m3u Ksenia - Автоматический инструмент для парсинга и проверки IPTV плейлистов
Copyright (C) 2026 IPTVru
"""

import sys
import os
import json
import requests
import concurrent.futures
import threading
import time
import re
import webbrowser
import pickle
import hashlib
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Set
from urllib.parse import urlparse
import logging
from difflib import SequenceMatcher
from collections import defaultdict

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMenuBar, QMenu, QStatusBar,
    QFileDialog, QMessageBox, QProgressBar, QSystemTrayIcon,
    QTabWidget, QGroupBox, QCheckBox, QSpinBox, QComboBox, QDialog, QDialogButtonBox
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QCoreApplication, QSize, QPoint
)
from PyQt6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QFont, QPen


# Инициализация логирования с проверкой наличия папки
def setup_logging():
    """Настройка логирования с созданием необходимых директорий"""
    try:
        # Определяем базовую директорию (для PyInstaller)
        if getattr(sys, 'frozen', False):
            # Запущено как собранное приложение
            base_dir = os.path.dirname(sys.executable)
        else:
            # Запущено как скрипт
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Создаем папку для логов
        logs_dir = os.path.join(base_dir, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        
        # Настройка логгера
        log_file = os.path.join(logs_dir, f"iptv_{datetime.now().strftime('%Y%m%d')}.log")
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        return True
    except Exception as e:
        # Если не удалось создать файл лога, используем только консоль
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler()]
        )
        print(f"Предупреждение: не удалось создать файл лога: {e}")
        return False

# Вызываем настройку логирования
setup_logging()
logger = logging.getLogger(__name__)


class CacheManager:
    """Менеджер кэширования результатов загрузки плейлистов"""
    
    def __init__(self, cache_dir="cache", cache_hours=24):
        # Определяем базовую директорию (для PyInstaller)
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        self.cache_dir = os.path.join(base_dir, cache_dir)
        self.cache_hours = cache_hours
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def _get_cache_key(self, url: str) -> str:
        """Генерация ключа кэша на основе URL"""
        return hashlib.md5(url.encode('utf-8')).hexdigest()
    
    def _get_cache_path(self, key: str) -> str:
        """Получение пути к файлу кэша"""
        return os.path.join(self.cache_dir, f"{key}.cache")
    
    def get(self, url: str) -> Optional[tuple]:
        """Получение данных из кэша"""
        key = self._get_cache_key(url)
        cache_path = self._get_cache_path(key)
        
        if os.path.exists(cache_path):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(cache_path))
                if datetime.now() - mtime < timedelta(hours=self.cache_hours):
                    with open(cache_path, 'rb') as f:
                        cached_data = pickle.load(f)
                        logger.debug(f"Cache hit for {url}")
                        return cached_data
            except Exception as e:
                logger.debug(f"Cache read error for {url}: {e}")
        
        return None
    
    def set(self, url: str, data: tuple):
        """Сохранение данных в кэш"""
        key = self._get_cache_key(url)
        cache_path = self._get_cache_path(key)
        
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump(data, f)
            logger.debug(f"Cached {url}")
        except Exception as e:
            logger.debug(f"Cache write error for {url}: {e}")
    
    def clear_old_cache(self, days: int = 7):
        """Очистка старого кэша"""
        try:
            for filename in os.listdir(self.cache_dir):
                if filename.endswith('.cache'):
                    filepath = os.path.join(self.cache_dir, filename)
                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                    if datetime.now() - mtime > timedelta(days=days):
                        os.remove(filepath)
                        logger.debug(f"Removed old cache: {filename}")
        except Exception as e:
            logger.debug(f"Cache cleanup error: {e}")


def create_app_icon():
    """Создание иконки приложения программно"""
    pixmap = QPixmap(256, 256)
    pixmap.fill(QColor(33, 150, 243))
    
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    
    painter.setPen(QPen(QColor(255, 255, 255), 12))
    for i in range(3):
        y = 70 + i * 58
        painter.drawLine(50, y, 206, y)
    
    painter.setBrush(QColor(255, 255, 255))
    painter.setPen(Qt.PenStyle.NoPen)
    
    play_triangle = [
        QPoint(128, 90),
        QPoint(128, 166),
        QPoint(186, 128)
    ]
    painter.drawPolygon(play_triangle)
    
    font = QFont("Arial", 48, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(180, 220, "K")
    
    painter.end()
    
    return QIcon(pixmap)


class SupportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Поддержка проекта")
        self.setMinimumWidth(500)
        self.setMinimumHeight(300)
        
        layout = QVBoxLayout(self)
        
        title_label = QLabel("☕ Поддержать проект")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        
        info_label = QLabel(
            "Если вам нравится этот инструмент и вы хотите поддержать его развитие,\n"
            "вы можете отправить добровольное пожертвование.\n\n"
            "Ваша поддержка поможет:\n"
            "• Добавлять новые функции\n"
            "• Поддерживать актуальность источников\n"
            "• Улучшать производительность\n\n"
            "Спасибо за вашу поддержку! 💝"
        )
        info_label.setWordWrap(True)
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)
        
        self.donate_btn = QPushButton("💰 Отправить перевод")
        self.donate_btn.setMinimumHeight(50)
        self.donate_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-weight: bold;
                font-size: 14px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
        """)
        self.donate_btn.clicked.connect(self._open_donate_link)
        layout.addWidget(self.donate_btn)
        
        wallet_label = QLabel(
            "Кошелек для переводов:\n"
            "<b>4100119518517127</b>\n\n"
            "YooMoney / ЮMoney"
        )
        wallet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wallet_label.setWordWrap(True)
        layout.addWidget(wallet_label)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def _open_donate_link(self):
        webbrowser.open("https://yoomoney.ru/to/4100119518517127")


class BlockedDomains:
    BLOCKED_DOMAINS = {
        "5.188.159.128", "5.188.221.43", "193.25.8.59", "31.148.48.15",
        "37.46.49.14", "77.245.98.18", "185.46.16.239", "158.101.222.193",
        "45.145.32.13", "195.26.83.96", "cef23ac9.rossteleccom.net",
        "mfe01.cliptv.az", "s1.tv-nano.com",
    }
    
    @staticmethod
    def is_blocked(url: str) -> bool:
        if not url:
            return False
        try:
            hostname = urlparse(url).hostname
            if not hostname:
                return False
            if hostname in BlockedDomains.BLOCKED_DOMAINS:
                return True
            for blocked in BlockedDomains.BLOCKED_DOMAINS:
                if hostname.startswith(blocked) or blocked in hostname:
                    return True
        except:
            pass
        return False


class Config:
    DEFAULT = {
        'max_workers': 15,
        'check_timeout': 3,
        'save_path': os.path.expanduser("~/Desktop"),
        'github_days_back': 7,
        'max_github_playlists': 15,
        'max_m3uguide_playlists': 10,
        'static_sources_enabled': True,
        'github_enabled': True,
        'm3uguide_enabled': True,
        'fast_channels_enabled': True,
        'use_smart_deduplication': True,
        'quality_priority': True,
        'save_history': True,
        'auto_update_days': 7,
        'use_proxy': False,
        'request_timeout': 15,
        'use_cache': True,
        'cache_hours': 24
        # Ограничение max_channels удалено
    }
    
    def __init__(self):
        self.config = self.DEFAULT.copy()
        self._load()
    
    def _load(self):
        # Определяем путь к конфигу (для PyInstaller)
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        config_path = os.path.join(base_dir, "config.json")
        
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    self.config.update(json.load(f))
            except:
                pass
    
    def save(self):
        # Определяем путь к конфигу (для PyInstaller)
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        config_path = os.path.join(base_dir, "config.json")
        
        with open(config_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get(self, key: str, default=None):
        return self.config.get(key, default)
    
    def set(self, key: str, value):
        self.config[key] = value


class ChannelData:
    __slots__ = ('name', 'group', 'tvg_id', 'tvg_logo', 'url', 'priority', 
                 'source_name', 'is_working', 'response_time', 'is_blocked',
                 'quality', 'has_epg', 'last_checked')
    
    def __init__(self):
        self.name = ""
        self.group = ""
        self.tvg_id = ""
        self.tvg_logo = ""
        self.url = ""
        self.priority = 5
        self.source_name = ""
        self.is_working = False
        self.response_time = 0.0
        self.is_blocked = False
        self.quality = 0
        self.has_epg = False
        self.last_checked = datetime.now()
    
    def to_m3u_line(self) -> str:
        """ИСПРАВЛЕНО: Теперь сохраняет только #EXTINF:-1 ,НАЗВАНИЕ без атрибутов"""
        # Только название канала, без атрибутов tvg-id, tvg-logo, group-title
        return f'#EXTINF:-1 ,{self.name}\n{self.url}'
    
    def get_quality_score(self) -> int:
        return self.quality * 10 + (5 if self.has_epg else 0) + max(0, 10 - int(self.response_time))


class LinkSource:
    __slots__ = ('name', 'path', 'source_type', 'enabled', 'priority', 'last_validated', 'channel_count')
    
    def __init__(self):
        self.name = ""
        self.path = ""
        self.source_type = "online"
        self.enabled = True
        self.priority = 5
        self.last_validated = None
        self.channel_count = 0


class FastChannelsSearcher(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list)
    
    def __init__(self):
        super().__init__()
        self._stop = False
        self._result = []
    
    def stop(self):
        self._stop = True
    
    def run(self):
        playlists = []
        
        fast_sources = [
            ("Pluto TV (US)", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_pluto.m3u8"),
            ("Samsung TV Plus (US)", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_samsung.m3u8"),
            ("Plex (US)", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_plex.m3u8"),
            ("Tubi (US)", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_tubi.m3u8"),
            ("Roku Channel (US)", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_roku.m3u8"),
            ("Xumo Play (US)", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us_xumo.m3u8"),
            ("Русские каналы", "https://raw.githubusercontent.com/iptv-org/iptv/master/playlists/ru.m3u"),
            ("CНГ каналы", "https://raw.githubusercontent.com/iptv-org/iptv/master/playlists/cis.m3u"),
        ]
        
        for name, url in fast_sources:
            if self._stop:
                break
            self.progress.emit(f"FAST: {name}")
            playlists.append({
                'name': f"FAST - {name}",
                'url': url,
                'source_type': 'fast',
                'priority': 3
            })
            time.sleep(0.3)
        
        self._result = playlists
        self.finished.emit(self._result)


class AdvancedGitHubSearcher(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list)
    
    def __init__(self, days_back: int):
        super().__init__()
        self.days_back = days_back
        self._stop = False
        self._result = []
    
    def stop(self):
        self._stop = True
    
    def run(self):
        playlists = []
        
        known_repos = [
            "iptv-org/iptv",
            "Free-TV/IPTV",
            "smolnp/IPTVru",
            "Spirt007/Tvru",
            "CrocoUser/zabava-project",
            "Azlux/iptv",
            "iptv-ru/iptv"
        ]
        
        for repo in known_repos:
            if self._stop:
                break
            self.progress.emit(f"GitHub: {repo}")
            try:
                response = requests.get(
                    f"https://api.github.com/repos/{repo}/contents/",
                    headers={'User-Agent': 'IPTV-Generator', 'Accept': 'application/vnd.github.v3+json'},
                    timeout=10
                )
                if response.status_code == 200:
                    contents = response.json()
                    if isinstance(contents, list):
                        for item in contents:
                            name = item.get('name', '')
                            if name.endswith('.m3u') or name.endswith('.m3u8'):
                                playlists.append({
                                    'name': f"{repo} - {name}",
                                    'url': item['download_url'],
                                    'stars': 0,
                                    'trusted': True,
                                    'channel_count': 0
                                })
            except Exception as e:
                logger.debug(f"GitHub error for {repo}: {e}")
            
            time.sleep(0.5)
        
        search_queries = [
            "iptv m3u russia",
            "russian iptv playlist",
            "iptv ru m3u"
        ]
        
        for query in search_queries:
            if self._stop:
                break
            self.progress.emit(f"GitHub поиск: {query}")
            
            try:
                response = requests.get(
                    f"https://api.github.com/search/repositories?q={requests.utils.quote(query)}&sort=stars&per_page=10",
                    headers={'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'IPTV-Generator'},
                    timeout=15
                )
                if response.status_code == 200:
                    for repo in response.json().get('items', []):
                        if self._stop:
                            break
                        
                        try:
                            contents = requests.get(
                                f"https://api.github.com/repos/{repo['full_name']}/contents/",
                                headers={'User-Agent': 'IPTV-Generator'},
                                timeout=10
                            ).json()
                            
                            if isinstance(contents, list):
                                for item in contents:
                                    name = item.get('name', '')
                                    if name.endswith('.m3u') or name.endswith('.m3u8'):
                                        if not any(p.get('url') == item['download_url'] for p in playlists):
                                            playlists.append({
                                                'name': f"{repo['full_name']} - {name}",
                                                'url': item['download_url'],
                                                'stars': repo.get('stargazers_count', 0),
                                                'trusted': False
                                            })
                        except:
                            pass
            except Exception as e:
                logger.debug(f"GitHub search error: {e}")
            
            time.sleep(0.5)
        
        playlists.sort(key=lambda x: (x.get('trusted', False), x.get('stars', 0)), reverse=True)
        
        self._result = playlists[:Config().get('max_github_playlists', 15)]
        self.finished.emit(self._result)


class PlaylistParser:
    URL_PATTERN = re.compile(r'^https?://(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|localhost|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?(?:/?|[/?]\S+)$', re.IGNORECASE)
    
    @staticmethod
    def normalize_name(name: str) -> str:
        if not name:
            return ""
        
        name = re.sub(r'\(\s*(?:720|1080|480|360|2160|4K|8K|UHD|FHD|HEVC|H264|H265)\s*[pPi]?\s*\)', '', name, flags=re.IGNORECASE)
        name = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002702-\U000027B0\U000024C2-\U0001F251]+', '', name, flags=re.UNICODE)
        name = re.sub(r'\s+', ' ', name).strip()
        name = re.sub(r'^[-–—\s]+|[-–—\s]+$', '', name)
        
        return name
    
    @staticmethod
    def is_valid_url(url: str) -> bool:
        """Улучшенная проверка URL"""
        if not url or len(url) < 10:
            return False
        
        # Проверка на локальные адреса
        if 'localhost' in url.lower() or '127.0.0.1' in url:
            return False
        
        # Проверка на внутренние IP
        internal_ips = ['10.', '172.16.', '172.17.', '172.18.', '172.19.', 
                       '172.20.', '172.21.', '172.22.', '172.23.', '172.24.',
                       '172.25.', '172.26.', '172.27.', '172.28.', '172.29.',
                       '172.30.', '172.31.', '192.168.']
        for ip in internal_ips:
            if ip in url:
                return False
        
        # Проверка схемы
        if not (url.startswith('http://') or url.startswith('https://')):
            return False
        
        # Проверка на недопустимые символы
        invalid_chars = ['\n', '\r', '\t', '\x00']
        for char in invalid_chars:
            if char in url:
                return False
        
        # Проверка длины
        if len(url) > 2000:
            return False
        
        # Проверка паттерном
        return bool(PlaylistParser.URL_PATTERN.match(url))
    
    @staticmethod
    def parse(content: str, source_name: str = "") -> List[ChannelData]:
        channels = []
        lines = content.split('\n')
        i, n = 0, len(lines)
        
        while i < n:
            line = lines[i].strip()
            if line.startswith('#EXTINF:'):
                channel = ChannelData()
                channel.source_name = source_name
                
                # Извлечение атрибутов (сохраняем для внутреннего использования, но не для вывода)
                tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
                if tvg_id_match:
                    channel.tvg_id = tvg_id_match.group(1)
                
                tvg_logo_match = re.search(r'tvg-logo="([^"]*)"', line)
                if tvg_logo_match:
                    channel.tvg_logo = tvg_logo_match.group(1)
                
                group_match = re.search(r'group-title="([^"]*)"', line)
                if group_match:
                    channel.group = group_match.group(1)
                
                if 'tvg-id' in line or 'epg' in line.lower():
                    channel.has_epg = True
                
                # Определение качества
                if '4K' in line or '2160' in line:
                    channel.quality = 4
                elif '1080' in line or 'FHD' in line:
                    channel.quality = 3
                elif '720' in line or 'HD' in line:
                    channel.quality = 2
                elif '480' in line or 'SD' in line:
                    channel.quality = 1
                
                if ',' in line:
                    clean_line = re.sub(r'tvg-id="[^"]*"\s*', '', line)
                    clean_line = re.sub(r'tvg-logo="[^"]*"\s*', '', clean_line)
                    clean_line = re.sub(r'group-title="[^"]*"\s*', '', clean_line)
                    clean_line = re.sub(r'tvg-name="[^"]*"\s*', '', clean_line)
                    clean_line = re.sub(r'tvg-country="[^"]*"\s*', '', clean_line)
                    clean_line = re.sub(r'tvg-language="[^"]*"\s*', '', clean_line)
                    
                    parts = clean_line.split(',')
                    if len(parts) > 1:
                        raw_name = ','.join(parts[1:]).strip()
                        channel.name = PlaylistParser.normalize_name(raw_name)
                
                # Поиск URL
                j = i + 1
                while j < n and j < i + 10:
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith('#'):
                        channel.url = next_line
                        break
                    j += 1
                
                if channel.url and PlaylistParser.is_valid_url(channel.url) and channel.name and len(channel.name) >= 2:
                    channels.append(channel)
                i = j
            else:
                i += 1
        return channels
    
    @staticmethod
    def parse_url(url: str, source_name: str = "", timeout: int = 30, use_cache: bool = True) -> List[ChannelData]:
        """Парсинг URL с поддержкой кэширования"""
        try:
            # Проверка кэша
            if use_cache:
                cache_manager = CacheManager(cache_hours=Config().get('cache_hours', 24))
                cached = cache_manager.get(url)
                if cached:
                    content, timestamp = cached
                    return PlaylistParser.parse(content, source_name)
            
            # Загрузка из сети
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            response = requests.get(url, timeout=timeout, verify=False, headers=headers)
            
            if response.status_code == 200:
                content = response.text
                
                # Сохранение в кэш
                if use_cache:
                    cache_manager.set(url, (content, datetime.now()))
                
                return PlaylistParser.parse(content, source_name)
            
            return []
        except Exception as e:
            logger.debug(f"Parse error for {url}: {e}")
            return []


class ChannelFilter:
    ALLOWED_NAMES: Set[str] = set()
    
    @staticmethod
    def load_allowed_channels() -> Set[str]:
        urls = [
            "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVmir.m3u8",
            "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVххх.m3u"
        ]
        
        allowed = set()
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        
        for url in urls:
            try:
                response = requests.get(url, timeout=30, verify=False, headers=headers)
                if response.status_code == 200:
                    for line in response.text.split('\n'):
                        if line.startswith('#EXTINF:') and ',' in line:
                            clean_line = re.sub(r'tvg-id="[^"]*"\s*', '', line)
                            clean_line = re.sub(r'tvg-logo="[^"]*"\s*', '', clean_line)
                            clean_line = re.sub(r'group-title="[^"]*"\s*', '', clean_line)
                            parts = clean_line.split(',')
                            if len(parts) > 1:
                                raw_name = ','.join(parts[1:]).strip()
                                name = PlaylistParser.normalize_name(raw_name)
                                if name and len(name) >= 2:
                                    allowed.add(name.lower())
            except:
                pass
        
        logger.info(f"Загружено разрешенных названий: {len(allowed)}")
        return allowed
    
    @staticmethod
    def is_allowed(name: str) -> bool:
        if not name or not ChannelFilter.ALLOWED_NAMES:
            return False
        
        name_clean = name.lower().strip()
        
        if name_clean in ChannelFilter.ALLOWED_NAMES:
            return True
        
        for allowed in ChannelFilter.ALLOWED_NAMES:
            if allowed in name_clean or name_clean in allowed:
                if len(allowed) > 3 and len(name_clean) > 3:
                    ratio = SequenceMatcher(None, name_clean, allowed).ratio()
                    if ratio > 0.85:
                        return True
        return False
    
    @staticmethod
    def should_keep(name: str) -> bool:
        if not name or len(name.strip()) < 2:
            return False
        name = name.strip()
        
        if name == '()' or name == '[]' or name == '{}':
            return False
        
        return ChannelFilter.is_allowed(name)


class SmartChannelDeduplicator:
    """ИСПРАВЛЕНО: Удаляет дубли URL, но сохраняет дубли названий"""
    def __init__(self, use_quality_priority: bool = True):
        self.use_quality_priority = use_quality_priority
        self.url_to_channel: Dict[str, ChannelData] = {}  # Для удаления дублей URL
    
    def add_channel(self, channel: ChannelData):
        """Добавляет канал, удаляя дубли по URL"""
        # Проверяем, есть ли уже такой URL
        if channel.url in self.url_to_channel:
            # URL уже существует - пропускаем дубль
            logger.debug(f"Deduplicator: skipping duplicate URL: {channel.url[:50]}...")
            return
        
        # Сохраняем канал по его URL
        self.url_to_channel[channel.url] = channel
    
    def deduplicate(self) -> List[ChannelData]:
        """Возвращает все каналы с уникальными URL (дубли названий сохраняются)"""
        result = list(self.url_to_channel.values())
        logger.info(f"Deduplicator: {len(result)} unique URLs from {len(self.url_to_channel)} total")
        return result


class StreamChecker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    
    def __init__(self, channels: List[ChannelData]):
        super().__init__()
        self.channels = channels
        self._stop = False
        self.results: List[ChannelData] = []
        self.config = Config()
    
    def stop(self):
        self._stop = True
    
    def _check_stream(self, channel: ChannelData) -> Optional[ChannelData]:
        if self._stop or BlockedDomains.is_blocked(channel.url):
            return None
        
        try:
            session = requests.Session()
            session.headers.update({'User-Agent': 'Mozilla/5.0', 'Connection': 'close'})
            
            start = time.time()
            resp = session.head(channel.url, timeout=self.config.get('check_timeout'), allow_redirects=True, verify=False)
            
            if resp.status_code in [200, 206, 301, 302]:
                channel.is_working = True
                channel.response_time = time.time() - start
                channel.last_checked = datetime.now()
                session.close()
                return channel
            
            resp = session.get(channel.url, timeout=self.config.get('check_timeout'), stream=True, verify=False)
            for _ in resp.iter_content(chunk_size=512):
                if resp.status_code in [200, 206]:
                    channel.is_working = True
                    channel.response_time = time.time() - start
                    channel.last_checked = datetime.now()
                    session.close()
                    return channel
                break
            session.close()
        except:
            pass
        return None
    
    def run(self):
        total = len(self.channels)
        checked = 0
        working = 0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.get('max_workers')) as executor:
            futures = {executor.submit(self._check_stream, ch): ch for ch in self.channels}
            
            for future in concurrent.futures.as_completed(futures):
                if self._stop:
                    break
                checked += 1
                result = future.result()
                if result:
                    working += 1
                    self.results.append(result)
                
                if checked % 10 == 0 or checked == total:
                    self.progress.emit(checked, total, f"Проверено: {checked}/{total} | Рабочих: {working}")
                    QCoreApplication.processEvents()
        
        self.results.sort(key=lambda x: x.name.lower())
        self.finished.emit(self.results)


class SourceManager:
    # Обновленный список статических источников
    STATIC_SOURCES = [
        ("Spirt007 Rus", "https://raw.githubusercontent.com/Spirt007/Tvru/refs/heads/Master/Rus.m3u", 5),
        ("iptv-org", "https://iptv-org.github.io/iptv/index.m3u", 5),
        ("SlyNet FreeBestTV", "https://slynet-iptv2025.do.am/FreeBestTV.m3u8", 5),
        ("Free-TV IPTV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", 5),
        ("artem-art998 iptv126", "https://raw.githubusercontent.com/artem-art998/IPTVru/refs/heads/main/iptv126.m3u", 5),
        ("empty180 pishma", "https://raw.githubusercontent.com/empty180/IPTVrus/refs/heads/main/pishma.m3u", 5),
        ("naggdd ru", "https://raw.githubusercontent.com/naggdd/iptv/refs/heads/main/ru.m3u", 5),
        ("Phoenix89S Iptv_Ru2026", "https://raw.githubusercontent.com/Phoenix89S/Iptv_Ru2026/refs/heads/main/test_channels.m3u", 5),
        ("empty180 ekb", "https://raw.githubusercontent.com/empty180/IPTVrus/refs/heads/main/ekb.m3u", 5),
        ("CrocoUser zabava", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 5),
        ("LoganetXIPTV All", "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXAll.m3u", 5),
        ("LoganetXIPTV Strawberry", "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXStrawberry.m3u", 5),
        ("LoganetXIPTV Central", "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetCentral.m3u", 5),
        ("CrocoUser zabava-ef", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-ef.m3u", 5),
        ("CrocoUser zabava-reg", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-reg.m3u", 5),
        ("iptv-org Russia only", "https://iptv-org.github.io/iptv/countries/ru.m3u", 5),
        ("iptv-org Language Russian", "https://iptv-org.github.io/iptv/index.language.m3u", 5),
        ("Основной плейлист", "https://smolnp.github.io/IPTVru/IPTVru.m3u", 1),
        ("Стабильный плейлист", "https://smolnp.github.io/IPTVru/IPTVstable.m3u8", 1),
        ("IPTV-org Russia", "https://iptv-org.github.io/iptv/countries/ru.m3u", 1),
        ("СНГ каналы", "https://iptv-org.github.io/iptv/playlists/cis.m3u", 2),
        ("Zabava Project", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 3),
    ]
    
    def __init__(self):
        self.sources: List[LinkSource] = []
        self._init_static()
    
    def _init_static(self):
        if Config().get('static_sources_enabled'):
            for name, url, priority in self.STATIC_SOURCES:
                src = LinkSource()
                src.name = name
                src.path = url
                src.priority = priority
                src.source_type = "static"
                self.sources.append(src)
    
    def add_github(self, playlists: List[Dict]):
        for pl in playlists[:Config().get('max_github_playlists', 15)]:
            if not any(s.path == pl['url'] for s in self.sources):
                src = LinkSource()
                src.name = f"GitHub: {pl['name'][:50]}"
                src.path = pl['url']
                src.priority = 4
                src.source_type = "github"
                src.channel_count = pl.get('channel_count', 0)
                self.sources.append(src)
    
    def add_fast_channels(self, playlists: List[Dict]):
        for pl in playlists:
            if not any(s.path == pl['url'] for s in self.sources):
                src = LinkSource()
                src.name = pl['name']
                src.path = pl['url']
                src.priority = pl.get('priority', 3)
                src.source_type = "fast"
                self.sources.append(src)
    
    def add_m3uguide(self, playlists: List[Dict]):
        for pl in playlists[:Config().get('max_m3uguide_playlists', 10)]:
            if not any(s.path == pl['url'] for s in self.sources):
                src = LinkSource()
                src.name = f"m3u.guide: {pl['name'][:50]}"
                src.path = pl['url']
                src.priority = 3
                src.source_type = "m3uguide"
                self.sources.append(src)
    
    def get_enabled(self) -> List[LinkSource]:
        return sorted([s for s in self.sources if s.enabled], key=lambda x: x.priority)


class PlaylistGenerator(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    
    def __init__(self, sources: List[LinkSource], use_smart_dedup: bool = True):
        super().__init__()
        self.sources = sources
        self._stop = False
        self._channels: List[ChannelData] = []
        self.use_smart_dedup = use_smart_dedup
        self.config = Config()
        self.cache_manager = CacheManager(cache_hours=self.config.get('cache_hours', 24))
    
    def stop(self):
        self._stop = True
    
    def run(self):
        total = len(self.sources)
        # ИСПРАВЛЕНО: Используем новый дедупликатор, который удаляет дубли URL
        deduplicator = SmartChannelDeduplicator(self.use_smart_dedup) if self.use_smart_dedup else None
        
        for i, source in enumerate(self.sources, 1):
            if self._stop:
                return
            
            self.progress.emit(i, total, f"Загрузка: {source.name}")
            
            channels = []
            if source.path:
                # Используем кэширование если включено
                use_cache = self.config.get('use_cache', True)
                channels = PlaylistParser.parse_url(source.path, source.name, timeout=20, use_cache=use_cache)
            
            for ch in channels:
                ch.priority = source.priority
                
                # Используем новый дедупликатор, который удаляет дубли URL
                if self.use_smart_dedup and deduplicator:
                    deduplicator.add_channel(ch)
                elif ch.url not in self._urls:  # Fallback
                    if not hasattr(self, '_urls'):
                        self._urls = set()
                    self._urls.add(ch.url)
                    self._channels.append(ch)
            
            QCoreApplication.processEvents()
        
        if self.use_smart_dedup and deduplicator:
            self._channels = deduplicator.deduplicate()
        
        # Ограничение по количеству каналов УДАЛЕНО
        
        # ИСПРАВЛЕНО: Сортировка в алфавитном порядке по названию
        self._channels.sort(key=lambda x: x.name.lower())
        logger.info(f"Загружено каналов: {len(self._channels)}")
        self.finished.emit(self._channels)


class M3UGuideSearcher(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list)
    
    def __init__(self):
        super().__init__()
        self._stop = False
        self._result = []
    
    def stop(self):
        self._stop = True
    
    def run(self):
        playlists = []
        sources = [
            ("Россия", "https://raw.githubusercontent.com/m3uguide/playlists/main/Russia.m3u"),
            ("Мир", "https://raw.githubusercontent.com/m3uguide/playlists/main/World.m3u"),
        ]
        
        for name, url in sources:
            if self._stop:
                break
            self.progress.emit(f"m3u.guide: {name}")
            playlists.append({'name': f"m3u.guide - {name}", 'url': url})
            time.sleep(0.5)
        
        self._result = playlists[:Config().get('max_m3uguide_playlists', 10)]
        self.finished.emit(self._result)


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Справка")
        self.setMinimumWidth(550)
        self.setMinimumHeight(450)
        
        layout = QVBoxLayout(self)
        
        tabs = QTabWidget()
        
        license_tab = QWidget()
        license_layout = QVBoxLayout(license_tab)
        
        license_text = QLabel(
            "GNU General Public License v3.0\n\n"
            "This program is free software: you can redistribute it and/or modify\n"
            "it under the terms of the GNU General Public License as published by\n"
            "the Free Software Foundation, either version 3 of the License, or\n"
            "(at your option) any later version.\n\n"
            "This program is distributed in the hope that it will be useful,\n"
            "but WITHOUT ANY WARRANTY; without even the implied warranty of\n"
            "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the\n"
            "GNU General Public License for more details.\n\n"
            "You should have received a copy of the GNU General Public License\n"
            "along with this program. If not, see <https://www.gnu.org/licenses/>."
        )
        license_text.setWordWrap(True)
        license_layout.addWidget(license_text)
        
        tools_tab = QWidget()
        tools_layout = QVBoxLayout(tools_tab)
        
        tools_info = QLabel(
            "Используемые инструменты и библиотеки:\n\n"
            "• Python 3.8+\n"
            "• PyQt6 - GUI framework\n"
            "• Requests - HTTP библиотека\n"
            "• urllib3 - HTTP клиент\n"
            "• threading / concurrent.futures - многопоточность\n"
            "• re - регулярные выражения\n"
            "• json - работа с конфигурацией\n"
            "• logging - система логирования\n\n"
            "Исходные коды и документация:\n"
            "https://github.com/IPTVru/ksenia-generator"
        )
        tools_info.setWordWrap(True)
        tools_layout.addWidget(tools_info)
        
        support_tab = QWidget()
        support_layout = QVBoxLayout(support_tab)
        
        support_label = QLabel(
            "Поддержать развитие проекта:\n\n"
            "Разработка и поддержка этого инструмента требует времени и усилий.\n"
            "Если вы хотите помочь проекту, вы можете:\n\n"
            "• Сообщать об ошибках и проблемах\n"
            "• Предлагать новые источники плейлистов\n"
            "• Делать добровольные пожертвования\n\n"
            "Способы поддержки:"
        )
        support_label.setWordWrap(True)
        support_layout.addWidget(support_label)
        
        donate_btn = QPushButton("☕ Поддержать проект (донат)")
        donate_btn.clicked.connect(lambda: SupportDialog(self).exec())
        support_layout.addWidget(donate_btn)
        
        support_layout.addStretch()
        
        tabs.addTab(license_tab, "Лицензия GPLv3")
        tabs.addTab(tools_tab, "Инструменты")
        tabs.addTab(support_tab, "Поддержка")
        
        layout.addWidget(tabs)
        
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)


class AutoProcessor(QThread):
    progress = pyqtSignal(str)
    search_progress = pyqtSignal(str)
    load_progress = pyqtSignal(int, int, str)
    check_progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._stop = False
        self.config = Config()
    
    def stop(self):
        self._stop = True
    
    def run(self):
        try:
            # Очистка старого кэша при запуске
            if self.config.get('use_cache', True):
                cache_manager = CacheManager(cache_hours=self.config.get('cache_hours', 24))
                cache_manager.clear_old_cache(days=7)
            
            self.progress.emit("📋 Загрузка эталонных каналов...")
            ChannelFilter.ALLOWED_NAMES = ChannelFilter.load_allowed_channels()
            
            manager = SourceManager()
            
            if self.config.get('github_enabled'):
                self.progress.emit("🌐 Поиск на GitHub...")
                github = AdvancedGitHubSearcher(self.config.get('github_days_back'))
                github.progress.connect(lambda msg: self.search_progress.emit(msg))
                github.start()
                github.wait()
                manager.add_github(github._result)
            
            if self.config.get('m3uguide_enabled'):
                self.progress.emit("🌍 Поиск в m3u.guide...")
                m3uguide = M3UGuideSearcher()
                m3uguide.progress.connect(lambda msg: self.search_progress.emit(msg))
                m3uguide.start()
                m3uguide.wait()
                manager.add_m3uguide(m3uguide._result)
            
            if self.config.get('fast_channels_enabled'):
                self.progress.emit("📺 Поиск FAST-каналов...")
                fast = FastChannelsSearcher()
                fast.progress.connect(lambda msg: self.search_progress.emit(msg))
                fast.start()
                fast.wait()
                manager.add_fast_channels(fast._result)
            
            sources = manager.get_enabled()
            if not sources:
                self.error.emit("Нет источников для загрузки")
                return
            
            self.progress.emit(f"📥 Загрузка из {len(sources)} источников...")
            use_smart_dedup = self.config.get('use_smart_deduplication', True)
            generator = PlaylistGenerator(sources, use_smart_dedup)
            generator.progress.connect(self.load_progress)
            generator.start()
            generator.wait()
            
            all_channels = generator._channels if hasattr(generator, '_channels') else []
            
            if not all_channels:
                self.error.emit("Не удалось загрузить каналы")
                return
            
            self.progress.emit(f"🔍 Фильтрация {len(all_channels)} каналов...")
            filtered = [ch for ch in all_channels if ChannelFilter.should_keep(ch.name)]
            logger.info(f"После фильтрации: {len(filtered)} каналов")
            
            if not filtered:
                self.finished.emit([], len(all_channels))
                return
            
            self.progress.emit(f"🔍 Проверка {len(filtered)} потоков...")
            checker = StreamChecker(filtered)
            checker.progress.connect(self.check_progress)
            checker.start()
            checker.wait()
            
            working = checker.results if hasattr(checker, 'results') else []
            
            self.progress.emit(f"✅ Готово! Рабочих каналов: {len(working)}")
            self.finished.emit(working, len(all_channels))
            
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Генератор m3u Ksenia v0.4")
        self.setMinimumSize(400, 260)
        
        self.setWindowIcon(create_app_icon())
        
        self.config = Config()
        self.processor: Optional[AutoProcessor] = None
        self.working_channels: List[ChannelData] = []
        
        self._setup_ui()
        self._setup_menu()
        self._setup_tray()
    
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(15)
        
        self.status_label = QLabel("⏳ Готов к работе")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 10px; background-color: #f0f0f0; border-radius: 5px;")
        layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        self.start_btn = QPushButton("▶ Старт")
        self.start_btn.setMinimumHeight(50)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                font-size: 16px;
                border-radius: 10px;
            }
            QPushButton:hover { background-color: #45a049; }
        """)
        self.start_btn.clicked.connect(self._start)
        layout.addWidget(self.start_btn)
        
        self.save_btn = QPushButton("💾 Сохранить плейлист")
        self.save_btn.setMinimumHeight(50)
        self.save_btn.setEnabled(False)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                font-weight: bold;
                font-size: 16px;
                border-radius: 10px;
            }
            QPushButton:hover { background-color: #1976D2; }
            QPushButton:disabled { background-color: #cccccc; color: #888; }
        """)
        self.save_btn.clicked.connect(self._save)
        layout.addWidget(self.save_btn)
        
        layout.addStretch()
        
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Готов")
        
        self.config.save()
    
    def _setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("📁 Файл")
        
        save_action = QAction("💾 Сохранить плейлист", self)
        save_action.triggered.connect(self._save)
        file_menu.addAction(save_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("🚪 Выход", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        help_menu = menubar.addMenu("❓ Справка")
        
        license_action = QAction("⚖ Лицензия GPLv3", self)
        license_action.triggered.connect(self._show_license)
        help_menu.addAction(license_action)
        
        tools_action = QAction("🔧 Используемые инструменты", self)
        tools_action.triggered.connect(self._show_tools)
        help_menu.addAction(tools_action)
        
        help_menu.addSeparator()
        
        support_action = QAction("☕ Поддержка проекта", self)
        support_action.triggered.connect(lambda: SupportDialog(self).exec())
        help_menu.addAction(support_action)
        
        help_menu.addSeparator()
        
        about_qt_action = QAction("О Qt", self)
        about_qt_action.triggered.connect(lambda: QMessageBox.aboutQt(self))
        help_menu.addAction(about_qt_action)
    
    def _show_license(self):
        license_text = """GNU General Public License v3.0

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>."""
        
        QMessageBox.about(self, "Лицензия GPLv3", license_text)
    
    def _show_tools(self):
        tools_text = """Используемые инструменты и библиотеки:

• Python 3.8+
• PyQt6 - GUI framework
• Requests - HTTP библиотека
• urllib3 - HTTP клиент
• threading / concurrent.futures - многопоточность
• re - регулярные выражения
• json - работа с конфигурацией
• logging - система логирования

Исходные коды и документация:
https://github.com/IPTVru/ksenia-generator"""
        
        QMessageBox.about(self, "Инструменты", tools_text)
    
    def _setup_tray(self):
        icon = create_app_icon()
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setVisible(True)
        
        tray_menu = QMenu()
        show_action = tray_menu.addAction("Показать")
        show_action.triggered.connect(self.show)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Выйти")
        quit_action.triggered.connect(self.close)
        self.tray_icon.setContextMenu(tray_menu)
    
    def _start(self):
        if self.processor and self.processor.isRunning():
            return
        
        self.config.save()
        
        self.start_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("⏳ Выполняется обработка...")
        
        self.processor = AutoProcessor()
        self.processor.progress.connect(self._on_progress)
        self.processor.search_progress.connect(self._on_search_progress)
        self.processor.load_progress.connect(self._on_load_progress)
        self.processor.check_progress.connect(self._on_check_progress)
        self.processor.finished.connect(self._on_finished)
        self.processor.error.connect(self._on_error)
        
        self.processor.start()
    
    def _on_progress(self, msg: str):
        self.status_label.setText(msg)
        self.status_bar.showMessage(msg[:50])
        QCoreApplication.processEvents()
    
    def _on_search_progress(self, msg: str):
        self.status_label.setText(f"🔍 {msg}")
        QCoreApplication.processEvents()
    
    def _on_load_progress(self, current: int, total: int, msg: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"Загрузка: {current}/{total}")
        self.status_label.setText(msg)
        QCoreApplication.processEvents()
    
    def _on_check_progress(self, current: int, total: int, msg: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"Проверка: {current}/{total}")
        self.status_label.setText(msg)
        QCoreApplication.processEvents()
    
    def _on_finished(self, channels: List[ChannelData], total: int):
        self.working_channels = channels
        self.progress_bar.setVisible(False)
        self.start_btn.setEnabled(True)
        self.save_btn.setEnabled(len(channels) > 0)
        
        working = len(channels)
        self.status_label.setText(f"✅ Готово! Рабочих каналов: {working} из {total}")
        
        if working > 0:
            QMessageBox.information(self, "Завершено", f"Найдено рабочих каналов: {working}\nНажмите 'Сохранить плейлист'")
        else:
            QMessageBox.warning(self, "Завершено", "Не найдено рабочих каналов")
    
    def _on_error(self, error: str):
        self.progress_bar.setVisible(False)
        self.start_btn.setEnabled(True)
        self.status_label.setText("❌ Ошибка")
        QMessageBox.critical(self, "Ошибка", error)
    
    def _save(self):
        if not self.working_channels:
            QMessageBox.warning(self, "Ошибка", "Нет каналов для сохранения")
            return
        
        # Определяем путь для сохранения (для PyInstaller)
        if getattr(sys, 'frozen', False):
            default_path = os.path.dirname(sys.executable)
        else:
            default_path = self.config.get('save_path')
        
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить плейлист", 
                                               os.path.join(default_path, "IPTVdonor.m3u"),
                                               "M3U файлы (*.m3u)")
        if not path:
            return
        
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('#EXTM3U\n')
                f.write(f'# Создано: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
                f.write(f'# Каналов: {len(self.working_channels)}\n')
                f.write(f'# Версия: 0.4\n\n')
                for ch in self.working_channels:
                    f.write(ch.to_m3u_line() + '\n')
            
            self.config.set('save_path', os.path.dirname(path))
            self.config.save()
            self.status_bar.showMessage(f"Сохранено: {os.path.basename(path)}")
            QMessageBox.information(self, "Успех", f"Плейлист сохранён!\n{path}\nКаналов: {len(self.working_channels)}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))
    
    def closeEvent(self, event):
        if self.processor and self.processor.isRunning():
            self.processor.stop()
            self.processor.wait(2000)
        self.config.save()
        self.tray_icon.hide()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Генератор m3u Ksenia v0.4")
    app.setWindowIcon(create_app_icon())
    app.setStyle('Fusion')
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
