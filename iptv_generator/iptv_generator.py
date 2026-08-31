#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
import socket
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Set, Tuple
from urllib.parse import urlparse, urljoin
import logging
from difflib import SequenceMatcher
from collections import defaultdict

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMenuBar, QMenu, QStatusBar,
    QFileDialog, QMessageBox, QProgressBar, QSystemTrayIcon,
    QTabWidget, QDialog, QDialogButtonBox, QCheckBox, QSpinBox,
    QGroupBox, QFormLayout, QTextEdit, QSplitter, QListWidget,
    QListWidgetItem, QComboBox, QLineEdit
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QCoreApplication, QPoint, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QFont, QPen


def setup_logging():
    try:
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        logs_dir = os.path.join(base_dir, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        
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
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler()]
        )
        print(f"Warning: Failed to create log file: {e}")
        return False

setup_logging()
logger = logging.getLogger(__name__)


class NetworkValidator:
    BLOCKED_DOMAINS = {
        "5.188.159.128", "5.188.221.43", "193.25.8.59", "31.148.48.15",
        "37.46.49.14", "77.245.98.18", "185.46.16.239", "158.101.222.193",
        "45.145.32.13", "195.26.83.96", "93.84.115.174", "151.80.18.177",
        "194.158.222.36", "217.11.177.56", "217.11.177.55", "178.124.153.123",
        "176.126.166.43", "176.118.197.101", "31.131.141.195", "31.210.208.171",
        "178.134.1.158", "5.188.159.128:8070", "193.25.8.59:8000",
        "37.46.49.14:18010", "77.245.98.18:8000", "185.46.16.239:8000",
        "176.126.166.43:1935", "158.101.222.193:88", "45.145.32.13:20440",
        "195.26.83.96:7007", "195.26.83.96:7013", "195.26.83.96:7006",
        "93.84.115.174:10181", "151.80.18.177:86", "178.134.1.158:8081",
        "31.131.141.195:8000", "194.158.222.36:6102", "31.210.208.171:8080",
        "rossteleccom.net", "cliptv.az", "tv-nano.com", "cdnvideo.ru",
        "sibinformburo.cdnvideo.ru", "smotrim.ru", "1tvcrimea.ru",
        "vintera.tv", "cinerama.uz", "teletarget.ru", "skygo.mn",
        "catcast.tv", "sofast.tv", "thestream.cyou", "slavmir.tv",
        "abinet.com", "tricolor.tv", "ott.tricolor.tv"
    }
    
    PRIVATE_IP_PATTERNS = [
        r'^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$',
        r'^172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}$',
        r'^192\.168\.\d{1,3}\.\d{1,3}$',
        r'^127\.\d{1,3}\.\d{1,3}$',
        r'^0\.\d{1,3}\.\d{1,3}\.\d{1,3}$',
        r'^169\.254\.\d{1,3}\.\d{1,3}$'
    ]
    
    @classmethod
    def test_network_connectivity(cls, url: str, timeout: int = 3) -> Tuple[bool, float, str]:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if hostname:
                try:
                    socket.gethostbyname(hostname)
                except socket.gaierror:
                    return False, 0, "DNS резолвинг не удался"
            
            start_time = time.time()
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Connection': 'close',
                'Accept': '*/*'
            })
            try:
                response = session.head(url, timeout=timeout, allow_redirects=True, verify=False)
                response_time = time.time() - start_time
                if response.status_code in [200, 206, 301, 302, 304, 307, 308]:
                    return True, response_time, f"HTTP {response.status_code}"
                else:
                    return False, response_time, f"HTTP {response.status_code}"
            except requests.Timeout:
                return False, timeout, "Превышен таймаут"
            except requests.ConnectionError:
                return False, 0, "Ошибка соединения"
            except Exception as e:
                return False, 0, f"Ошибка: {str(e)}"
            finally:
                try:
                    session.close()
                except:
                    pass
        except Exception as e:
            return False, 0, f"Критическая ошибка: {str(e)}"


