#!/usr/bin/env python3
"""
Ksenia Radio Player
Оптимизированный радио-плеер для работы с M3U плейлистами
Поддержка: Linux, Windows
"""

import sys
import os
import json
import re
import hashlib
import traceback
from pathlib import Path
from urllib.parse import urlparse
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

# --- Иконка приложения ---
APP_ICON = """<?xml version="1.0" encoding="UTF-8"?>
<svg width="64" height="64" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#1a3265"/>
            <stop offset="100%" stop-color="#244887"/>
        </linearGradient>
    </defs>
    <rect width="64" height="64" rx="12" fill="url(#bg)"/>
    <path d="M32 22C26.48 22 22 26.48 22 32C22 37.52 26.48 42 32 42C37.52 42 42 37.52 42 32C42 26.48 37.52 22 32 22ZM32 40C27.58 40 24 36.42 24 32C24 27.58 27.58 24 32 24C36.42 24 40 27.58 40 32C40 36.42 36.42 40 32 40Z" fill="white"/>
    <path d="M34 28H30V36H34V28Z" fill="white"/>
</svg>"""

# --- Класс M3U парсера ---
class M3UParser:
    """Оптимизированный парсер M3U-плейлистов"""
    
    @staticmethod
    def parse_file(file_path):
        """Парсинг M3U из локального файла или URL"""
        stations = []
        
        try:
            content = ""
            parsed = urlparse(file_path)
            
            if parsed.scheme in ('http', 'https'):
                try:
                    import requests
                    response = requests.get(file_path, timeout=15)
                    response.raise_for_status()
                    content = response.text
                except ImportError:
                    import urllib.request
                    with urllib.request.urlopen(file_path, timeout=15) as response:
                        content = response.read().decode('utf-8', errors='ignore')
                except Exception as e:
                    raise Exception(f"Ошибка загрузки URL: {str(e)}")
            else:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            
            if not content:
                raise Exception("Пустой плейлист")
            
            lines = content.strip().split('\n')
            i = 0
            
            while i < len(lines):
                line = lines[i].strip()
                
                if line.startswith('#EXTINF:'):
                    j = i + 1
                    url = ""
                    
                    while j < len(lines) and not url:
                        next_line = lines[j].strip()
                        if next_line and not next_line.startswith('#'):
                            url = next_line
                        j += 1
                    
                    if url:
                        try:
                            extinf_data = line[8:]
                            comma_pos = extinf_data.find(',')
                            
                            if comma_pos != -1:
                                title = extinf_data[comma_pos + 1:].strip()
                            else:
                                title = extinf_data.strip()
                            
                            attributes = {}
                            attr_matches = re.findall(r'(\S+?)="([^"]*)"', extinf_data[:comma_pos] if comma_pos != -1 else '')
                            for key, value in attr_matches:
                                attributes[key] = value
                            
                            tvg_name = title
                            if 'tvg-name' in attributes:
                                tvg_name = attributes['tvg-name']
                            elif 'tvg-id' in attributes:
                                tvg_name = attributes['tvg-id']
                            
                            station = {
                                'name': tvg_name,
                                'title': title,
                                'url': url,
                                'genre': attributes.get('group-title', 'Радио'),
                                'logo_url': attributes.get('tvg-logo', ''),
                                'available': True
                            }
                            
                            stations.append(station)
                            
                        except Exception as e:
                            print(f"Ошибка парсинга строки: {e}")
                    
                    i = j
                else:
                    i += 1
            
            return stations
            
        except Exception as e:
            raise Exception(f"Ошибка парсинга M3U: {str(e)}")

# --- Кастомный виджет для громкости с поддержкой колесика мыши ---
class VolumeControlWidget(QWidget):
    """Виджет управления громкостью с поддержкой колесика мыши"""
    
    volumeChanged = pyqtSignal(int)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()
        
    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(10)
        
        # Иконка громкости
        self.volume_icon = QLabel("🔊")
        self.volume_icon.setFixedWidth(25)
        self.volume_icon.setObjectName("volumeIcon")
        self.volume_icon.setToolTip("Колесико мыши для регулировки громкости")
        layout.addWidget(self.volume_icon)
        
        # Ползунок громкости
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setObjectName("volumeSlider")
        self.volume_slider.setToolTip("Колесико мыши для регулировки громкости")
        layout.addWidget(self.volume_slider, 1)
        
        # Метка громкости
        self.volume_label = QLabel("50%")
        self.volume_label.setFixedWidth(35)
        self.volume_label.setObjectName("volumeLabel")
        self.volume_label.setToolTip("Колесико мыши для регулировки громкости")
        layout.addWidget(self.volume_label)
        
        self.setLayout(layout)
    
    def set_volume(self, volume):
        """Установка громкости"""
        self.volume_slider.setValue(volume)
        self.volume_label.setText(f"{volume}%")
        
        # Обновляем иконку в зависимости от громкости
        if volume == 0:
            self.volume_icon.setText("🔇")
        elif volume < 30:
            self.volume_icon.setText("🔈")
        elif volume < 70:
            self.volume_icon.setText("🔉")
        else:
            self.volume_icon.setText("🔊")
    
    def wheelEvent(self, event):
        """Обработка колесика мыши для регулировки громкости"""
        delta = event.angleDelta().y()
        step = 5  # Шаг изменения громкости
        
        current_volume = self.volume_slider.value()
        
        if delta > 0:
            new_volume = min(100, current_volume + step)
        else:
            new_volume = max(0, current_volume - step)
        
        if new_volume != current_volume:
            self.volume_slider.setValue(new_volume)
            self.volumeChanged.emit(new_volume)
        
        event.accept()

