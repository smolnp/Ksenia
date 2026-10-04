# -*- coding: utf-8 -*-
"""Встроенный VLC-плеер в отдельном процессе.

GUI-процесс создаёт дочерний процесс с libvlc. Обмен — через
multiprocessing.Queue. Это гарантирует, что зависание VLC
не блокирует редактор.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue as _queue
import sys
import threading
import time
from contextlib import suppress
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QVBoxLayout,
    QPushButton, QSlider, QWidget)

from constants import (PLAYER_CMD_QUEUE_TIMEOUT, PLAYER_PROCESS_JOIN_TIMEOUT_SEC,
    PLAYER_PROCESS_TERMINATE_TIMEOUT_SEC, PLAYER_STATUS_POLL_MS,
    VLC_INSTANCE_USER_AGENT, VLC_PLAYER_DEFAULT_HEIGHT,
    VLC_PLAYER_DEFAULT_VOLUME, VLC_PLAYER_DEFAULT_WIDTH)
from dialogs import BaseDialog, _is_qobject_valid
from models import ChannelData
from paths import logger, warn_box

try:
    import vlc
    _HAS_VLC_MODULE = True
    _VLC_IMPORT_ERROR = ""
except ImportError as e:
    vlc = None
    _HAS_VLC_MODULE = False
    _VLC_IMPORT_ERROR = str(e)
except Exception as e:
    vlc = None
    _HAS_VLC_MODULE = False
    _VLC_IMPORT_ERROR = str(e)


# =====================================================================
# Публичные хелперы
# =====================================================================
def is_vlc_available() -> bool:
    return _HAS_VLC_MODULE


def get_vlc_error() -> str:
    if _HAS_VLC_MODULE:
        return ""
    if _VLC_IMPORT_ERROR:
        return f"Не удалось импортировать python-vlc: {_VLC_IMPORT_ERROR}"
    return "Модуль python-vlc не установлен."


# =====================================================================
# Дочерний процесс: работа с libvlc
# =====================================================================
def _vlc_process_main(cmd_queue: mp.Queue, status_queue: mp.Queue):
    """Главная функция дочернего процесса. Все вызовы VLC — здесь."""
    try:
        import vlc as _vlc
    except Exception as e:
        with suppress(Exception):
            status_queue.put_nowait(
                {'ready': False, 'error': f'vlc import: {e}'})
        return

    args = ["--quiet", "--video-on-top"]
    if sys.platform.startswith("linux"):
        args.append("--no-xlib")

    instance = None
    player = None
    try:
        instance = _vlc.Instance(*args)
        if instance is None:
            with suppress(Exception):
                status_queue.put_nowait(
                    {'ready': False, 'error': 'VLC Instance вернул None'})
            return
        instance.set_user_agent(VLC_INSTANCE_USER_AGENT,
                                VLC_INSTANCE_USER_AGENT)
        player = instance.media_player_new()
    except Exception as e:
        with suppress(Exception):
            status_queue.put_nowait(
                {'ready': False, 'error': f'vlc init: {e}'})
        return

    end_event = mp.Event()

    def _on_end(*_a):
        end_event.set()

    with suppress(Exception):
        player.event_manager().event_attach(
            _vlc.EventType.MediaPlayerEndReached, _on_end)

    state = {
        'state': 'idle',
        'size': '',
        'fps': '',
        'error': '',
        'current_audio': -1,
        'current_video': -1,
    }
    audio_tracks: List[Tuple[int, str]] = []
    video_tracks: List[Tuple[int, str]] = []

    def _collect_status() -> dict:
        return {
            'ready': True,
            'state': state['state'],
            'size': state['size'],
            'fps': state['fps'],
            'error': state['error'],
            'audio_tracks': list(audio_tracks),
            'video_tracks': list(video_tracks),
            'current_audio': state['current_audio'],
            'current_video': state['current_video'],
        }

    def _refresh_tracks():
        nonlocal audio_tracks, video_tracks
        try:
            desc = player.audio_get_track_description() or []
            audio_tracks = [
                (t[0], t[1].decode('utf-8', 'replace')
                 if isinstance(t[1], bytes) else str(t[1]))
                for t in desc
            ]
        except Exception:
            audio_tracks = []
        try:
            desc = player.video_get_track_description() or []
            video_tracks = [
                (t[0], t[1].decode('utf-8', 'replace')
                 if isinstance(t[1], bytes) else str(t[1]))
                for t in desc
            ]
        except Exception:
            video_tracks = []

    def _refresh_size_fps():
        with suppress(Exception):
            size = player.video_get_size(0)
            if size and size[0] and size[1]:
                state['size'] = f"{size[0]}x{size[1]}"
            else:
                state['size'] = ''
        with suppress(Exception):
            fps = player.get_fps()
            if fps and fps > 0:
                state['fps'] = f"{fps:.0f}"
            else:
                state['fps'] = ''

    def _apply_play(msg: dict):
        url = msg.get('url', '')
        ua = msg.get('user_agent', '') or ''
        extra = msg.get('extra_headers') or {}
        if not url:
            state['error'] = 'Пустой URL'
            state['state'] = 'error'
            return
        with suppress(Exception):
            player.set_media(None)
        try:
            media = instance.media_new(url)
            for opt in (":network-caching=800",
                        ":live-caching=800",
                        ":http-reconnect=true",
                        ":ipv4-timeout=5000"):
                with suppress(Exception):
                    media.add_option(opt)
            if ua:
                media.add_option(f":http-user-agent={ua}")
            for k, v in extra.items():
                if k.lower() == 'user-agent':
                    continue
                if k.lower() == 'referer':
                    media.add_option(f":http-referrer={v}")
                else:
                    media.add_option(f":http-header={k}: {v}")
            player.set_media(media)
            player.play()
            state['state'] = 'playing'
            state['error'] = ''
        except Exception as e:
            state['error'] = str(e)
            state['state'] = 'error'

    last_publish = 0.0
    poll_interval = max(0.05, PLAYER_STATUS_POLL_MS / 1000.0) * 2
    while True:
        if end_event.is_set():
            end_event.clear()
            state['state'] = 'ended'

        try:
            msg = cmd_queue.get(timeout=poll_interval)
        except _queue.Empty:
            msg = None
        except (EOFError, OSError):
            break

        if msg is not None:
            cmd = msg.get('cmd')
            try:
                if cmd == 'play':
                    _apply_play(msg)
                    _refresh_tracks()
                elif cmd == 'pause':
                    with suppress(Exception):
                        player.pause()
                    state['state'] = (
                        'paused' if player.is_playing() else 'playing')
                elif cmd == 'stop':
                    with suppress(Exception):
                        player.stop()
                    with suppress(Exception):
                        player.set_media(None)
                    state['state'] = 'stopped'
                elif cmd == 'volume':
                    with suppress(Exception):
                        player.audio_set_volume(int(msg.get('value', 100)))
                elif cmd == 'aspect':
                    with suppress(Exception):
                        player.video_set_aspect_ratio(
                            msg.get('value') or None)
                elif cmd == 'audio':
                    with suppress(Exception):
                        player.audio_set_track(int(msg.get('value', -1)))
                        state['current_audio'] = int(msg.get('value', -1))
                elif cmd == 'video':
                    with suppress(Exception):
                        player.video_set_track(int(msg.get('value', -1)))
                        state['current_video'] = int(msg.get('value', -1))
                elif cmd == 'snapshot':
                    with suppress(Exception):
                        player.video_take_snapshot(
                            0, msg.get('path', ''), 0, 0)
                elif cmd == 'release':
                    break
            except Exception as e:
                state['error'] = f'cmd {cmd}: {e}'

        now = time.time()
        if now - last_publish >= 0.5:
            last_publish = now
            _refresh_size_fps()
            with suppress(Exception):
                state['current_audio'] = player.audio_get_track()
            with suppress(Exception):
                state['current_video'] = player.video_get_track()
            with suppress(Exception):
                if state['state'] not in ('error', 'ended', 'stopped'):
                    if player.is_playing() and state['state'] != 'paused':
                        state['state'] = 'playing'
            with suppress(Exception):
                status_queue.put_nowait(_collect_status())

    with suppress(Exception):
        player.stop()
    with suppress(Exception):
        player.release()
    with suppress(Exception):
        instance.release()
    with suppress(Exception):
        status_queue.put_nowait({'ready': False, 'state': 'stopped'})


# =====================================================================
# GUI-обёртка над дочерним процессом
# =====================================================================
class RemoteVlcPlayer(QWidget):
    """Виджет-заглушка. Реальное видео — в отдельном окне VLC."""

    playback_error = pyqtSignal(str)
    end_reached = pyqtSignal()
    status_updated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setAutoFillBackground(True)
        self.setStyleSheet("background-color: black;")

        self._ctx = mp.get_context('spawn')
        self._cmd_queue: Optional[mp.Queue] = None
        self._status_queue: Optional[mp.Queue] = None
        self._process: Optional[mp.Process] = None

        self._last_status: dict = {}
        self._current_url = ""
        self._current_user_agent = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QLabel(
            "🎬 VLC-плеер работает в отдельном процессе\n"
            "и показывает видео в собственном окне.\n\n"
            "Если окно плеера не видно — проверьте панель задач:\n"
            "оно могло быть свёрнуто или скрыто за другими окнами.\n\n"
            "Управляйте воспроизведением кнопками ниже —\n"
            "редактор остаётся полностью рабочим.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(
            "QLabel { color: #cccccc; font-size: 13px; "
            "padding: 20px; background-color: #101010; }")
        layout.addWidget(self.placeholder)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(max(100, int(PLAYER_STATUS_POLL_MS)))
        self._status_timer.timeout.connect(self._poll_status)

        self._start_process()

    def _start_process(self):
        if not _HAS_VLC_MODULE:
            self.placeholder.setText(
                "❌ python-vlc не установлен.\n\n" + get_vlc_error())
            return
        try:
            self._cmd_queue = self._ctx.Queue()
            self._status_queue = self._ctx.Queue()
            self._process = self._ctx.Process(
                target=_vlc_process_main,
                args=(self._cmd_queue, self._status_queue),
                daemon=True,
                name="KseniaVlcProcess")
            self._process.start()
            self._status_timer.start()
            logger.info(f"VLC-процесс запущен: pid={self._process.pid}")
        except Exception as e:
            logger.exception("Не удалось запустить VLC-процесс")
            self.placeholder.setText(f"❌ Ошибка запуска VLC: {e}")

    def _poll_status(self):
        if self._status_queue is None:
            return
        drained = 0
        try:
            while drained < 20:
                try:
                    st = self._status_queue.get_nowait()
                except _queue.Empty:
                    break
                drained += 1
                self._last_status = st
                if st.get('ready') is False and 'error' in st:
                    self.playback_error.emit(st['error'])
                elif st.get('state') == 'ended':
                    self.end_reached.emit()
                self.status_updated.emit(st)
        except Exception:
            logger.exception("VLC status poll")
        if self._process is not None and not self._process.is_alive():
            self._status_timer.stop()

    def is_vlc_ready(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _send(self, msg: dict):
        if self._cmd_queue is None:
            return
        try:
            self._cmd_queue.put_nowait(msg)
        except Exception as e:
            logger.debug(f"VLC cmd error: {e}")

    def play_url(self, url: str, user_agent: str = "",
                 extra_headers: Optional[Dict[str, str]] = None):
        self._current_url = url
        self._current_user_agent = user_agent or ""
        self._send({
            'cmd': 'play',
            'url': url,
            'user_agent': user_agent or '',
            'extra_headers': dict(extra_headers or {}),
        })

    def pause(self):
        self._send({'cmd': 'pause'})

    def stop(self):
        self._send({'cmd': 'stop'})

    def set_volume(self, volume: int):
        self._send({'cmd': 'volume', 'value': int(volume)})

    def set_aspect_ratio(self, ratio: str):
        self._send({'cmd': 'aspect', 'value': ratio or ''})

    def set_audio_track(self, track_id: int):
        self._send({'cmd': 'audio', 'value': int(track_id)})

    def set_video_track(self, track_id: int):
        self._send({'cmd': 'video', 'value': int(track_id)})

    def take_snapshot(self, path: str) -> bool:
        self._send({'cmd': 'snapshot', 'path': path})
        return True

    def get_video_info_text(self) -> str:
        st = self._last_status
        parts: List[str] = []
        size = st.get('size') or ''
        if size:
            parts.append(size.replace('x', '×'))
        fps = st.get('fps') or ''
        if fps:
            parts.append(f"{fps} fps")
        return "  ·  ".join(parts)

    def get_last_status(self) -> dict:
        return dict(self._last_status)

    def release(self):
        """Корректно завершить дочерний процесс."""
        self._status_timer.stop()
        if self._cmd_queue is not None:
            with suppress(Exception):
                self._cmd_queue.put_nowait({'cmd': 'release'})
        if self._process is not None:
            self._process.join(timeout=PLAYER_PROCESS_JOIN_TIMEOUT_SEC)
            if self._process.is_alive():
                logger.warning("VLC-процесс не завершился, terminate()")
                with suppress(Exception):
                    self._process.terminate()
                self._process.join(
                    timeout=PLAYER_PROCESS_TERMINATE_TIMEOUT_SEC)
            self._process = None
        for q in (self._cmd_queue, self._status_queue):
            if q is not None:
                with suppress(Exception):
                    q.close()
                    q.join_thread()
        self._cmd_queue = None
        self._status_queue = None

    def closeEvent(self, event):
        self.release()
        super().closeEvent(event)


# =====================================================================
# Диалог плеера
# =====================================================================
class EmbeddedPlayerDialog(BaseDialog):
    """Ksenia Player — управление VLC, работающим в отдельном процессе."""

    def __init__(self, channel: ChannelData, parent=None,
                 playlist: Optional[List[ChannelData]] = None):
        super().__init__(
            f"Ksenia Player — {channel.meta.name}",
            parent, size=(VLC_PLAYER_DEFAULT_WIDTH,
                          VLC_PLAYER_DEFAULT_HEIGHT))

        from ksenia_window import ApplicationCore
        self.core = ApplicationCore.instance()

        src = playlist if playlist else [channel]
        self.playlist = [ch for ch in src if ch.has_valid_url] or [channel]
        self.index = next(
            (i for i, ch in enumerate(self.playlist)
             if ch.uid == channel.uid), 0)
        self.channel = self.playlist[self.index]

        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(640, 480)

        self.player = RemoteVlcPlayer(self)
        self.root.addWidget(self.player, 1)

        controls = QHBoxLayout()
        self.prev_btn = QPushButton("⏮ Пред.")
        self.prev_btn.clicked.connect(self.play_previous)
        controls.addWidget(self.prev_btn)

        self.play_btn = QPushButton("⏸ Пауза")
        self.play_btn.clicked.connect(self._toggle_pause)
        controls.addWidget(self.play_btn)

        self.next_btn = QPushButton("След. ⏭")
        self.next_btn.clicked.connect(self.play_next)
        controls.addWidget(self.next_btn)

        controls.addSpacing(12)
        controls.addWidget(QLabel("🔊"))

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        saved_vol = int(self.core.config.get('vlc_volume',
                                              VLC_PLAYER_DEFAULT_VOLUME))
        self.volume_slider.setValue(saved_vol)
        self.volume_slider.setFixedWidth(140)
        self.volume_slider.valueChanged.connect(self._on_volume)
        controls.addWidget(self.volume_slider)

        controls.addSpacing(12)
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["По умолчанию", "16:9", "4:3", "1:1"])
        self.aspect_combo.currentTextChanged.connect(self._on_aspect)
        controls.addWidget(self.aspect_combo)

        controls.addSpacing(12)
        self.video_info_label = QLabel("")
        self.video_info_label.setMinimumWidth(140)
        self.video_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_info_label.setStyleSheet(
            "QLabel {"
            "  color: #000000;"
            "  background-color: #e0e0e0;"
            "  padding: 3px 10px;"
            "  border-radius: 4px;"
            "  font-family: 'Consolas', 'Menlo', monospace;"
            "  font-size: 12px;"
            "}")
        self.video_info_label.setToolTip("Разрешение и частота кадров")
        controls.addWidget(self.video_info_label)

        controls.addStretch()
        self.root.addLayout(controls)

        tracks = QHBoxLayout()
        self.audio_combo = QComboBox()
        self.audio_combo.addItem("Аудио: по умолчанию", -1)
        self.audio_combo.currentIndexChanged.connect(self._on_audio)
        tracks.addWidget(self.audio_combo)

        self.video_combo = QComboBox()
        self.video_combo.addItem("Видео: по умолчанию", -1)
        self.video_combo.currentIndexChanged.connect(self._on_video)
        tracks.addWidget(self.video_combo)
        self.root.addLayout(tracks)

        self.add_close()

        self.player.playback_error.connect(self._on_error)
        self.player.end_reached.connect(self._on_end)
        self.player.status_updated.connect(self._on_status)

        self._shortcuts: List[QShortcut] = []
        for keys, slot in (
            ("Ctrl+Right", self.play_next),
            ("Ctrl+Left", self.play_previous),
            ("A", lambda: self.aspect_combo.setCurrentText("16:9")),
            ("Z", lambda: self.aspect_combo.setCurrentText("По умолчанию")),
            ("Space", self._toggle_pause),
        ):
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

        self.player.set_volume(saved_vol)
        self._play_at(self.index)

    # --- Плейлист ---
    def _play_at(self, index: int):
        if not _is_qobject_valid(self):
            return
        if not (0 <= index < len(self.playlist)):
            return
        self.index = index
        self.channel = self.playlist[index]
        self.setWindowTitle(f"Ksenia Player — {self.channel.meta.name}")
        ua = self.channel.link.user_agent or ""
        extra = (dict(self.channel.link.extra_headers)
                 if self.channel.link.extra_headers else None)
        self.player.play_url(self.channel.link.url, ua, extra)
        self._update_nav()

    def play_next(self):
        if self.index < len(self.playlist) - 1:
            self._play_at(self.index + 1)

    def play_previous(self):
        if self.index > 0:
            self._play_at(self.index - 1)

    def _update_nav(self):
        if not _is_qobject_valid(self):
            return
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < len(self.playlist) - 1)

    def _on_end(self):
        if not _is_qobject_valid(self):
            return
        if self.index < len(self.playlist) - 1:
            self.play_next()

    # --- Управление ---
    def _toggle_pause(self):
        if _is_qobject_valid(self):
            self.player.pause()

    def _on_volume(self, v):
        if not _is_qobject_valid(self):
            return
        self.player.set_volume(v)
        self.core.config.set('vlc_volume', v)

    def _on_aspect(self, text):
        if not _is_qobject_valid(self):
            return
        self.player.set_aspect_ratio("" if text == "По умолчанию" else text)

    def _on_audio(self, idx):
        if not _is_qobject_valid(self) or idx <= 0:
            return
        tid = self.audio_combo.itemData(idx)
        if tid is not None and tid >= 0:
            self.player.set_audio_track(tid)

    def _on_video(self, idx):
        if not _is_qobject_valid(self) or idx <= 0:
            return
        tid = self.video_combo.itemData(idx)
        if tid is not None and tid >= 0:
            self.player.set_video_track(tid)

    # ФИКС #79: недостающий метод _on_status
    def _on_status(self, st: dict):
        """Обработка статуса от VLC-процесса."""
        if not _is_qobject_valid(self):
            return
        # Обновление разрешения/fps
        with suppress(Exception):
            self.video_info_label.setText(
                self.player.get_video_info_text())
        # Обновление списка аудио-треков
        audio_tracks = st.get('audio_tracks') or []
        if audio_tracks:
            self.audio_combo.blockSignals(True)
            current_data = self.audio_combo.currentData()
            self.audio_combo.clear()
            self.audio_combo.addItem("Аудио: по умолчанию", -1)
            for tid, name in audio_tracks:
                self.audio_combo.addItem(name or f"Track {tid}", tid)
            idx = self.audio_combo.findData(current_data)
            if idx >= 0:
                self.audio_combo.setCurrentIndex(idx)
            self.audio_combo.blockSignals(False)
        # Обновление списка видео-треков
        video_tracks = st.get('video_tracks') or []
        if video_tracks:
            self.video_combo.blockSignals(True)
            current_data = self.video_combo.currentData()
            self.video_combo.clear()
            self.video_combo.addItem("Видео: по умолчанию", -1)
            for tid, name in video_tracks:
                self.video_combo.addItem(name or f"Track {tid}", tid)
            idx = self.video_combo.findData(current_data)
            if idx >= 0:
                self.video_combo.setCurrentIndex(idx)
            self.video_combo.blockSignals(False)

    def _on_error(self, msg: str):
        if not _is_qobject_valid(self):
            return
        logger.warning(f"VLC player error: {msg}")

    # ФИКС #34: освобождаем VLC-процесс при закрытии диалога
    def closeEvent(self, event):
        try:
            if self.player is not None:
                with suppress(Exception):
                    self.player.release()
        finally:
            super().closeEvent(event)

    def reject(self):
        # Также освобождаем при reject() (закрытие по Esc / кнопке)
        try:
            if self.player is not None:
                with suppress(Exception):
                    self.player.release()
        finally:
            super().reject()