class CacheManager:
    def __init__(self, cache_dir="cache", cache_hours=24):
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(base_dir, cache_dir)
        self.cache_hours = cache_hours
        os.makedirs(self.cache_dir, exist_ok=True)
        self.working_links_cache: Dict[str, str] = {}
        self._load_working_links()
    
    def _load_working_links(self):
        cache_file = os.path.join(self.cache_dir, "working_links.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    self.working_links_cache = json.load(f)
            except Exception as e:
                logger.debug(f"Failed to load working links: {e}")
    
    def save_working_link(self, name: str, url: str):
        if name and url:
            self.working_links_cache[name.lower()] = url
            cache_file = os.path.join(self.cache_dir, "working_links.json")
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self.working_links_cache, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.debug(f"Failed to save working links: {e}")
    
    def get_working_link(self, name: str) -> Optional[str]:
        if not name:
            return None
        return self.working_links_cache.get(name.lower())
    
    def get_all_working_links(self) -> Dict[str, str]:
        return self.working_links_cache.copy()


def create_app_icon():
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
    play_triangle = [QPoint(128, 90), QPoint(128, 166), QPoint(186, 128)]
    painter.drawPolygon(play_triangle)
    font = QFont("Arial", 48, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(180, 220, "K")
    painter.end()
    return QIcon(pixmap)


class Config:
    DEFAULT = {
        'max_workers': 20,
        'check_timeout': 2,
        'save_path': os.path.expanduser("~/Desktop"),
        'static_sources_enabled': True,
        'use_smart_deduplication': True,
        'quality_priority': True,
        'use_cache': True,
        'cache_hours': 24,
        'connection_timeout': 3,
        'max_retries': 2,
        'retry_delay': 1,
        'max_channels_to_check': 500,
        'update_broken_only': True,
        'fallback_to_cached': True,
        'keep_duplicates': True
    }
    
    def __init__(self):
        self.config = self.DEFAULT.copy()
        self._load()
    
    def _load(self):
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
                 'quality', 'has_epg', 'last_checked', 'validation_error',
                 'alternative_urls', 'best_source')
    
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
        self.validation_error = ""
        self.alternative_urls: List[str] = []
        self.best_source = ""
    
    def to_m3u_line(self) -> str:
        """Сохранение только #EXTINF:-1 ,НАЗВАНИЕ без атрибутов"""
        return f'#EXTINF:-1 ,{self.name}\n{self.url}'
    
    def get_quality_score(self) -> int:
        return self.quality * 10 + (5 if self.has_epg else 0) + max(0, 10 - int(self.response_time))
    
    def add_alternative(self, url: str, source_name: str):
        if url and url not in self.alternative_urls:
            self.alternative_urls.append(url)
            if not self.best_source:
                self.best_source = source_name


class LinkSource:
    __slots__ = ('name', 'path', 'source_type', 'enabled', 'priority', 'last_validated', 'channel_count', 'last_error')
    
    def __init__(self):
        self.name = ""
        self.path = ""
        self.source_type = "online"
        self.enabled = True
        self.priority = 5
        self.last_validated = None
        self.channel_count = 0
        self.last_error = ""


class PlaylistParser:
    def __init__(self):
        self.config = Config()
    
    @staticmethod
    def normalize_name(name: str) -> str:
        if not name:
            return ""
        name = re.sub(r'\(\s*(?:720|1080|480|360|2160|4K|8K|UHD|FHD|HEVC|H264|H265)\s*[pPi]?\s*\)', '', name, flags=re.IGNORECASE)
        name = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002702-\U000027B0\U000024C2-\U0001F251]+', '', name, flags=re.UNICODE)
        name = re.sub(r'\s+', ' ', name).strip()
        name = re.sub(r'^[-–—\s]+|[-–—\s]+$', '', name)
        return name
    
    def parse(self, content: str, source_name: str = "") -> List[ChannelData]:
        channels = []
        lines = content.split('\n')
        i, n = 0, len(lines)
        
        while i < n:
            line = lines[i].strip()
            if line.startswith('#EXTINF:'):
                channel = ChannelData()
                channel.source_name = source_name
                
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
                
                j = i + 1
                while j < n and j < i + 10:
                    next_line = lines[j].strip()
                    if next_line and not next_line.startswith('#'):
                        channel.url = next_line
                        break
                    j += 1
                
                if channel.url and channel.name and len(channel.name) >= 2:
                    channels.append(channel)
                i = j
            else:
                i += 1
        
        return channels
    
    def parse_url(self, url: str, source_name: str = "", timeout: int = 15, use_cache: bool = True) -> List[ChannelData]:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            session = requests.Session()
            session.headers.update(headers)
            
            response = session.get(url, timeout=timeout, verify=False)
            
            if response.status_code == 200:
                content = response.text
                return self.parse(content, source_name)
            
            return []
            
        except Exception as e:
            logger.error(f"Parse error for {url}: {e}")
            return []


class ChannelFilter:
    ALLOWED_NAMES: Set[str] = set()
    CHANNEL_ALIASES: Dict[str, List[str]] = {}
    
    @staticmethod
    def load_allowed_channels() -> Set[str]:
        urls = [
            "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVmir.m3u8",
            "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVххх.m3u"
        ]
        
        allowed = set()
        aliases = defaultdict(list)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        
        for url in urls:
            try:
                logger.info(f"Loading reference playlist: {url}")
                response = requests.get(url, timeout=10, verify=False, headers=headers)
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
                                    clean_alias = re.sub(r'\s*(?:HD|FHD|4K|UHD|1080|720|480)\s*', '', name.lower()).strip()
                                    if clean_alias and clean_alias != name.lower():
                                        aliases[clean_alias].append(name.lower())
            except Exception as e:
                logger.error(f"Error loading reference playlist {url}: {e}")
        
        for alias, names in aliases.items():
            ChannelFilter.CHANNEL_ALIASES[alias] = names
        
        logger.info(f"Loaded reference channels: {len(allowed)}")
        return allowed
    
    @staticmethod
    def is_allowed(name: str) -> bool:
        if not name or not ChannelFilter.ALLOWED_NAMES:
            return False
        
        name_clean = name.lower().strip()
        
        if name_clean in ChannelFilter.ALLOWED_NAMES:
            return True
        
        for alias, names in ChannelFilter.CHANNEL_ALIASES.items():
            if alias in name_clean:
                for ref_name in names:
                    if ref_name in name_clean or name_clean in ref_name:
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