# --- Кастомная панель заголовка ---
class TitleBar(QWidget):
    """Кастомная панель заголовка для перетаскивания окна"""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.drag_position = QPoint()
        self.init_ui()
        
    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(10)
        
        # Иконка и название
        icon_label = QLabel()
        icon_label.setPixmap(self.parent.windowIcon().pixmap(20, 20))
        
        title_label = QLabel("Ksenia Radio")
        title_label.setStyleSheet("font-weight: bold; color: #ffffff; font-size: 14px;")
        
        layout.addWidget(icon_label)
        layout.addWidget(title_label)
        layout.addStretch()
        
        # Кнопка блокировки экрана
        self.lock_btn = QPushButton("🔒")
        self.lock_btn.setFixedSize(25, 25)
        self.lock_btn.setCheckable(True)
        self.lock_btn.setChecked(self.parent.screen_locked)
        self.lock_btn.clicked.connect(self.parent.toggle_screen_lock)
        self.lock_btn.setToolTip("Блокировка экрана")
        self.lock_btn.setObjectName("windowButton")
        layout.addWidget(self.lock_btn)
        
        # Кнопки управления окном
        self.minimize_btn = QPushButton("—")
        self.minimize_btn.setFixedSize(25, 25)
        self.minimize_btn.clicked.connect(self.parent.showMinimized)
        self.minimize_btn.setObjectName("windowButton")
        
        self.maximize_btn = QPushButton("□")
        self.maximize_btn.setFixedSize(25, 25)
        self.maximize_btn.clicked.connect(self.toggle_maximize)
        self.maximize_btn.setObjectName("windowButton")
        
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(25, 25)
        self.close_btn.clicked.connect(self.parent.close)
        self.close_btn.setObjectName("closeButton")
        
        layout.addWidget(self.minimize_btn)
        layout.addWidget(self.maximize_btn)
        layout.addWidget(self.close_btn)
        
        self.setLayout(layout)
    
    def toggle_maximize(self):
        if self.parent.isMaximized():
            self.parent.showNormal()
            self.maximize_btn.setText("□")
        else:
            self.parent.showMaximized()
            self.maximize_btn.setText("❐")

# --- Окно блокировки экрана ---
class ScreenLockWindow(QWidget):
    """Окно блокировки экрана"""
    
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.init_ui()
        
    def init_ui(self):
        # Устанавливаем флаги для окна блокировки
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        
        # На весь экран
        screen_geometry = QApplication.primaryScreen().geometry()
        self.setGeometry(screen_geometry)
        
        # Темно-синий фон
        self.setStyleSheet("""
            QWidget {
                background-color: rgba(0, 18, 36, 0.95);
            }
        """)
        
        # Основной контейнер
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Центральный виджет с информацией
        center_widget = QWidget()
        center_widget.setObjectName("centerWidget")
        center_layout = QVBoxLayout(center_widget)
        center_layout.setSpacing(20)
        center_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # Иконка радио
        radio_icon = QLabel("📻")
        radio_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        radio_icon.setStyleSheet("""
            QLabel {
                font-size: 80px;
                color: #2677e6;
            }
        """)
        center_layout.addWidget(radio_icon)
        
        # Название текущей станции
        self.station_label = QLabel("Радио не играет")
        self.station_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.station_label.setStyleSheet("""
            QLabel {
                font-size: 28px;
                font-weight: bold;
                color: #ffffff;
                padding: 10px;
            }
        """)
        self.station_label.setWordWrap(True)
        center_layout.addWidget(self.station_label)
        
        # Жанр станции
        self.genre_label = QLabel("")
        self.genre_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.genre_label.setStyleSheet("""
            QLabel {
                font-size: 18px;
                color: #99bbee;
                font-style: italic;
            }
        """)
        center_layout.addWidget(self.genre_label)
        
        # Статус воспроизведения
        self.status_label = QLabel("⏸ Пауза")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("""
            QLabel {
                font-size: 16px;
                color: #2677e6;
                font-weight: bold;
                margin-top: 20px;
            }
        """)
        center_layout.addWidget(self.status_label)
        
        # Инструкция
        instruction = QLabel("Двойной клик для разблокировки")
        instruction.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instruction.setStyleSheet("""
            QLabel {
                font-size: 14px;
                color: #7799cc;
                margin-top: 40px;
                padding: 10px;
                border: 1px solid #3d5577;
                border-radius: 5px;
            }
        """)
        center_layout.addWidget(instruction)
        
        main_layout.addWidget(center_widget)
    
    def update_info(self, station_name, station_genre, is_playing):
        """Обновление информации на экране блокировки"""
        self.station_label.setText(station_name if station_name else "Радио не играет")
        self.genre_label.setText(station_genre if station_genre else "")
        self.status_label.setText("▶ В эфире" if is_playing else "⏸ Пауза")
    
    def mouseDoubleClickEvent(self, event):
        """Обработка двойного клика для разблокировки"""
        self.parent.toggle_screen_lock()
        event.accept()
    
    def keyPressEvent(self, event):
        """Обработка нажатий клавиш"""
        # ESC или F11 для разблокировки
        if event.key() in [Qt.Key.Key_Escape, Qt.Key.Key_F11, Qt.Key.Key_Space]:
            self.parent.toggle_screen_lock()
        else:
            super().keyPressEvent(event)