class PlaylistUpdater(QThread):
    progress = pyqtSignal(int, int, str)
    channel_updated = pyqtSignal(str, str)
    finished = pyqtSignal(int)
    error = pyqtSignal(str)
    
    def __init__(self, working_links: Dict[str, str]):
        super().__init__()
        self.working_links = working_links
        self.target_playlists: List[str] = []
        self._stop = False
        self.updated_count = 0
        self.parser = PlaylistParser()
    
    def set_playlists(self, playlists: List[str]):
        self.target_playlists = [p for p in playlists if p and os.path.exists(p)]
    
    def stop(self):
        self._stop = True
    
    def run(self):
        try:
            if not self.target_playlists:
                self.error.emit("Нет файлов для обновления")
                return
            
            if not self.working_links:
                self.error.emit("Нет рабочих ссылок для обновления")
                return
            
            logger.info(f"Начинаем обновление {len(self.target_playlists)} плейлистов")
            logger.info(f"Доступно рабочих ссылок: {len(self.working_links)}")
            
            total = len(self.target_playlists)
            
            for i, target_path in enumerate(self.target_playlists, 1):
                if self._stop:
                    break
                
                self.progress.emit(i, total, f"Обработка: {os.path.basename(target_path)}")
                logger.info(f"Обработка: {target_path}")
                
                try:
                    updated = self._update_playlist_file(target_path)
                    self.updated_count += updated
                    logger.info(f"Обновлено {updated} каналов в {os.path.basename(target_path)}")
                    
                except Exception as e:
                    logger.error(f"Ошибка обработки {target_path}: {e}")
                    self.error.emit(f"Ошибка: {os.path.basename(target_path)} - {str(e)}")
            
            self.finished.emit(self.updated_count)
            
        except Exception as e:
            logger.error(f"Error: {e}")
            self.error.emit(str(e))
    
    def _update_playlist_file(self, path: str) -> int:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            channels = self.parser.parse(content, os.path.basename(path))
            
            if not channels:
                logger.warning(f"Нет каналов в {path}")
                return 0
            
            updated = 0
            updated_channels = []
            
            for ch in channels:
                if self._stop:
                    break
                
                name_key = ch.name.lower()
                new_url = None
                
                if name_key in self.working_links:
                    new_url = self.working_links[name_key]
                
                if not new_url and name_key in ChannelFilter.CHANNEL_ALIASES:
                    for alias in ChannelFilter.CHANNEL_ALIASES[name_key]:
                        if alias in self.working_links:
                            new_url = self.working_links[alias]
                            break
                
                if not new_url:
                    for working_name, working_url in self.working_links.items():
                        if working_name in name_key or name_key in working_name:
                            if len(working_name) > 3 and len(name_key) > 3:
                                ratio = SequenceMatcher(None, name_key, working_name).ratio()
                                if ratio > 0.85:
                                    new_url = working_url
                                    break
                
                if new_url and new_url != ch.url:
                    ch.url = new_url
                    ch.is_working = True
                    ch.source_name = "обновлено из донора"
                    updated += 1
                    updated_channels.append(ch)
                    self.channel_updated.emit(ch.name, ch.url)
                    logger.info(f"Обновлен: {ch.name}")
            
            if updated > 0:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('#EXTM3U\n')
                    f.write(f'# Обновлено из донора: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
                    f.write(f'# Обновлено каналов: {updated}\n\n')
                    
                    lines = content.split('\n')
                    i = 0
                    while i < len(lines):
                        line = lines[i].strip()
                        if line.startswith('#EXTINF:'):
                            ch_name = ""
                            if ',' in line:
                                parts = line.split(',')
                                if len(parts) > 1:
                                    ch_name = PlaylistParser.normalize_name(','.join(parts[1:]).strip())
                            
                            url_line = ""
                            j = i + 1
                            while j < len(lines) and j < i + 10:
                                next_line = lines[j].strip()
                                if next_line and not next_line.startswith('#'):
                                    url_line = next_line
                                    break
                                j += 1
                            
                            if ch_name:
                                for updated_ch in updated_channels:
                                    if updated_ch.name.lower() == ch_name.lower():
                                        if url_line:
                                            f.write(line + '\n')
                                            f.write(updated_ch.url + '\n')
                                        else:
                                            f.write(line + '\n')
                                            f.write(updated_ch.url + '\n')
                                        break
                                else:
                                    f.write(line + '\n')
                                    if url_line:
                                        f.write(url_line + '\n')
                            else:
                                f.write(line + '\n')
                                if url_line:
                                    f.write(url_line + '\n')
                            
                            i = j
                        else:
                            f.write(line + '\n')
                            i += 1
                
                logger.info(f"Сохранен обновленный плейлист: {path}")
            
            return updated
            
        except Exception as e:
            logger.error(f"Ошибка обновления {path}: {e}")
            return 0


class SmartChannelDeduplicator:
    def __init__(self, use_quality_priority: bool = True, keep_duplicates: bool = False):
        self.use_quality_priority = use_quality_priority
        self.keep_duplicates = keep_duplicates
        self.name_to_channels: Dict[str, List[ChannelData]] = defaultdict(list)
        self.cache_manager = CacheManager()
        self.all_channels: List[ChannelData] = []
        self.seen_urls: Set[str] = set()
    
    def add_channel(self, channel: ChannelData):
        if channel.url in self.seen_urls:
            logger.debug(f"Skipping duplicate URL: {channel.url[:50]}...")
            return
        
        self.seen_urls.add(channel.url)
        name_key = channel.name.lower()
        self.name_to_channels[name_key].append(channel)
        self.all_channels.append(channel)
    
    def deduplicate(self) -> List[ChannelData]:
        if self.keep_duplicates:
            result = []
            for name, channels in self.name_to_channels.items():
                channels.sort(key=lambda x: (x.priority, x.get_quality_score()), reverse=True)
                result.extend(channels)
            result.sort(key=lambda x: x.name.lower())
            logger.info(f"Deduplicator: keeping {len(result)} channels with unique URLs")
            return result
        else:
            result = []
            for name, channels in self.name_to_channels.items():
                if channels:
                    channels.sort(key=lambda x: (x.priority, x.get_quality_score()), reverse=True)
                    best = channels[0]
                    
                    for ch in channels[1:]:
                        if ch.url and ch.url != best.url:
                            best.add_alternative(ch.url, ch.source_name)
                    
                    if best.is_working and best.url:
                        self.cache_manager.save_working_link(best.name, best.url)
                    
                    result.append(best)
            
            result.sort(key=lambda x: x.name.lower())
            logger.info(f"Deduplicator: keeping {len(result)} channels (best only)")
            return result
    
    def get_all_channels(self) -> List[ChannelData]:
        self.all_channels.sort(key=lambda x: x.name.lower())
        return self.all_channels


class StreamChecker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    channel_checked = pyqtSignal(str, bool)
    
    def __init__(self, channels: List[ChannelData]):
        super().__init__()
        self.channels = channels
        self._stop = False
        self.results: List[ChannelData] = []
        self.config = Config()
        self.checked_count = 0
        self.working_count = 0
        self.cache_manager = CacheManager()
    
    def stop(self):
        self._stop = True
    
    def _check_stream(self, channel: ChannelData) -> Optional[ChannelData]:
        if self._stop:
            return None
        
        cached_url = self.cache_manager.get_working_link(channel.name)
        if cached_url and cached_url != channel.url:
            is_available, response_time, _ = NetworkValidator.test_network_connectivity(
                cached_url, timeout=self.config.get('check_timeout', 2)
            )
            if is_available:
                channel.url = cached_url
                channel.is_working = True
                channel.response_time = response_time
                channel.last_checked = datetime.now()
                channel.source_name = "cached"
                return channel
        
        is_available, response_time, _ = NetworkValidator.test_network_connectivity(
            channel.url, timeout=self.config.get('check_timeout', 2)
        )
        
        if is_available:
            channel.is_working = True
            channel.response_time = response_time
            channel.last_checked = datetime.now()
            self.cache_manager.save_working_link(channel.name, channel.url)
            return channel
        
        if not is_available and channel.alternative_urls:
            for alt_url in channel.alternative_urls:
                if self._stop:
                    break
                is_alt_available, alt_time, _ = NetworkValidator.test_network_connectivity(
                    alt_url, timeout=self.config.get('check_timeout', 2)
                )
                if is_alt_available:
                    channel.url = alt_url
                    channel.is_working = True
                    channel.response_time = alt_time
                    channel.last_checked = datetime.now()
                    self.cache_manager.save_working_link(channel.name, alt_url)
                    return channel
        
        return None
    
    def run(self):
        total = len(self.channels)
        
        max_check = self.config.get('max_channels_to_check', 500)
        if total > max_check:
            self.channels = self.channels[:max_check]
            total = len(self.channels)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.get('max_workers', 20)) as executor:
            futures = {executor.submit(self._check_stream, ch): ch for ch in self.channels}
            
            for future in concurrent.futures.as_completed(futures):
                if self._stop:
                    break
                self.checked_count += 1
                result = future.result()
                if result:
                    self.working_count += 1
                    self.results.append(result)
                    self.channel_checked.emit(result.name, True)
                
                if self.checked_count % 20 == 0 or self.checked_count == total:
                    self.progress.emit(
                        self.checked_count, total, 
                        f"Проверено: {self.checked_count}/{total} | Рабочих: {self.working_count}"
                    )
                    QCoreApplication.processEvents()
        
        self.results.sort(key=lambda x: x.name.lower())
        self.finished.emit(self.results)