# --- Главный класс плеера ---
class KseniaRadioPlayer(QMainWindow):
    """Основной класс радио-плеера"""
    
    image_loaded = pyqtSignal(str, QPixmap)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ksenia Radio")
        self.setGeometry(100, 100, 500, 650)
        self.setMinimumSize(450, 500)
        
        # Флаг для отслеживания изменения размера
        self._resizing = False
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None
        self._mouse_pos = QPoint()  # Для отслеживания позиции мыши
        
        # Переменные для блокировки экрана
        self.screen_locked = False
        self.lock_window = None
        
        # Переменные для перетаскивания
        self._dragging = False
        self._drag_position = QPoint()
        
        # Флаг для предотвращения рекурсии при ошибках
        self._error_handling = False
        
        # Устанавливаем флаги окна
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
        )
        
        # Включаем прозрачность для закругленных углов
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # Включаем отслеживание мыши для изменения курсора
        self.setMouseTracking(True)
        
        self.set_app_icon()
        
        # Инициализация медиаплеера
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        
        # Менеджер сетевых запросов
        self.network_manager = QNetworkAccessManager()
        self.network_manager.finished.connect(self.on_image_loaded)
        
        # Переменные состояния
        self.current_volume = 50
        self.radio_stations = []
        self.current_index = -1
        self._skip_error = False  # Флаг для пропуска ошибок при смене станции
        
        # Кэширование
        self.logo_cache = {}
        self.pending_image_requests = {}
        
        # Настройки
        self.load_settings()
        
        # Инициализация интерфейса
        self.init_ui()
        
        # Подключение сигналов
        self.image_loaded.connect(self.on_image_loaded_signal)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        self.audio_output.volumeChanged.connect(self.on_volume_changed)
        self.player.errorOccurred.connect(self.on_player_error)
        
        # Начальная громкость
        self.audio_output.setVolume(self.current_volume / 100.0)
        
        # Загрузка плейлиста по умолчанию
        QTimer.singleShot(100, self.load_default_playlist)
    
    def set_app_icon(self):
        """Установка иконки приложения"""
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.svg', delete=False) as f:
                f.write(APP_ICON)
                temp_svg = f.name
            pixmap = QPixmap(temp_svg)
            if not pixmap.isNull():
                self.setWindowIcon(QIcon(pixmap))
            os.unlink(temp_svg)
        except:
            pixmap = QPixmap(64, 64)
            pixmap.fill(QColor(27, 54, 103))
            painter = QPainter(pixmap)
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont("Arial", 24))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "📻")
            painter.end()
            self.setWindowIcon(QIcon(pixmap))
    
    def init_ui(self):
        """Инициализация интерфейса"""
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Кастомная панель заголовка
        self.title_bar = TitleBar(self)
        main_layout.addWidget(self.title_bar)
        
        # Основное содержимое
        content_widget = QWidget()
        content_widget.setObjectName("contentWidget")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(15, 15, 15, 15)
        content_layout.setSpacing(15)
        
        # Область информации
        info_container = QWidget()
        info_container.setObjectName("infoContainer")
        info_layout = QHBoxLayout(info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(15)
        
        # Изображение станции
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(120, 120)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("""
            QLabel {
                background-color: #1a1a2e;
                border: 2px solid #2677e6;
                border-radius: 10px;
            }
        """)
        self.set_default_image()
        info_layout.addWidget(self.cover_label)
        
        # Информация о станции
        info_text_widget = QWidget()
        info_text_layout = QVBoxLayout(info_text_widget)
        info_text_layout.setSpacing(8)
        
        self.title_label = QLabel("Выберите радиостанцию")
        self.title_label.setObjectName("titleLabel")
        self.title_label.setWordWrap(True)
        
        self.genre_label = QLabel("Загрузка плейлиста...")
        self.genre_label.setObjectName("genreLabel")
        
        self.status_label = QLabel("Готов к работе")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        
        info_text_layout.addWidget(self.title_label)
        info_text_layout.addWidget(self.genre_label)
        info_text_layout.addWidget(self.status_label)
        info_text_layout.addStretch()
        
        info_layout.addWidget(info_text_widget, 1)
        content_layout.addWidget(info_container)
        
        # Панель управления плеером
        controls_container = QWidget()
        controls_layout = QHBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(5)
        
        # Кнопки управления
        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setFixedSize(45, 45)
        self.prev_btn.clicked.connect(self.prev_station)
        self.prev_btn.setToolTip("Предыдущая станция")
        self.prev_btn.setObjectName("controlButton")
        
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(55, 55)
        self.play_btn.clicked.connect(self.toggle_play)
        self.play_btn.setToolTip("Воспроизвести/Пауза")
        self.play_btn.setObjectName("playButton")
        
        self.next_btn = QPushButton("⏭")
        self.next_btn.setFixedSize(45, 45)
        self.next_btn.clicked.connect(self.next_station)
        self.next_btn.setToolTip("Следующая станция")
        self.next_btn.setObjectName("controlButton")
        
        # Центрируем кнопки
        controls_layout.addStretch()
        controls_layout.addWidget(self.prev_btn)
        controls_layout.addWidget(self.play_btn)
        controls_layout.addWidget(self.next_btn)
        controls_layout.addStretch()
        
        content_layout.addWidget(controls_container)
        
        # Ползунок громкости с поддержкой колесика мыши
        self.volume_control = VolumeControlWidget()
        self.volume_control.volume_slider.valueChanged.connect(self.set_volume)
        self.volume_control.volumeChanged.connect(self.set_volume)
        self.volume_control.set_volume(self.current_volume)
        content_layout.addWidget(self.volume_control)
        
        # Таблица радиостанций
        self.stations_table = QTableWidget()
        self.stations_table.setObjectName("stationsTable")
        self.stations_table.setColumnCount(2)
        self.stations_table.setHorizontalHeaderLabels(["Название", "Жанр"])
        self.stations_table.horizontalHeader().setStretchLastSection(True)
        self.stations_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stations_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stations_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.stations_table.doubleClicked.connect(self.play_selected_station)
        self.stations_table.setAlternatingRowColors(True)
        
        # Показываем вертикальный заголовок для отображения количества строк
        self.stations_table.verticalHeader().setVisible(True)
        self.stations_table.verticalHeader().setDefaultSectionSize(35)
        self.stations_table.verticalHeader().setMinimumWidth(40)
        
        # Настройка таблицы
        header = self.stations_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        
        content_layout.addWidget(self.stations_table, 1)
        
        # Панель статуса
        status_widget = QWidget()
        status_layout = QHBoxLayout(status_widget)
        status_layout.setContentsMargins(5, 5, 5, 5)
        
        self.station_count_label = QLabel("Станций: 0")
        self.station_count_label.setObjectName("statusLabel")
        
        self.refresh_btn = QPushButton("🔄 Обновить")
        self.refresh_btn.clicked.connect(self.load_default_playlist)
        self.refresh_btn.setObjectName("refreshButton")
        
        status_layout.addWidget(self.station_count_label)
        status_layout.addStretch()
        status_layout.addWidget(self.refresh_btn)
        
        content_layout.addWidget(status_widget)
        
        main_layout.addWidget(content_widget, 1)
        
        self.apply_styles()
    
    # --- Функции перетаскивания и изменения размера ---
    def get_resize_edge(self, pos):
        """Определяет, на какой границе находится курсор для изменения размера"""
        margin = 8
        rect = self.rect()
        
        if pos.x() <= margin and pos.y() <= margin:
            return "top_left"
        elif pos.x() >= rect.width() - margin and pos.y() <= margin:
            return "top_right"
        elif pos.x() <= margin and pos.y() >= rect.height() - margin:
            return "bottom_left"
        elif pos.x() >= rect.width() - margin and pos.y() >= rect.height() - margin:
            return "bottom_right"
        elif pos.x() <= margin:
            return "left"
        elif pos.x() >= rect.width() - margin:
            return "right"
        elif pos.y() <= margin:
            return "top"
        elif pos.y() >= rect.height() - margin:
            return "bottom"
        
        return None
    
    def set_cursor_for_edge(self, edge):
        """Устанавливает соответствующий курсор для границы"""
        if edge is None:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        elif edge in ["top", "bottom"]:
            self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
        elif edge in ["left", "right"]:
            self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
        elif edge in ["top_left", "bottom_right"]:
            self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
        elif edge in ["top_right", "bottom_left"]:
            self.setCursor(QCursor(Qt.CursorShape.SizeBDiagCursor))
    
    def mouseMoveEvent(self, event):
        """Обработка движения мыши"""
        pos = event.position().toPoint()
        self._mouse_pos = pos
        
        if not self._dragging and not self._resizing:
            title_bar_rect = self.title_bar.rect()
            title_bar_rect.moveTopLeft(self.title_bar.mapTo(self, QPoint(0, 0)))
            
            if title_bar_rect.contains(pos):
                if self.cursor().shape() != Qt.CursorShape.ArrowCursor:
                    self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            else:
                edge = self.get_resize_edge(pos)
                self.set_cursor_for_edge(edge)
        
        if self._dragging:
            new_pos = event.globalPosition().toPoint() - self._drag_position
            self.move(new_pos)
            event.accept()
            return
        
        if self._resizing and self._resize_edge is not None:
            self.handle_resize(event.globalPosition().toPoint())
            event.accept()
            return
        
        super().mouseMoveEvent(event)
    
    def mousePressEvent(self, event):
        """Обработка нажатия кнопки мыши"""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            
            title_bar_rect = self.title_bar.rect()
            title_bar_rect.moveTopLeft(self.title_bar.mapTo(self, QPoint(0, 0)))
            
            if title_bar_rect.contains(pos) and self.get_resize_edge(pos) is None:
                self._dragging = True
                self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
            
            edge = self.get_resize_edge(pos)
            if edge is not None:
                self._resizing = True
                self._resize_edge = edge
                self._resize_start_pos = event.globalPosition().toPoint()
                self._resize_start_geometry = self.geometry()
                event.accept()
                return
        
        super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event):
        """Обработка отпускания кнопки мыши"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._resizing = False
            self._resize_edge = None
            
            pos = event.position().toPoint()
            edge = self.get_resize_edge(pos)
            self.set_cursor_for_edge(edge)
            event.accept()
        
        super().mouseReleaseEvent(event)
    
    def leaveEvent(self, event):
        """Обработка выхода курсора за пределы окна"""
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        super().leaveEvent(event)
    
    def handle_resize(self, global_pos):
        """Обработка изменения размера окна"""
        if not self._resize_start_geometry:
            return
        
        delta_x = global_pos.x() - self._resize_start_pos.x()
        delta_y = global_pos.y() - self._resize_start_pos.y()
        
        x = self._resize_start_geometry.x()
        y = self._resize_start_geometry.y()
        width = self._resize_start_geometry.width()
        height = self._resize_start_geometry.height()
        
        if self._resize_edge == "left":
            new_width = max(self.minimumWidth(), width - delta_x)
            new_x = x + (width - new_width)
            self.setGeometry(new_x, y, new_width, height)
        elif self._resize_edge == "right":
            new_width = max(self.minimumWidth(), width + delta_x)
            self.setGeometry(x, y, new_width, height)
        elif self._resize_edge == "top":
            new_height = max(self.minimumHeight(), height - delta_y)
            new_y = y + (height - new_height)
            self.setGeometry(x, new_y, width, new_height)
        elif self._resize_edge == "bottom":
            new_height = max(self.minimumHeight(), height + delta_y)
            self.setGeometry(x, y, width, new_height)
        elif self._resize_edge == "top_left":
            new_width = max(self.minimumWidth(), width - delta_x)
            new_height = max(self.minimumHeight(), height - delta_y)
            new_x = x + (width - new_width)
            new_y = y + (height - new_height)
            self.setGeometry(new_x, new_y, new_width, new_height)
        elif self._resize_edge == "top_right":
            new_width = max(self.minimumWidth(), width + delta_x)
            new_height = max(self.minimumHeight(), height - delta_y)
            new_y = y + (height - new_height)
            self.setGeometry(x, new_y, new_width, new_height)
        elif self._resize_edge == "bottom_left":
            new_width = max(self.minimumWidth(), width - delta_x)
            new_height = max(self.minimumHeight(), height + delta_y)
            new_x = x + (width - new_width)
            self.setGeometry(new_x, y, new_width, new_height)
        elif self._resize_edge == "bottom_right":
            new_width = max(self.minimumWidth(), width + delta_x)
            new_height = max(self.minimumHeight(), height + delta_y)
            self.setGeometry(x, y, new_width, new_height)
    
    def paintEvent(self, event):
        """Отрисовка закругленных углов и границы"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        path = QPainterPath()
        rect = self.rect()
        rect_f = QRectF(rect)
        path.addRoundedRect(rect_f, 12, 12)
        
        gradient = QLinearGradient(rect_f.topLeft(), rect_f.bottomRight())
        gradient.setColorAt(0, QColor(14, 23, 41))
        gradient.setColorAt(1, QColor(27, 54, 103))
        painter.fillPath(path, gradient)
        
        painter.setPen(QPen(QColor(38, 119, 230), 2))
        painter.drawPath(path)
        
        super().paintEvent(event)
    
    # --- Функции блокировки экрана ---
    def toggle_screen_lock(self):
        """Переключение режима блокировки экрана"""
        self.screen_locked = not self.screen_locked
        
        if self.screen_locked:
            self.enter_screen_lock_mode()
        else:
            self.exit_screen_lock_mode()
    
    def enter_screen_lock_mode(self):
        """Вход в режим блокировки экрана"""
        if not self.lock_window:
            self.lock_window = ScreenLockWindow(self)
        
        station_name = ""
        station_genre = ""
        is_playing = False
        
        if self.current_index >= 0 and self.current_index < len(self.radio_stations):
            station = self.radio_stations[self.current_index]
            station_name = station['name']
            station_genre = station['genre']
            is_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        
        self.lock_window.update_info(station_name, station_genre, is_playing)
        self.lock_window.showFullScreen()
        self.hide()
        
        if hasattr(self, 'title_bar'):
            self.title_bar.lock_btn.setChecked(True)
            self.title_bar.lock_btn.setText("🔓")
    
    def exit_screen_lock_mode(self):
        """Выход из режима блокировки экрана"""
        if self.lock_window:
            self.lock_window.hide()
        
        self.show()
        
        if hasattr(self, 'title_bar'):
            self.title_bar.lock_btn.setChecked(False)
            self.title_bar.lock_btn.setText("🔒")
    
    def update_lock_screen_info(self):
        """Обновление информации на экране блокировки"""
        if self.screen_locked and self.lock_window:
            station_name = ""
            station_genre = ""
            is_playing = False
            
            if self.current_index >= 0 and self.current_index < len(self.radio_stations):
                station = self.radio_stations[self.current_index]
                station_name = station['name']
                station_genre = station['genre']
                is_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            
            self.lock_window.update_info(station_name, station_genre, is_playing)
    
    def load_default_playlist(self):
        """Загрузка плейлиста по умолчанию"""
        default_url = "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPRadio.m3u"
        self.status_label.setText("Загрузка плейлиста...")
        QApplication.processEvents()
        
        try:
            stations = M3UParser.parse_file(default_url)
            self.radio_stations = stations
            self.update_stations_table()
            self.status_label.setText("Готов к воспроизведению")
            self.genre_label.setText(f"{len(stations)} радиостанций")
            self.station_count_label.setText(f"Станций: {len(stations)}")
            
            QTimer.singleShot(100, self.preload_station_logos)
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить плейлист:\n{str(e)}")
            self.status_label.setText("Ошибка загрузки плейлиста")
    
    def preload_station_logos(self):
        """Фоновая предзагрузка логотипов"""
        for i, station in enumerate(self.radio_stations):
            logo_url = station.get('logo_url', '')
            if logo_url and logo_url not in self.logo_cache and logo_url not in self.pending_image_requests:
                self.load_image_from_url(logo_url, f"station_{i}")
    
    def load_image_from_url(self, url, identifier):
        """Асинхронная загрузка изображения"""
        if not url or url in self.pending_image_requests:
            return
        
        try:
            request = QNetworkRequest(QUrl(url))
            request.setAttribute(QNetworkRequest.Attribute.User, identifier)
            self.pending_image_requests[url] = identifier
            self.network_manager.get(request)
        except Exception as e:
            print(f"Ошибка загрузки изображения: {e}")
            if url in self.pending_image_requests:
                del self.pending_image_requests[url]
    
    def on_image_loaded(self, reply):
        """Обработка загруженного изображения"""
        url = reply.url().toString()
        identifier = reply.request().attribute(QNetworkRequest.Attribute.User)
        
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = reply.readAll()
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    self.logo_cache[url] = pixmap
                    self.image_loaded.emit(identifier, pixmap)
        except Exception as e:
            print(f"Ошибка обработки изображения: {e}")
        finally:
            if url in self.pending_image_requests:
                del self.pending_image_requests[url]
            reply.deleteLater()
    
    def on_image_loaded_signal(self, identifier, pixmap):
        """Обновление изображения при загрузке"""
        if identifier.startswith("station_"):
            try:
                station_index = int(identifier.split("_")[1])
                if (self.current_index == station_index and 
                    self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState):
                    self.set_station_image(self.radio_stations[station_index]['name'])
            except (ValueError, IndexError):
                pass
    
    # --- Управление изображениями ---
    def set_default_image(self):
        """Установка изображения по умолчанию"""
        pixmap = QPixmap(120, 120)
        pixmap.fill(QColor(26, 36, 66))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        gradient = QLinearGradient(0, 0, 120, 120)
        gradient.setColorAt(0, QColor(38, 119, 230))
        gradient.setColorAt(1, QColor(122, 185, 225))
        
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(5, 5, 110, 110, 10, 10)
        
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Arial", 36))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "📻")
        painter.end()
        self.cover_label.setPixmap(pixmap)
    
    def set_station_image(self, station_name):
        """Установка изображения радиостанции"""
        station = None
        station_index = -1
        
        for i, s in enumerate(self.radio_stations):
            if s['name'] == station_name:
                station = s
                station_index = i
                break
        
        if not station:
            self.set_default_image()
            return
        
        logo_url = station.get('logo_url', '')
        
        if logo_url and logo_url in self.logo_cache:
            pixmap = self.logo_cache[logo_url]
            scaled_pixmap = pixmap.scaled(120, 120, Qt.AspectRatioMode.KeepAspectRatio, 
                                         Qt.TransformationMode.SmoothTransformation)
            self.cover_label.setPixmap(scaled_pixmap)
            return
        
        self.create_station_gradient(station_name)
        
        if logo_url and station_index >= 0:
            self.load_image_from_url(logo_url, f"station_{station_index}")
    
    def create_station_gradient(self, station_name):
        """Создание градиента для станции"""
        pixmap = QPixmap(120, 120)
        pixmap.fill(QColor(26, 36, 66))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        gradient = QLinearGradient(0, 0, 120, 120)
        
        hash_obj = hashlib.md5(station_name.encode())
        hash_hex = hash_obj.hexdigest()[:6]
        
        r = int(hash_hex[0:2], 16) % 45 + 90
        g = int(hash_hex[2:4], 16) % 90 + 135
        b = int(hash_hex[4:6], 16) % 90 + 180
        
        r = min(r, 135)
        g = min(g, 230)
        b = min(b, 230)
        
        gradient.setColorAt(0, QColor(r, g, b))
        gradient.setColorAt(1, QColor(max(r-63, 27), max(g-63, 72), max(b-45, 135)))
        
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(5, 5, 110, 110, 10, 10)
        
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        
        display_name = station_name[:10] if len(station_name) > 10 else station_name
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, display_name)
        painter.end()
        self.cover_label.setPixmap(pixmap)
    
    def update_stations_table(self):
        """Обновление таблицы радиостанций"""
        self.stations_table.setRowCount(len(self.radio_stations))
        
        for i in range(len(self.radio_stations)):
            self.stations_table.setVerticalHeaderItem(i, QTableWidgetItem(str(i + 1)))
        
        for i, station in enumerate(self.radio_stations):
            status_icon = "▶" if station.get('available', True) else "❌"
            has_logo = "🖼️" if station.get('logo_url') else ""
            name_text = f"{status_icon} {has_logo} {station['name']}"
            name_item = QTableWidgetItem(name_text)
            
            if not station.get('available', True):
                name_item.setForeground(QColor(90, 135, 180))
            else:
                name_item.setForeground(QColor(198, 216, 230))
            
            name_item.setToolTip(f"Жанр: {station['genre']}")
            self.stations_table.setItem(i, 0, name_item)
            
            genre_item = QTableWidgetItem(station['genre'])
            genre_item.setForeground(QColor(162, 198, 230))
            self.stations_table.setItem(i, 1, genre_item)
    
    # --- Управление воспроизведением ---
    def play_selected_station(self):
        """Воспроизведение выбранной станции"""
        selected_row = self.stations_table.currentRow()
        if 0 <= selected_row < len(self.radio_stations):
            self._skip_error = True
            self.play_radio_station(selected_row)
            self.update_lock_screen_info()
    
    def play_radio_station(self, index):
        """Воспроизведение радиостанции по индексу"""
        if index < len(self.radio_stations):
            station = self.radio_stations[index]
            
            if not station.get('available', True):
                self.status_label.setText("❌ Станция недоступна")
                return
            
            try:
                # Останавливаем текущее воспроизведение
                self.player.stop()
                
                # Устанавливаем новый источник
                self.player.setSource(QUrl(station['url']))
                
                self.title_label.setText(station['name'])
                self.genre_label.setText(station['genre'])
                self.status_label.setText("▶ В эфире")
                
                self.set_station_image(station['name'])
                
                # Запускаем воспроизведение с небольшой задержкой
                QTimer.singleShot(100, lambda: self._start_playback(index))
                
            except Exception as e:
                print(f"Ошибка подключения: {e}")
                self.status_label.setText("❌ Ошибка воспроизведения")
    
    def _start_playback(self, index):
        """Запуск воспроизведения после установки источника"""
        if index < len(self.radio_stations):
            self.player.play()
            self.current_index = index
            self.play_btn.setText("⏸")
            self.highlight_current_station(index)
            self._skip_error = False
    
    def toggle_play(self):
        """Переключение воспроизведения/паузы"""
        if not self.radio_stations:
            self.status_label.setText("Нет станций для воспроизведения")
            return
            
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.status_label.setText("⏸ Пауза")
            self.play_btn.setText("▶")
        else:
            if self.current_index >= 0:
                self.player.play()
                self.status_label.setText("▶ В эфире")
                self.play_btn.setText("⏸")
            else:
                if self.radio_stations:
                    self.play_radio_station(0)
        
        self.update_lock_screen_info()
    
    def prev_station(self):
        """Предыдущая станция"""
        if not self.radio_stations:
            return
        
        self._skip_error = True
        new_index = self.current_index - 1 if self.current_index > 0 else len(self.radio_stations) - 1
        self.play_radio_station(new_index)
    
    def next_station(self):
        """Следующая станция"""
        if not self.radio_stations:
            return
        
        self._skip_error = True
        new_index = (self.current_index + 1) % len(self.radio_stations)
        self.play_radio_station(new_index)
    
    def highlight_current_station(self, index):
        """Выделение текущей станции в таблице"""
        for i in range(self.stations_table.rowCount()):
            for col in range(2):
                item = self.stations_table.item(i, col)
                if item:
                    if i == index:
                        item.setBackground(QColor(38, 119, 230, 80))
                        item.setForeground(QColor(255, 255, 255))
                    else:
                        item.setBackground(QBrush())
                        station = self.radio_stations[i]
                        item.setForeground(QColor(90, 135, 180) if not station.get('available', True) else QColor(198, 216, 230))
    
    # --- Обработка ошибок ---
    def handle_error(self, context, error):
        """Обработка ошибок"""
        print(f"Ошибка: {context}: {error}")
        if not self._skip_error:
            self.status_label.setText("❌ Ошибка воспроизведения")
    
    def on_playback_state_changed(self, state):
        """Обработка изменения состояния воспроизведения"""
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self.play_btn.setText("▶")
            if self.current_index >= 0:
                self.highlight_current_station(self.current_index)
        
        self.update_lock_screen_info()
    
    def on_player_error(self, error, error_string):
        """Обработка ошибок медиаплеера - с защитой от рекурсии"""
        if self._error_handling or self._skip_error:
            return
        
        self._error_handling = True
        
        try:
            error_msg = str(error_string)
            print(f"Ошибка воспроизведения: {error_msg}")
            
            # Игнорируем ошибки декодирования mp3
            if "mp3float" in error_msg or "overread" in error_msg:
                # Просто игнорируем ошибки декодирования
                pass
            elif "Resource not found" in error_msg or "404" in error_msg:
                self.status_label.setText("❌ Файл не найден")
            elif "resolve" in error_msg or "NetworkError" in error_msg:
                self.status_label.setText("❌ Нет подключения")
            elif "format" in error_msg.lower() or "unsupported" in error_msg.lower():
                self.status_label.setText("❌ Неподдерживаемый формат")
            else:
                self.status_label.setText("❌ Ошибка воспроизведения")
            
            if self.current_index >= 0 and self.current_index < len(self.radio_stations) and not self._skip_error:
                self.radio_stations[self.current_index]['available'] = False
                self.update_stations_table()
            
            self.update_lock_screen_info()
            
        finally:
            self._error_handling = False
    
    # --- Управление громкостью ---
    def set_volume(self, volume):
        """Установка громкости"""
        self.current_volume = volume
        self.audio_output.setVolume(volume / 100.0)
        self.volume_control.set_volume(volume)
    
    def on_volume_changed(self, volume):
        """Обработка изменения громкости"""
        try:
            int_volume = int(volume * 100)
            self.current_volume = int_volume
            self.volume_control.set_volume(int_volume)
        except:
            pass
    
    # --- Стили ---
    def apply_styles(self):
        """Применение стилей"""
        style_sheet = """
            QMainWindow { 
                background-color: transparent; 
            }
            
            QWidget#centralWidget { 
                background-color: transparent; 
            }
            
            QWidget#contentWidget {
                background-color: #0e1524;
                border-radius: 12px;
            }
            
            QWidget#TitleBar {
                background-color: #18213b;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 2px solid #2677e6;
                height: 35px;
            }
            
            QPushButton#windowButton {
                background-color: #273450;
                border: 1px solid #3d5c77;
                border-radius: 3px;
                color: #ffffff;
                font-size: 12px;
            }
            QPushButton#windowButton:hover {
                background-color: #374560;
                border-color: #2677e6;
            }
            
            QPushButton#closeButton {
                background-color: #273450;
                border: 1px solid #3d5c77;
                border-radius: 3px;
                color: #ffffff;
                font-size: 12px;
            }
            QPushButton#closeButton:hover {
                background-color: #ff4757;
                border-color: #ff6b81;
            }
            
            QLabel#titleLabel { 
                font-size: 16px; 
                font-weight: bold; 
                color: #ffffff; 
            }
            QLabel#genreLabel { 
                font-size: 13px; 
                color: #99bbee; 
                font-style: italic;
            }
            QLabel#statusLabel { 
                font-size: 13px; 
                color: #2677e6; 
                font-weight: bold;
            }
            QLabel#volumeLabel {
                font-size: 11px;
                color: #99bbee;
            }
            QLabel#volumeIcon {
                color: #99bbee;
            }
            
            QPushButton#controlButton {
                background-color: #273450;
                border: 2px solid #3d5c77;
                border-radius: 8px;
                font-size: 18px;
                color: #ffffff;
                margin: 0;
            }
            QPushButton#controlButton:hover {
                background-color: #374560;
                border-color: #2677e6;
            }
            
            QPushButton#playButton {
                background-color: #2677e6;
                border: 2px solid #4394e6;
                border-radius: 10px;
                font-size: 22px;
                color: #ffffff;
                margin: 0;
            }
            QPushButton#playButton:hover {
                background-color: #4394e6;
                border-color: #5ab0e6;
            }
            
            QSlider#volumeSlider {
                height: 20px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #273450;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #2677e6;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #4394e6;
            }
            
            QTableWidget#stationsTable {
                background-color: #18213b; 
                border: 1px solid #273450; 
                border-radius: 5px;
                gridline-color: #273450; 
                font-size: 12px; 
                alternate-background-color: #151d35;
                color: #ffffff;
            }
            
            QTableWidget#stationsTable QTableCornerButton::section {
                background-color: #273450;
                border: none;
                border-bottom: 2px solid #2677e6;
                border-right: 1px solid #3d5c77;
                min-width: 40px;
                min-height: 35px;
            }
            
            QTableWidget#stationsTable QHeaderView::section:vertical {
                background-color: #273450;
                padding: 8px;
                border: none;
                border-right: 1px solid #3d5c77;
                border-bottom: 1px solid #273450;
                color: #99bbee;
                font-size: 11px;
                font-weight: normal;
            }
            
            QTableWidget#stationsTable QHeaderView::section:vertical:hover {
                background-color: #374560;
            }
            
            QTableWidget#stationsTable QHeaderView::section:horizontal {
                background-color: #273450;
                padding: 10px;
                border: none;
                border-bottom: 2px solid #2677e6;
                border-right: 1px solid #3d5c77;
                font-size: 12px;
                font-weight: bold;
                color: #ffffff;
            }
            
            QTableWidget#stationsTable QHeaderView::section:horizontal:last {
                border-right: none;
            }
            
            QTableWidget#stationsTable QHeaderView::section:horizontal:hover {
                background-color: #374560;
            }
            
            QTableWidget#stationsTable QScrollBar:vertical {
                background: #18213b;
                width: 12px;
                border-radius: 6px;
                margin: 2px 0px;
            }
            
            QTableWidget#stationsTable QScrollBar::handle:vertical {
                background: #2677e6;
                min-height: 30px;
                border-radius: 5px;
            }
            
            QTableWidget#stationsTable QScrollBar::handle:vertical:hover {
                background: #4394e6;
            }
            
            QTableWidget#stationsTable QScrollBar::add-line:vertical,
            QTableWidget#stationsTable QScrollBar::sub-line:vertical {
                border: none;
                background: none;
                height: 0px;
            }
            
            QTableWidget#stationsTable QScrollBar::add-page:vertical,
            QTableWidget#stationsTable QScrollBar::sub-page:vertical {
                background: #273450;
                border-radius: 3px;
            }
            
            QTableWidget#stationsTable QScrollBar:horizontal {
                background: #18213b;
                height: 12px;
                border-radius: 6px;
                margin: 0px 2px;
            }
            
            QTableWidget#stationsTable QScrollBar::handle:horizontal {
                background: #2677e6;
                min-width: 30px;
                border-radius: 5px;
            }
            
            QTableWidget#stationsTable QScrollBar::handle:horizontal:hover {
                background: #4394e6;
            }
            
            QTableWidget#stationsTable QScrollBar::add-line:horizontal,
            QTableWidget#stationsTable QScrollBar::sub-line:horizontal {
                border: none;
                background: none;
                width: 0px;
            }
            
            QTableWidget#stationsTable QScrollBar::add-page:horizontal,
            QTableWidget#stationsTable QScrollBar::sub-page:horizontal {
                background: #273450;
                border-radius: 3px;
            }
            
            QTableWidget#stationsTable::item { 
                padding: 8px; 
                border-bottom: 1px solid #273450;
            }
            QTableWidget#stationsTable::item:selected {
                background-color: #2677e6;
                color: white;
            }
            
            QPushButton#refreshButton {
                background-color: #273450;
                border: 1px solid #2677e6;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 10px;
                color: #ffffff;
            }
            QPushButton#refreshButton:hover {
                background-color: #2677e6;
                border-color: #4394e6;
            }
        """
        
        self.setStyleSheet(style_sheet)
    
    # --- Настройки ---
    def get_config_path(self):
        """Получение пути к файлу конфигурации"""
        if sys.platform == "win32":
            config_dir = Path.home() / "AppData" / "Local" / "KseniaRadio"
        else:
            config_dir = Path.home() / ".config" / "kseniaradio"
        
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "settings.json"
    
    def save_settings(self):
        """Сохранение настроек"""
        try:
            settings = {
                'volume': self.current_volume,
                'window_geometry': {
                    'x': self.x(),
                    'y': self.y(),
                    'width': self.width(),
                    'height': self.height()
                },
                'window_maximized': self.isMaximized(),
                'screen_locked': self.screen_locked
            }
            
            config_file = self.get_config_path()
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")
    
    def load_settings(self):
        """Загрузка настроек"""
        config_file = self.get_config_path()
        
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                
                self.current_volume = settings.get('volume', 50)
                self.screen_locked = settings.get('screen_locked', False)
                
                if settings.get('window_maximized', False):
                    self.showMaximized()
                else:
                    geometry = settings.get('window_geometry')
                    if geometry:
                        self.setGeometry(
                            geometry['x'],
                            geometry['y'],
                            geometry['width'],
                            geometry['height']
                        )
                    
            except Exception as e:
                print(f"Ошибка загрузки настроек: {e}")
    
    def closeEvent(self, event):
        """Обработка закрытия окна"""
        self.save_settings()
        self.player.stop()
        event.accept()

# --- Запуск приложения ---
def main():
    """Точка входа в приложение"""
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    
    app = QApplication(sys.argv)
    app.setApplicationName("Ksenia Radio")
    app.setStyle("Fusion")
    
    player = KseniaRadioPlayer()
    player.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