class SourceManager:
    STATIC_SOURCES = [
        ("Основной плейлист", "https://smolnp.github.io/IPTVru/IPTVru.m3u", 1),
        ("Стабильный плейлист", "https://smolnp.github.io/IPTVru/IPTVstable.m3u8", 1),
        ("IPTV-org Russia", "https://iptv-org.github.io/iptv/countries/ru.m3u", 1),
        ("Zabava Project", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 2),
        ("Spirt007 Rus", "https://raw.githubusercontent.com/Spirt007/Tvru/refs/heads/Master/Rus.m3u", 3),
        ("iptv-org", "https://iptv-org.github.io/iptv/index.m3u", 3),
        ("SlyNet FreeBestTV", "https://slynet-iptv2025.do.am/FreeBestTV.m3u8", 3),
        ("Free-TV IPTV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", 3),
        ("artem-art998 iptv126", "https://raw.githubusercontent.com/artem-art998/IPTVru/refs/heads/main/iptv126.m3u", 4),
        ("empty180 pishma", "https://raw.githubusercontent.com/empty180/IPTVrus/refs/heads/main/pishma.m3u", 4),
        ("naggdd ru", "https://raw.githubusercontent.com/naggdd/iptv/refs/heads/main/ru.m3u", 4),
        ("CrocoUser zabava", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 4),
        ("LoganetXIPTV All", "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXAll.m3u", 4),
        ("LoganetXIPTV Strawberry", "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXStrawberry.m3u", 4),
        ("CrocoUser zabava-ef", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-ef.m3u", 4),
        ("CrocoUser zabava-reg", "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-reg.m3u", 4),
        ("iptv-org Russia only", "https://iptv-org.github.io/iptv/countries/ru.m3u", 4),
        ("iptv-org Language Russian", "https://iptv-org.github.io/iptv/index.language.m3u", 4),
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
    
    def get_enabled(self) -> List[LinkSource]:
        return sorted([s for s in self.sources if s.enabled], key=lambda x: x.priority)


class PlaylistGenerator(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    
    def __init__(self, sources: List[LinkSource], use_smart_dedup: bool = True, keep_duplicates: bool = False):
        super().__init__()
        self.sources = sources
        self._stop = False
        self._channels: List[ChannelData] = []
        self.use_smart_dedup = use_smart_dedup
        self.keep_duplicates = keep_duplicates
        self.config = Config()
        self.parser = PlaylistParser()
        self.name_to_channels: Dict[str, List[ChannelData]] = defaultdict(list)
        self.seen_urls: Set[str] = set()
    
    def stop(self):
        self._stop = True
    
    def run(self):
        total = len(self.sources)
        
        for i, source in enumerate(self.sources, 1):
            if self._stop:
                return
            
            self.progress.emit(i, total, f"Загрузка: {source.name}")
            
            if source.path:
                channels = self.parser.parse_url(source.path, source.name, timeout=15, use_cache=False)
                
                if channels:
                    for ch in channels:
                        ch.priority = source.priority
                        if ch.url not in self.seen_urls:
                            self.seen_urls.add(ch.url)
                            name_key = ch.name.lower()
                            self.name_to_channels[name_key].append(ch)
                    
                    logger.info(f"Source {source.name}: loaded {len(channels)} channels")
                    source.channel_count = len(channels)
                else:
                    logger.warning(f"Source {source.name}: no channels loaded")
                    source.last_error = "No channels loaded"
            
            QCoreApplication.processEvents()
        
        all_channels = []
        for name, channels in self.name_to_channels.items():
            for ch in channels:
                all_channels.append(ch)
        
        if self.use_smart_dedup and not self.keep_duplicates:
            deduplicator = SmartChannelDeduplicator(
                use_quality_priority=self.config.get('quality_priority', True),
                keep_duplicates=False
            )
            for ch in all_channels:
                deduplicator.add_channel(ch)
            self._channels = deduplicator.deduplicate()
        else:
            self._channels = all_channels
            self._channels.sort(key=lambda x: x.name.lower())
            logger.info(f"Keeping all {len(self._channels)} channels with unique URLs")
        
        logger.info(f"Total channels: {len(self._channels)}")
        self.finished.emit(self._channels)


class AutoProcessor(QThread):
    progress = pyqtSignal(str)
    load_progress = pyqtSignal(int, int, str)
    check_progress = pyqtSignal(int, int, str)
    channel_status = pyqtSignal(str, bool)
    finished = pyqtSignal(list, int)
    error = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._stop = False
        self.config = Config()
        self.cache_manager = CacheManager()
    
    def stop(self):
        self._stop = True
    
    def run(self):
        try:
            self.progress.emit("📋 Загрузка эталонных каналов...")
            ChannelFilter.ALLOWED_NAMES = ChannelFilter.load_allowed_channels()
            
            if not ChannelFilter.ALLOWED_NAMES:
                self.error.emit("Не удалось загрузить эталонные каналы.")
                return
            
            self.progress.emit(f"✅ Загружено {len(ChannelFilter.ALLOWED_NAMES)} эталонных каналов")
            
            manager = SourceManager()
            self.sources = manager.get_enabled()
            
            if not self.sources:
                self.error.emit("Нет доступных источников для загрузки")
                return
            
            self.progress.emit(f"📥 Загрузка из {len(self.sources)} источников...")
            
            keep_duplicates = self.config.get('keep_duplicates', True)
            generator = PlaylistGenerator(
                self.sources, 
                use_smart_dedup=True,
                keep_duplicates=keep_duplicates
            )
            generator.progress.connect(self.load_progress)
            generator.start()
            generator.wait()
            
            all_channels = generator._channels if hasattr(generator, '_channels') else []
            
            if not all_channels:
                self.error.emit("Не удалось загрузить каналы из источников")
                return
            
            self.progress.emit(f"🔍 Фильтрация по эталонным каналам ({len(all_channels)} найдено)...")
            
            filtered = []
            for ch in all_channels:
                if ChannelFilter.should_keep(ch.name):
                    if self.config.get('fallback_to_cached', True):
                        cached_url = self.cache_manager.get_working_link(ch.name)
                        if cached_url:
                            ch.url = cached_url
                            ch.is_working = True
                            ch.source_name = "cached"
                    filtered.append(ch)
            
            logger.info(f"After filtering: {len(filtered)} channels from {len(all_channels)}")
            
            if not filtered:
                self.progress.emit("⚠️ Не найдено совпадений с эталонными каналами")
                self.finished.emit([], len(all_channels))
                return
            
            channels_to_check = []
            for ch in filtered:
                cached_url = self.cache_manager.get_working_link(ch.name)
                if not cached_url:
                    channels_to_check.append(ch)
                else:
                    ch.is_working = True
                    ch.url = cached_url
                    ch.source_name = "cached"
                    ch.last_checked = datetime.now()
            
            self.progress.emit(f"🔄 Проверка {len(channels_to_check)} каналов...")
            
            if channels_to_check:
                checker = StreamChecker(channels_to_check)
                checker.progress.connect(self.check_progress)
                checker.channel_checked.connect(self.channel_status)
                checker.start()
                checker.wait()
                checked_results = checker.results if hasattr(checker, 'results') else []
            else:
                checked_results = []
            
            working = []
            for ch in filtered:
                if ch.is_working:
                    working.append(ch)
            
            for ch in checked_results:
                if ch.is_working and ch not in working:
                    working.append(ch)
            
            self.progress.emit(f"✅ Готово! Рабочих каналов: {len(working)}")
            self.finished.emit(working, len(all_channels))
            
        except Exception as e:
            logger.error(f"Error: {e}")
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Генератор m3u Ksenia v0.8")
        self.setMinimumSize(600, 550)
        
        self.setWindowIcon(create_app_icon())
        
        self.config = Config()
        self.processor: Optional[AutoProcessor] = None
        self.working_channels: List[ChannelData] = []
        self.cache_manager = CacheManager()
        
        self._setup_ui()
        self._setup_menu()
        self._setup_tray()
    
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        
        self.status_label = QLabel("⏳ Готов к работе")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 10px; background-color: #f0f0f0; border-radius: 5px;")
        layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        button_layout1 = QHBoxLayout()
        
        self.start_btn = QPushButton("▶ Создать новый плейлист")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                font-size: 13px;
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #45a049; }
        """)
        self.start_btn.clicked.connect(self._start)
        button_layout1.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("⏹ Стоп")
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                font-weight: bold;
                font-size: 13px;
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #d32f2f; }
            QPushButton:disabled { background-color: #cccccc; color: #888; }
        """)
        self.stop_btn.clicked.connect(self._stop)
        button_layout1.addWidget(self.stop_btn)
        
        layout.addLayout(button_layout1)
        
        button_layout2 = QHBoxLayout()
        
        self.save_btn = QPushButton("💾 Сохранить плейлист")
        self.save_btn.setMinimumHeight(40)
        self.save_btn.setEnabled(False)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                font-weight: bold;
                font-size: 13px;
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #1976D2; }
            QPushButton:disabled { background-color: #cccccc; color: #888; }
        """)
        self.save_btn.clicked.connect(self._save)
        button_layout2.addWidget(self.save_btn)
        
        self.update_btn = QPushButton("🔄 Обновить другие плейлисты")
        self.update_btn.setMinimumHeight(40)
        self.update_btn.setEnabled(False)
        self.update_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-weight: bold;
                font-size: 13px;
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #F57C00; }
            QPushButton:disabled { background-color: #cccccc; color: #888; }
        """)
        self.update_btn.clicked.connect(self._update_other_playlists)
        button_layout2.addWidget(self.update_btn)
        
        layout.addLayout(button_layout2)
        
        settings_layout = QHBoxLayout()
        self.keep_duplicates_cb = QCheckBox("Сохранять все рабочие дубликаты каналов")
        self.keep_duplicates_cb.setChecked(self.config.get('keep_duplicates', True))
        self.keep_duplicates_cb.stateChanged.connect(self._on_keep_duplicates_changed)
        settings_layout.addWidget(self.keep_duplicates_cb)
        settings_layout.addStretch()
        layout.addLayout(settings_layout)
        
        info_group = QGroupBox("📊 Статус")
        info_layout = QVBoxLayout()
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(150)
        self.info_text.setStyleSheet("font-size: 11px; background-color: #fafafa;")
        info_layout.addWidget(self.info_text)
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
        
        stats_layout = QHBoxLayout()
        self.working_count_label = QLabel("Рабочих каналов: 0")
        self.working_count_label.setStyleSheet("font-weight: bold; color: #4CAF50;")
        stats_layout.addWidget(self.working_count_label)
        stats_layout.addStretch()
        self.links_count_label = QLabel("Ссылок в кэше: 0")
        self.links_count_label.setStyleSheet("color: #666;")
        stats_layout.addWidget(self.links_count_label)
        layout.addLayout(stats_layout)
        
        layout.addStretch()
        
        info_label = QLabel("⚡ Версия 0.8 - Сохранение дубликатов каналов")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_label.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(info_label)
        
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Готов")
        
        self._update_info("Ожидание действий...")
        self._update_stats()
        self.config.save()
    
    def _setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("📁 Файл")
        
        save_action = QAction("💾 Сохранить плейлист", self)
        save_action.triggered.connect(self._save)
        file_menu.addAction(save_action)
        
        update_action = QAction("🔄 Обновить другие плейлисты", self)
        update_action.triggered.connect(self._update_other_playlists)
        file_menu.addAction(update_action)
        
        file_menu.addSeparator()
        exit_action = QAction("🚪 Выход", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        settings_menu = menubar.addMenu("⚙️ Настройки")
        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self._show_settings)
        settings_menu.addAction(settings_action)
        
        help_menu = menubar.addMenu("❓ Справка")
        license_action = QAction("⚖ Лицензия GPLv3", self)
        license_action.triggered.connect(self._show_license)
        help_menu.addAction(license_action)
        support_action = QAction("☕ Поддержка проекта", self)
        support_action.triggered.connect(lambda: QMessageBox.about(self, "Поддержка", 
            "Поддержать проект: https://yoomoney.ru/to/4100119518517127"))
        help_menu.addAction(support_action)
    
    def _show_settings(self):
        QMessageBox.information(self, "Настройки", 
            "Настройки можно изменить в файле config.json\n"
            "Основные параметры:\n"
            "- max_workers: количество потоков (по умолчанию 20)\n"
            "- check_timeout: таймаут проверки канала (сек)\n"
            "- max_channels_to_check: макс. каналов для проверки\n"
            "- update_broken_only: обновлять только неработающие каналы\n"
            "- fallback_to_cached: использовать кэшированные ссылки\n"
            "- keep_duplicates: сохранять дубликаты каналов")
    
    def _show_license(self):
        QMessageBox.about(self, "Лицензия GPLv3", 
            "GNU General Public License v3.0\n\n"
            "Это свободное программное обеспечение.")
    
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
    
    def _update_info(self, text: str):
        self.info_text.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        self.info_text.verticalScrollBar().setValue(
            self.info_text.verticalScrollBar().maximum()
        )
    
    def _update_stats(self):
        links = self.cache_manager.get_all_working_links()
        self.links_count_label.setText(f"Ссылок в кэше: {len(links)}")
        self.working_count_label.setText(f"Рабочих каналов: {len(self.working_channels)}")
    
    def _on_keep_duplicates_changed(self, state):
        self.config.set('keep_duplicates', state == Qt.CheckState.Checked.value)
        self.config.save()
    
    def _start(self):
        if self.processor and self.processor.isRunning():
            return
        
        self.config.save()
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.save_btn.setEnabled(False)
        self.update_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("⏳ Выполняется обработка...")
        self.working_channels = []
        
        self.processor = AutoProcessor()
        self.processor.progress.connect(self._on_progress)
        self.processor.load_progress.connect(self._on_load_progress)
        self.processor.check_progress.connect(self._on_check_progress)
        self.processor.channel_status.connect(self._on_channel_status)
        self.processor.finished.connect(self._on_finished)
        self.processor.error.connect(self._on_error)
        
        self.processor.start()
    
    def _stop(self):
        if self.processor and self.processor.isRunning():
            self.processor.stop()
            self.processor.wait(2000)
        
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("⏹ Остановлено пользователем")
        self.status_bar.showMessage("Остановлено")
        self._update_info("⏹ Остановлено пользователем")
    
    def _on_progress(self, msg: str):
        self.status_label.setText(msg)
        self.status_bar.showMessage(msg[:50])
        self._update_info(msg)
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
    
    def _on_channel_status(self, name: str, working: bool):
        status = "✅" if working else "❌"
        self._update_info(f"{status} {name[:50]}")
    
    def _on_finished(self, channels: List[ChannelData], total: int):
        self.working_channels = channels
        self.progress_bar.setVisible(False)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.save_btn.setEnabled(len(channels) > 0)
        self.update_btn.setEnabled(len(channels) > 0)
        
        working = len(channels)
        self.status_label.setText(f"✅ Готово! Рабочих каналов: {working} из {total}")
        self._update_info(f"✅ Завершено: {working} рабочих каналов")
        self._update_stats()
        
        if working > 0:
            QMessageBox.information(self, "Завершено", 
                f"Найдено рабочих каналов: {working}\n"
                f"Эталонных каналов: {len(ChannelFilter.ALLOWED_NAMES)}\n\n"
                f"Дубликаты сохранены: {self.config.get('keep_duplicates', True)}")
        else:
            QMessageBox.warning(self, "Завершено", 
                f"Не найдено рабочих каналов\n"
                f"Эталонных каналов: {len(ChannelFilter.ALLOWED_NAMES)}")
    
    def _on_error(self, error: str):
        self.progress_bar.setVisible(False)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.update_btn.setEnabled(False)
        self.status_label.setText("❌ Ошибка")
        self._update_info(f"❌ Ошибка: {error}")
        QMessageBox.critical(self, "Ошибка", error)
    
    def _save(self):
        if not self.working_channels:
            QMessageBox.warning(self, "Ошибка", "Нет каналов для сохранения")
            return
        
        if getattr(sys, 'frozen', False):
            default_path = os.path.dirname(sys.executable)
        else:
            default_path = self.config.get('save_path')
        
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить плейлист",
            os.path.join(default_path, "IPTVdonor.m3u"),
            "M3U файлы (*.m3u)"
        )
        
        if not path:
            return
        
        try:
            for ch in self.working_channels:
                if ch.is_working and ch.url:
                    self.cache_manager.save_working_link(ch.name, ch.url)
            
            with open(path, 'w', encoding='utf-8') as f:
                f.write('#EXTM3U\n')
                f.write(f'# Создано: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
                f.write(f'# Каналов: {len(self.working_channels)}\n')
                f.write(f'# Версия: 0.8\n\n')
                for ch in self.working_channels:
                    f.write(ch.to_m3u_line() + '\n')
            
            self.config.set('save_path', os.path.dirname(path))
            self.config.save()
            self.status_bar.showMessage(f"Сохранено: {os.path.basename(path)}")
            self._update_info(f"💾 Сохранено: {os.path.basename(path)} ({len(self.working_channels)} каналов)")
            self._update_stats()
            
            QMessageBox.information(self, "Успех", 
                f"Плейлист сохранён!\n{path}\n"
                f"Каналов: {len(self.working_channels)}\n"
                f"Дубликаты сохранены: {self.config.get('keep_duplicates', True)}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))
    
    def _update_other_playlists(self):
        if not self.working_channels:
            QMessageBox.warning(self, "Ошибка", "Сначала создайте рабочий плейлист")
            return
        
        working_links = {}
        for ch in self.working_channels:
            if ch.url and ch.is_working:
                working_links[ch.name.lower()] = ch.url
        
        if not working_links:
            QMessageBox.warning(self, "Ошибка", "Нет рабочих ссылок для обновления")
            return
        
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите плейлисты для обновления (M3U файлы)",
            self.config.get('save_path', os.path.expanduser("~/Desktop")),
            "M3U файлы (*.m3u *.m3u8);;Все файлы (*)"
        )
        
        if not files:
            return
        
        valid_files = [f for f in files if os.path.exists(f)]
        if not valid_files:
            QMessageBox.warning(self, "Ошибка", "Выбранные файлы не существуют")
            return
        
        logger.info(f"Выбрано для обновления: {len(valid_files)} файлов")
        logger.info(f"Доступно рабочих ссылок: {len(working_links)}")
        
        self.update_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("🔄 Обновление плейлистов...")
        
        self.updater = PlaylistUpdater(working_links)
        self.updater.set_playlists(valid_files)
        self.updater.progress.connect(self._on_update_progress)
        self.updater.channel_updated.connect(self._on_channel_updated)
        self.updater.finished.connect(self._on_update_finished)
        self.updater.error.connect(self._on_update_error)
        self.updater.start()
    
    def _on_update_progress(self, current: int, total: int, msg: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"Обновление: {current}/{total}")
        self.status_label.setText(msg)
        self._update_info(msg)
        QCoreApplication.processEvents()
    
    def _on_channel_updated(self, name: str, url: str):
        self._update_info(f"🔄 Обновлен: {name}")
    
    def _on_update_finished(self, updated: int):
        self.progress_bar.setVisible(False)
        self.update_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.status_label.setText(f"✅ Обновлено {updated} каналов")
        self._update_info(f"✅ Обновлено {updated} каналов в других плейлистах")
        
        if updated > 0:
            QMessageBox.information(self, "Готово", 
                f"Обновлено каналов: {updated}\n"
                f"Плейлисты успешно обновлены!")
        else:
            QMessageBox.information(self, "Готово", 
                "Не найдено совпадений для обновления.\n"
                "Проверьте, что названия каналов совпадают.")
    
    def _on_update_error(self, error: str):
        self.progress_bar.setVisible(False)
        self.update_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.status_label.setText("❌ Ошибка обновления")
        self._update_info(f"❌ Ошибка: {error}")
        QMessageBox.critical(self, "Ошибка", error)
    
    def closeEvent(self, event):
        if self.processor and self.processor.isRunning():
            self.processor.stop()
            self.processor.wait(2000)
        if hasattr(self, 'updater') and self.updater and self.updater.isRunning():
            self.updater.stop()
            self.updater.wait(2000)
        self.config.save()
        self.tray_icon.hide()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Генератор m3u Ksenia v0.8")
    app.setWindowIcon(create_app_icon())
    app.setStyle('Fusion')
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
