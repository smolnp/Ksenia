# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Встроенный плеер на mpv через QProcess --wid + JSON IPC."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from contextlib import suppress
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import (
    Qt, QProcess, QTimer, pyqtSignal)
from PyQt6.QtGui import (
    QFontDatabase, QKeySequence, QPalette, QShortcut)
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSlider, QVBoxLayout, QWidget)

from constants import (
    PLAYER_STATUS_POLL_MS, PLAYER_DEFAULT_VOLUME,
    PLAYER_IPC_CONNECT_TIMEOUT_MS, StatusText)
from dialogs_base import BaseDialog, _is_qobject_valid
from logging_setup import logger
from models import ChannelData, LinkQuality
from theme import hint_hex
from utils import URLUtils


def is_mpv_available() -> bool:
    return shutil.which("mpv") is not None


def get_mpv_error() -> str:
    if is_mpv_available():
        return ""
    return (
        "mpv не установлен.\n\n"
        "Установите его через менеджер пакетов:\n"
        "  sudo apt install mpv      (Debian/Ubuntu)\n"
        "  sudo dnf install mpv      (Fedora)\n"
        "  sudo pacman -S mpv        (Arch)")


class MpvPlayerWidget(QFrame):
    playback_error = pyqtSignal(str)
    end_reached = pyqtSignal()
    status_updated = pyqtSignal(dict)
    channel_status_changed = pyqtSignal(int, bool)
    tracks_updated = pyqtSignal(list, list, int, int)

    def __init__(self, parent=None, *,
                 hwdec: str = "auto",
                 adaptive_logic: str = "highest",
                 network_caching_ms: int = 1500,
                 live_caching_ms: int = 1500):
        super().__init__(parent)
        self.setMinimumSize(320, 240)

        self._surface = QWidget(self)
        self._surface.setMinimumSize(320, 240)
        self._surface.setAttribute(
            Qt.WidgetAttribute.WA_NativeWindow, True)
        self._surface.setAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._surface.setAttribute(
            Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._surface.setAutoFillBackground(True)
        pal = self._surface.palette()
        pal.setColor(QPalette.ColorRole.Window,
                     Qt.GlobalColor.black)
        self._surface.setPalette(pal)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._surface)

        self._error_overlay = QLabel(self)
        self._error_overlay.setWordWrap(True)
        self._error_overlay.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._error_overlay.setTextFormat(
            Qt.TextFormat.RichText)
        self._error_overlay.setStyleSheet(
            "QLabel {"
            " background-color: rgba(30, 30, 30, 230);"
            " color: #ff8c8c;"
            " font-size: 13px;"
            " padding: 18px 24px;"
            " border: 1px solid rgba(255, 107, 107, 130);"
            " border-radius: 8px;"
            "}")
        self._error_overlay.hide()
        self._error_overlay.raise_()
        self._current_error_text: str = ""

        self._ipc_path: Optional[str] = None
        self._ipc: Optional[QLocalSocket] = None
        self._ipc_buf = bytearray()
        self._ipc_req_id = 0
        self._ipc_callbacks: Dict[int, Any] = {}

        self._pending_loadfile: Optional[
            Tuple[str, str, Dict[str, str]]] = None

        self._proc: Optional[QProcess] = None
        self._last_status: dict = {}
        self._current_url = ""
        self._released = False
        self._release_lock = threading.RLock()
        self._aspect_user_choice: Optional[str] = None
        self._paused = False
        self._volume: int = PLAYER_DEFAULT_VOLUME
        self._pending_volume: Optional[int] = None

        self._video_w = 0
        self._video_h = 0
        self._fps = 0.0
        self._cache_speed = 0.0
        self._have_video = False

        self._audio_tracks: List[Dict[str, Any]] = []
        self._video_tracks: List[Dict[str, Any]] = []
        self._current_aid: int = -1
        self._current_vid: int = -1

        self._pending_uid: int = -1
        self._last_loaded_uid: int = -1

        self._pref_audio_lang: str = ""
        self._pref_video_lang: str = ""

        self._hwdec = hwdec
        self._adaptive_logic = adaptive_logic
        self._network_caching_ms = max(
            200, int(network_caching_ms))
        self._live_caching_ms = max(200, int(live_caching_ms))

        self._started = False

    def play_url(self, url: str, user_agent: str = "",
                 extra_headers: Optional[Dict[str, str]] = None,
                 uid: int = -1,
                 pref_audio_lang: str = "",
                 pref_video_lang: str = ""):
        with self._release_lock:
            if self._released:
                return

        if not url:
            self._show_error_overlay(
                "<b>Пустой URL</b><br>"
                "У канала не задана ссылка.<br><br>"
                "Добавьте ссылку через контекстное меню "
                "или «🔁 Альтернативы».")
            if uid >= 0:
                self.channel_status_changed.emit(uid, False)
            self.playback_error.emit("Пустой URL")
            return

        if not is_mpv_available():
            self._show_error_overlay(
                "<b>mpv не установлен</b><br>"
                "Установите mpv через менеджер пакетов.<br>"
                "<span style='color:#cccccc;'>"
                "sudo apt install mpv</span>")
            self.playback_error.emit(get_mpv_error())
            return

        if self._started and (
                self._proc is None
                or self._proc.state()
                == QProcess.ProcessState.NotRunning):
            logger.warning("mpv process died, restarting")
            self._started = False
            self._proc = None
            self._cleanup_ipc_path()

        self._current_url = url
        self._video_w = 0
        self._video_h = 0
        self._fps = 0.0
        self._cache_speed = 0.0
        self._have_video = False
        self._last_status = {}
        self._paused = False
        self._audio_tracks = []
        self._video_tracks = []
        self._current_aid = -1
        self._current_vid = -1
        self._pref_audio_lang = pref_audio_lang or ""
        self._pref_video_lang = pref_video_lang or ""

        self._hide_error_overlay()

        if not self._started:
            if not self._start_mpv():
                return
        self._pending_uid = uid
        try:
            self._loadfile(url, user_agent, extra_headers or {})
        except Exception:
            logger.exception("play_url: _loadfile failed")
            self._pending_uid = -1

    def pause(self):
        if self._released or not self._started:
            return
        self._send_ipc({"command": ["cycle", "pause"]})

    def stop(self):
        if self._released or not self._started:
            return
        self._pending_loadfile = None
        self._send_ipc({"command": ["stop"]})
        self._current_url = ""
        self._pending_uid = -1
        self._last_loaded_uid = -1

    def is_playing(self) -> bool:
        if self._released or not self._started:
            return False
        return not self._paused

    def set_volume(self, v: int):
        self._volume = max(0, min(200, int(v)))
        if self._released or not self._started:
            return
        if (self._ipc is None
                or self._ipc.state()
                != QLocalSocket.LocalSocketState.ConnectedState):
            self._pending_volume = self._volume
            return
        self._send_ipc(
            {"command": ["set_property", "volume", self._volume]})

    def set_aspect_ratio(self, ratio: str):
        if self._released or not self._started:
            return
        self._aspect_user_choice = ratio
        value = "-1"
        if ratio == "16:9":
            value = "16:9"
        elif ratio == "4:3":
            value = "4:3"
        elif ratio == "1:1":
            value = "1:1"
        self._send_ipc({
            "command": ["set_property",
                        "video-aspect-override", value]})

    def reset_aspect(self):
        self._aspect_user_choice = None
        if self._released or not self._started:
            return
        self._send_ipc({
            "command": ["set_property",
                        "video-aspect-override", "-1"]})

    def set_audio_track_by_id(self, tid: int):
        if self._released or not self._started:
            return
        self._send_ipc(
            {"command": ["set_property", "aid", int(tid)]})

    def set_video_track_by_id(self, tid: int):
        if self._released or not self._started:
            return
        self._send_ipc(
            {"command": ["set_property", "vid", int(tid)]})

    def apply_preferred_tracks(self, pref_audio_lang: str = "",
                                pref_video_lang: str = ""):
        if pref_audio_lang:
            low = pref_audio_lang.lower()
            for t in self._audio_tracks:
                if (t.get("lang") or "").lower() == low:
                    self.set_audio_track_by_id(t["id"])
                    break
        if pref_video_lang:
            low = pref_video_lang.lower()
            for t in self._video_tracks:
                if (t.get("lang") or "").lower() == low:
                    self.set_video_track_by_id(t["id"])
                    break

    def take_snapshot(self, path: str) -> bool:
        if self._released or not self._started:
            return False
        self._send_ipc({
            "command": ["screenshot-to-file", path, "video"]})
        return True

    def get_video_info_text(self) -> str:
        parts: List[str] = []
        if self._video_w > 0 and self._video_h > 0:
            parts.append(f"{self._video_w}×{self._video_h}")
        if self._fps > 0:
            parts.append(f"{self._fps:.2f} fps")
        if self._cache_speed > 0:
            kb = self._cache_speed / 1024.0
            if kb >= 1024:
                parts.append(f"{kb / 1024:.1f} MB/s")
            else:
                parts.append(f"{kb:.0f} KB/s")
        return "  ·  ".join(parts)

    def get_audio_tracks(self) -> List[Dict[str, Any]]:
        return list(self._audio_tracks)

    def get_video_tracks(self) -> List[Dict[str, Any]]:
        return list(self._video_tracks)

    def get_current_aid(self) -> int:
        return self._current_aid

    def get_current_vid(self) -> int:
        return self._current_vid

    def get_last_status(self) -> dict:
        return dict(self._last_status)

    def release(self):
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._pending_loadfile = None
        if self._started:
            self._send_ipc({"command": ["quit"]})
        with suppress(Exception):
            if self._ipc is not None:
                self._ipc.disconnectFromServer()
        self._ipc = None
        if self._proc is not None:
            try:
                if not self._proc.waitForFinished(2000):
                    self._proc.kill()
                    self._proc.waitForFinished(500)
            except Exception:
                logger.exception("mpv terminate")
        self._proc = None
        self._started = False
        self._cleanup_ipc_path()

    def closeEvent(self, event):
        self.release()
        super().closeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if (self._error_overlay is not None
                and self._error_overlay.isVisible()):
            self._reposition_error_overlay()

    @staticmethod
    def _make_ipc_path(wid: int) -> str:
        name = f"ksenia-mpv-{os.getpid()}-{wid}.sock"
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and os.path.isdir(runtime):
            path = os.path.join(runtime, name)
            with suppress(OSError):
                if os.path.exists(path):
                    os.unlink(path)
            return path
        cache = os.path.join(
            os.path.expanduser("~"), ".cache", "ksenia")
        with suppress(OSError):
            os.makedirs(cache, exist_ok=True)
        path = os.path.join(cache, name)
        with suppress(OSError):
            if os.path.exists(path):
                os.unlink(path)
        return path

    def _start_mpv(self) -> bool:
        mpv_path = shutil.which("mpv")
        if not mpv_path:
            self._show_error_overlay(
                "<b>mpv не установлен</b><br>"
                "Установите mpv через менеджер пакетов.")
            self.playback_error.emit(get_mpv_error())
            return False

        if sys.platform.startswith("linux"):
            wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
            qpa = (os.environ.get("QT_QPA_PLATFORM") or "").lower()
            using_xcb = qpa in ("xcb", "x11") or (
                not qpa and not wayland)
            if wayland and not using_xcb:
                msg = (
                    "Встроенный плеер требует X11 "
                    "(или XWayland).<br><br>"
                    "Обнаружен Wayland. Запустите приложение "
                    "так:<br>"
                    "<span style='color:#cccccc;'>"
                    "QT_QPA_PLATFORM=xcb python3 ksenia.py"
                    "</span>")
                self._show_error_overlay(msg)
                self.playback_error.emit(msg)
                return False

        try:
            wid = int(self._surface.winId())
        except Exception:
            logger.exception("winId() for mpv --wid")
            self._show_error_overlay(
                "<b>Не удалось получить window id</b>")
            self.playback_error.emit(
                "Не удалось получить window id")
            return False

        if sys.platform.startswith("win"):
            self._ipc_path = (
                rf"\\.\pipe\ksenia_mpv_{os.getpid()}_{wid}")
        else:
            self._ipc_path = self._make_ipc_path(wid)

        hwdec_value = self._hwdec or "auto-safe"
        args: List[str] = [
            "--no-config",
            "--idle=yes",
            "--force-window=yes",
            f"--wid={wid}",
            f"--input-ipc-server={self._ipc_path}",
            "--osc=no",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--keep-open=no",
            f"--hwdec={hwdec_value}",
            f"--cache-secs="
            f"{max(3, self._network_caching_ms // 1000)}",
            "--demuxer-readahead-secs=3",
            "--demuxer-max-bytes=128M",
            "--demuxer-max-back-bytes=16M",
            "--network-timeout=15",
            "--msg-level=all=warn",
        ]

        proc = QProcess(self)
        proc.setProgram(mpv_path)
        proc.setArguments(args)
        proc.setProcessChannelMode(
            QProcess.ProcessChannelMode.SeparateChannels)

        proc.start()
        if not proc.waitForStarted(3000):
            self._show_error_overlay(
                "<b>mpv не запустился</b><br>"
                "Проверьте установку mpv.")
            self.playback_error.emit("mpv не запустился")
            return False
        self._proc = proc
        self._started = True

        QTimer.singleShot(1500, self._check_mpv_alive)
        self._connect_ipc_with_retry()
        return True

    def _check_mpv_alive(self):
        # FIX 8: если mpv упал — сбрасываем состояние, чтобы
        # следующий play_url() корректно перезапустил процесс.
        if self._proc is None:
            return
        state = self._proc.state()
        if state == QProcess.ProcessState.NotRunning:
            logger.error("mpv exited (state=NotRunning)")
            self._pending_loadfile = None
            self._pending_volume = None
            self._started = False
            self._proc = None
            self._hide_error_overlay()
            self._show_error_overlay(
                "<b>mpv завершился неожиданно</b><br>"
                "Плеер будет перезапущен при следующем "
                "воспроизведении.")

    def _connect_ipc_with_retry(self):
        if not self._ipc_path:
            return
        sock = QLocalSocket(self)
        sock.connected.connect(self._on_ipc_connected)
        sock.disconnected.connect(self._on_ipc_disconnected)
        sock.readyRead.connect(self._on_ipc_ready_read)
        sock.errorOccurred.connect(self._on_ipc_error)
        self._ipc = sock
        deadline = time.monotonic() + (
            PLAYER_IPC_CONNECT_TIMEOUT_MS / 1000.0)
        QTimer.singleShot(
            50, lambda: self._try_connect_ipc(deadline))

    def _try_connect_ipc(self, deadline: float):
        if self._released or self._ipc is None:
            return
        if (self._ipc.state()
                == QLocalSocket.LocalSocketState.ConnectedState):
            return
        if time.monotonic() > deadline:
            logger.warning("mpv IPC connect timeout")
            return
        self._ipc.connectToServer(self._ipc_path)
        if (self._ipc.state()
                != QLocalSocket.LocalSocketState.ConnectedState):
            QTimer.singleShot(
                100, lambda: self._try_connect_ipc(deadline))

    def _on_ipc_connected(self):
        for rid, prop in (
            (1, "width"), (2, "height"), (3, "pause"),
            (4, "eof-reached"), (5, "track-list"),
            (6, "video-params"), (7, "cache-speed"),
            (8, "demuxer-cache-state"), (9, "audio-params"),
        ):
            self._send_ipc(
                {"command": ["observe_property", rid, prop]})

        self._send_ipc(
            {"command": ["set_property", "volume", self._volume]})
        self._pending_volume = None

        if self._aspect_user_choice and self._aspect_user_choice != "auto":
            self.set_aspect_ratio(self._aspect_user_choice)

        pending = self._pending_loadfile
        if pending is not None:
            self._pending_loadfile = None
            url, ua, extra = pending
            try:
                self._loadfile(url, ua, extra)
            except Exception:
                logger.exception(
                    "_on_ipc_connected: pending loadfile")

    def _on_ipc_disconnected(self):
        pass

    def _on_ipc_error(self, err):
        logger.debug("mpv IPC error: %s", err)

    def _on_ipc_ready_read(self):
        if self._ipc is None:
            return
        self._ipc_buf.extend(bytes(self._ipc.readAll()))
        while True:
            nl = self._ipc_buf.find(b"\n")
            if nl < 0:
                return
            line = bytes(self._ipc_buf[:nl])
            del self._ipc_buf[:nl + 1]
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            self._handle_ipc_message(msg)

    def _handle_ipc_message(self, msg: Dict[str, Any]):
        ev = msg.get("event")
        if ev == "file-loaded":
            self._hide_error_overlay()
            if self._pending_uid >= 0:
                self.channel_status_changed.emit(
                    self._pending_uid, True)
                self._last_loaded_uid = self._pending_uid
                self._pending_uid = -1
            QTimer.singleShot(
                200, lambda: self.apply_preferred_tracks(
                    self._pref_audio_lang, self._pref_video_lang))
            return
        if ev == "property-change":
            self._on_property_change(msg)
            return
        if ev == "end-file":
            reason = msg.get("reason")
            if reason == "error":
                err = msg.get("file_error") or "mpv file error"
                self._handle_playback_error(str(err))
                if self._pending_uid >= 0:
                    self.channel_status_changed.emit(
                        self._pending_uid, False)
                    self._pending_uid = -1
                elif self._last_loaded_uid >= 0:
                    self.channel_status_changed.emit(
                        self._last_loaded_uid, False)
                    self._last_loaded_uid = -1
            elif reason == "eof":
                self.end_reached.emit()
            return
        rid = msg.get("request_id")
        if rid is not None and rid in self._ipc_callbacks:
            cb = self._ipc_callbacks.pop(rid)
            with suppress(Exception):
                cb(msg)

    @staticmethod
    def _humanize_mpv_error(err: str) -> str:
        low = (err or "").lower()
        if not low:
            return "Неизвестная ошибка воспроизведения"
        if "empty" in low or "пуст" in low:
            return "Пустой URL"
        if "loading failed" in low:
            return ("Поток недоступен — ссылка мертва или "
                    "сервер не отдаёт видео")
        if "unrecognized" in low or "format" in low:
            return ("Неверный формат потока — сервер вернул "
                    "не видео (например, HTML-ошибку 404/403)")
        if "timed out" in low or "timeout" in low:
            return "Таймаут — сервер не ответил вовремя"
        if "forbidden" in low or "403" in low:
            return "Доступ запрещён (403) — возможен геоблок"
        if "not found" in low or "404" in low:
            return "Поток не найден (404)"
        if "resolve" in low or "dns" in low:
            return "Не удалось разрешить домен (DNS)"
        if "connection" in low or "connect" in low:
            return "Ошибка соединения с сервером"
        if "protocol" in low:
            return "Неподдерживаемый протокол"
        return err.strip()[:200] or "Ошибка воспроизведения"

    def _handle_playback_error(self, err: str):
        human = self._humanize_mpv_error(err)
        try:
            status_text, _ = URLUtils.classify_status(
                self._current_url or "", None, error=err)
            if status_text and status_text not in (
                    StatusText.UNCHECKED, StatusText.NO_URL):
                clean = status_text.strip()
                if clean:
                    human = clean
        except Exception:
            logger.exception("classify_status in playback error")

        logger.warning("mpv error: %s (raw: %s)",
                       human, (err or "")[:200])
        self._show_error_overlay(
            f"<b>Ошибка воспроизведения</b><br>{human}")
        self.playback_error.emit(human)

    def _show_error_overlay(self, text: str):
        self._current_error_text = text
        if not _is_qobject_valid(self._error_overlay):
            return
        self._error_overlay.setText(text)
        self._error_overlay.adjustSize()
        self._reposition_error_overlay()
        self._error_overlay.show()
        self._error_overlay.raise_()

    def _hide_error_overlay(self):
        self._current_error_text = ""
        if _is_qobject_valid(self._error_overlay):
            self._error_overlay.hide()

    def _reposition_error_overlay(self):
        if not _is_qobject_valid(self._error_overlay):
            return
        margin = 24
        w = max(0, self.width() - 2 * margin)
        h = max(0, self.height() - 2 * margin)
        max_w = max(180, w)
        self._error_overlay.setMaximumWidth(max_w)
        self._error_overlay.adjustSize()
        ow = min(self._error_overlay.sizeHint().width(), max_w)
        oh = self._error_overlay.sizeHint().height()
        x = margin + max(0, (w - ow) // 2)
        y = margin + max(0, (h - oh) // 2)
        self._error_overlay.setGeometry(int(x), int(y),
                                         int(ow), int(oh))

    def _on_property_change(self, msg: Dict[str, Any]):
        name = msg.get("name")
        data = msg.get("data")
        if name == "width" and isinstance(data, int):
            self._video_w = int(data)
        elif name == "height" and isinstance(data, int):
            self._video_h = int(data)
        elif name == "pause":
            self._paused = bool(data)
        elif name == "eof-reached" and data:
            self.end_reached.emit()
            return
        elif name == "track-list" and isinstance(data, list):
            self._update_tracks(data)
            return
        elif name == "video-params" and isinstance(data, dict):
            self._fps = float(data.get("fps") or 0.0)
        elif name == "cache-speed":
            try:
                self._cache_speed = float(data or 0.0)
            except (TypeError, ValueError):
                self._cache_speed = 0.0
        else:
            return
        if self._video_w > 0 and self._video_h > 0:
            self._have_video = True
        st: dict = {
            "width": self._video_w,
            "height": self._video_h,
            "size": (f"{self._video_w}x{self._video_h}"
                     if self._have_video else ""),
            "paused": self._paused,
            "fps": self._fps,
            "cache_speed": self._cache_speed,
        }
        if st != self._last_status:
            self._last_status = st
            self.status_updated.emit(st)

    def _update_tracks(self, tracks: List[Dict[str, Any]]):
        audio: List[Dict[str, Any]] = []
        video: List[Dict[str, Any]] = []
        cur_aid = -1
        cur_vid = -1
        for t in tracks:
            ttype = t.get("type")
            if ttype == "audio":
                audio.append(t)
                if t.get("selected"):
                    cur_aid = int(t.get("id", -1))
            elif ttype == "video":
                if t.get("albumart"):
                    continue
                video.append(t)
                if t.get("selected"):
                    cur_vid = int(t.get("id", -1))
        self._audio_tracks = audio
        self._video_tracks = video
        self._current_aid = cur_aid
        self._current_vid = cur_vid
        self.tracks_updated.emit(
            list(audio), list(video), cur_aid, cur_vid)

    def _send_ipc(self, payload: Dict[str, Any]):
        if self._released or self._ipc is None:
            return
        if (self._ipc.state()
                != QLocalSocket.LocalSocketState.ConnectedState):
            return
        self._ipc_req_id += 1
        payload = dict(payload)
        payload["request_id"] = self._ipc_req_id
        data = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self._ipc.write(data)
            self._ipc.flush()
        except Exception:
            logger.exception("mpv IPC write")

    def _loadfile(self, url: str, user_agent: str,
                  extra_headers: Dict[str, str]):
        if self._ipc is None or (self._ipc.state()
                != QLocalSocket.LocalSocketState.ConnectedState):
            self._pending_loadfile = (
                url, user_agent, dict(extra_headers))
            logger.debug(
                "_loadfile: IPC not connected yet, "
                "queued pending loadfile for %s", url[:80])
            return
        self._pending_loadfile = None
        lines: List[str] = []
        if user_agent:
            lines.append(f"User-Agent: {user_agent}")
        for k, v in extra_headers.items():
            if k.lower() == "user-agent":
                continue
            lines.append(f"{k}: {v}")
        headers_str = "\r\n".join(lines) if lines else ""
        self._send_ipc({
            "command": ["set_property",
                        "http-header-fields", headers_str]})
        self._send_ipc({"command": ["loadfile", url, "replace"]})

    def _cleanup_ipc_path(self):
        if self._ipc_path and not sys.platform.startswith("win"):
            with suppress(OSError):
                if os.path.exists(self._ipc_path):
                    os.unlink(self._ipc_path)


def _apply_box_palette(label: QLabel):
    pal = label.palette()
    pal.setColor(QPalette.ColorRole.Window,
                 pal.color(QPalette.ColorRole.AlternateBase))
    pal.setColor(QPalette.ColorRole.WindowText,
                 pal.color(QPalette.ColorRole.Text))
    label.setPalette(pal)
    label.setAutoFillBackground(True)


def _track_label(t: Dict[str, Any], fallback_id: int) -> str:
    parts: List[str] = []
    lang = (t.get("lang") or "").strip()
    title = (t.get("title") or "").strip()
    codec = (t.get("codec") or "").strip()
    if lang:
        parts.append(lang)
    if title:
        parts.append(title)
    if codec:
        parts.append(codec)
    if not parts:
        return f"Дорожка {fallback_id}"
    return " · ".join(parts)


class EmbeddedPlayerDialog(BaseDialog):
    channel_changed = pyqtSignal(int)
    channel_status_changed = pyqtSignal(int, bool)
    channel_modified = pyqtSignal(int)

    def __init__(self, channel: ChannelData, parent=None,
                 playlist: Optional[List[ChannelData]] = None):
        super().__init__(
            f"Ksenia Player — {channel.meta.name}",
            parent, size=(1000, 720))

        from app_core import ApplicationCore
        self.core = ApplicationCore.instance()

        src = playlist if playlist else [channel]
        self.playlist = ([ch for ch in src if ch.has_valid_url]
                         or [channel])
        self.index = next(
            (i for i, ch in enumerate(self.playlist)
             if ch.uid == channel.uid), 0)
        self.channel = self.playlist[self.index]

        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(640, 480)

        self._player_released = False
        self._release_lock = threading.Lock()
        self._updating_tracks = False

        cfg = self.core.config
        self.player = MpvPlayerWidget(
            self,
            hwdec=str(cfg.get('player_hwdec', 'auto')),
            adaptive_logic=str(cfg.get(
                'player_adaptive_logic', 'highest')),
            network_caching_ms=int(cfg.get(
                'player_network_caching_ms', 1500)),
            live_caching_ms=int(cfg.get(
                'player_live_caching_ms', 1500)),
        )
        self.root.addWidget(self.player, 1)

        self.epg_label = QLabel("")
        self.epg_label.setWordWrap(True)
        self.epg_label.setTextFormat(Qt.TextFormat.RichText)
        _apply_box_palette(self.epg_label)
        self.epg_label.setVisible(
            bool(cfg.get('player_show_epg', True)))
        self.root.addWidget(self.epg_label)

        self._epg_timer = QTimer(self)
        self._epg_timer.setInterval(30000)
        self._epg_timer.timeout.connect(self._update_epg)
        self._epg_timer.start()

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
        saved_vol = int(self.core.config.get(
            'player_volume', PLAYER_DEFAULT_VOLUME))
        self.volume_slider.setValue(saved_vol)
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.valueChanged.connect(self._on_volume)
        controls.addWidget(self.volume_slider)

        controls.addSpacing(12)
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(
            ["Авто", "16:9", "4:3", "1:1"])
        saved_aspect = str(self.core.config.get(
            'player_aspect', 'auto'))
        _aspect_idx = {"auto": 0, "16:9": 1,
                       "4:3": 2, "1:1": 3}.get(saved_aspect, 0)
        self.aspect_combo.setCurrentIndex(_aspect_idx)
        self.aspect_combo.currentTextChanged.connect(
            self._on_aspect)
        controls.addWidget(self.aspect_combo)

        controls.addStretch()

        self.alts_btn = QPushButton("🔁 Альтернативы")
        self.alts_btn.setToolTip(
            "Показать альтернативы: сохранённые у канала и "
            "живые URL\n"
            "из кэша источников (наполняется кнопкой "
            "«🔄 Обновить всё»)")
        self.alts_btn.clicked.connect(self._show_alts_menu)
        controls.addWidget(self.alts_btn)

        self.remove_url_btn = QPushButton("🗑 Удалить ссылку")
        self.remove_url_btn.setToolTip(
            "Удалить текущую ссылку. Доступно только если "
            "ссылка\nне работает или у канала нет альтернатив.")
        self.remove_url_btn.clicked.connect(
            self._remove_current_url)
        controls.addWidget(self.remove_url_btn)

        self.block_domain_btn = QPushButton("🚫 Домен/IP")
        self.block_domain_btn.setToolTip(
            "Заблокировать домен/IP текущей ссылки")
        self.block_domain_btn.clicked.connect(self._block_domain)
        controls.addWidget(self.block_domain_btn)

        self.root.addLayout(controls)

        tracks_layout = QHBoxLayout()
        tracks_layout.addWidget(QLabel("🎧"))
        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setMinimumWidth(160)
        self.audio_track_combo.currentIndexChanged.connect(
            self._on_audio_track_changed)
        tracks_layout.addWidget(self.audio_track_combo)

        tracks_layout.addSpacing(12)
        tracks_layout.addWidget(QLabel("🎬"))
        self.video_track_combo = QComboBox()
        self.video_track_combo.setMinimumWidth(160)
        self.video_track_combo.currentIndexChanged.connect(
            self._on_video_track_changed)
        tracks_layout.addWidget(self.video_track_combo)

        tracks_layout.addSpacing(12)
        self.remember_tracks_btn = QPushButton("💾")
        self.remember_tracks_btn.setToolTip(
            "Сохранить выбранные дорожки для канала")
        self.remember_tracks_btn.clicked.connect(
            self._remember_tracks)
        tracks_layout.addWidget(self.remember_tracks_btn)

        tracks_layout.addStretch()

        self.video_info_label = QLabel("")
        self.video_info_label.setMinimumWidth(220)
        self.video_info_label.setAlignment(
            Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter)
        _apply_box_palette(self.video_info_label)
        mono = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont)
        pt = mono.pointSize()
        if pt > 0:
            mono.setPointSize(max(8, pt - 1))
        self.video_info_label.setFont(mono)
        tracks_layout.addWidget(self.video_info_label)

        self.root.addLayout(tracks_layout)

        self.add_close()

        self.player.playback_error.connect(self._on_error)
        self.player.end_reached.connect(self._on_end)
        self.player.status_updated.connect(self._on_status)
        self.player.channel_status_changed.connect(
            self._on_channel_status_changed)
        self.player.tracks_updated.connect(
            self._on_tracks_updated)

        self._shortcuts: List[QShortcut] = []
        for keys, slot in (
            ("Ctrl+Right", self.play_next),
            ("Ctrl+Left", self.play_previous),
            ("A", lambda: self.aspect_combo.setCurrentText(
                "16:9")),
            ("Z", lambda: self.aspect_combo.setCurrentText(
                "Авто")),
            ("Space", self._toggle_pause),
        ):
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

        self.player.set_volume(saved_vol)
        self._update_link_buttons()
        QTimer.singleShot(0, lambda: self._play_at(self.index))

    def _current_has_alts(self) -> bool:
        if self.channel is None:
            return False
        return bool([u for u in self.channel.link.alternative_urls
                     if u])

    def _current_url_broken(self) -> bool:
        if self.channel is None:
            return False
        return self.channel.status.url_status is False

    def _update_link_buttons(self):
        has_url = bool(self.channel and self.channel.has_valid_url)
        broken = self._current_url_broken()
        self.alts_btn.setEnabled(has_url)
        can_remove = has_url and (
            broken or not self._current_has_alts())
        self.remove_url_btn.setEnabled(can_remove)
        self.block_domain_btn.setEnabled(has_url)
        if can_remove:
            self.remove_url_btn.setStyleSheet(
                "QPushButton { color: #b00020; font-weight: bold; }")
        else:
            self.remove_url_btn.setStyleSheet("")

    def _on_channel_status_changed(self, uid: int, ok: bool):
        if uid == (self.channel.uid if self.channel else -1):
            if self.channel is not None:
                from datetime import datetime as _dt
                self.channel.status.url_status = bool(ok)
                self.channel.status.url_check_time = _dt.now()
                if ok:
                    self.channel.status.link_quality = (
                        LinkQuality.WORKING)
                    self.channel.status.status_text = (
                        StatusText.WORKING)
                else:
                    self.channel.status.link_quality = (
                        LinkQuality.NOT_WORKING)
                    self.channel.status.status_text = (
                        StatusText.NOT_WORKING)
                try:
                    self.channel.touch()
                except Exception:
                    logger.exception("channel.touch")
            self._update_link_buttons()
        self.channel_status_changed.emit(uid, ok)

    def _collect_alternative_urls(self) -> List[str]:
        if self.channel is None:
            return []
        current = (self.channel.link.url or "").strip()
        result: List[str] = []
        seen: set = set()

        def _add(u: str):
            u = (u or "").strip()
            if not u or u == current or u in seen:
                return
            seen.add(u)
            result.append(u)

        for u in self.channel.link.alternative_urls:
            _add(u)

        settings = None
        try:
            settings = self.core.get_replacement_settings()
        except Exception:
            logger.exception("get_replacement_settings")

        if settings is not None:
            try:
                limit = max(1, int(
                    settings.max_urls_to_check_per_channel))
                search_keys = self.channel.search_keys(
                    settings, epg_db=self.core.epg_db)
                alive = (
                    self.core.link_source_manager
                    .get_alive_urls_for_keys(
                        search_keys, settings, limit=limit))
                for u in alive:
                    _add(u)
            except Exception:
                logger.exception("get_alive_urls_for_keys from player")

            if not result:
                try:
                    matches = (self.core.link_source_manager
                               .find_matches_for_channel(
                                   self.channel, settings,
                                   config=self.core.config,
                                   only_working=False,
                                   epg_db=self.core.epg_db))
                    for ch in matches:
                        if (ch.link.url
                                and ch.status.url_status
                                is not False):
                            _add(ch.link.url)
                except Exception:
                    logger.exception(
                        "find_matches_for_channel in player")
        return result

    def _show_alts_menu(self):
        if not _is_qobject_valid(self) or self.channel is None:
            return
        alts = self._collect_alternative_urls()
        if not alts:
            from paths import info_box
            info_box(
                self,
                "Альтернатив не найдено.\n\n"
                "Проверьте:\n"
                "• есть ли включённые источники в "
                "«Инструменты → Источники ссылок»;\n"
                "• запускали ли вы «🔄 Обновить всё» после "
                "добавления источников;\n"
                "• совпадает ли имя канала с именем в "
                "источнике.\n\n"
                "Подробности — в логе "
                "(Инструменты → Показать лог).",
                "Плеер")
            return

        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        for i, u in enumerate(alts):
            a = menu.addAction(f"{i + 1}. {u[:80]}...")
            a.setToolTip(u)
            a.triggered.connect(
                lambda checked=False, uu=u:
                    self._apply_alt(uu))
        menu.addSeparator()
        clear = menu.addAction(
            "🧹 Очистить сохранённые альтернативы")
        clear.setEnabled(bool(
            self.channel.link.alternative_urls))
        clear.triggered.connect(self._clear_alts)
        menu.exec(self.alts_btn.mapToGlobal(
            self.alts_btn.rect().bottomLeft()))

    def _apply_alt(self, url: str):
        if not url or not url.strip():
            return
        if self.channel is None:
            return
        old_url = self.channel.link.url or ""
        if url == old_url:
            return
        from paths import confirm
        if not confirm(
                self,
                f"Переключиться на ссылку?\n\n{url[:120]}"):
            return
        if old_url and old_url not in \
                self.channel.link.alternative_urls:
            self.channel.link.alternative_urls.append(old_url)
        if url in self.channel.link.alternative_urls:
            self.channel.link.alternative_urls.remove(url)
        self.channel.link.url = url
        self.channel.link.has_url = True
        self.channel.status.reset()
        try:
            self.channel.update_extinf()
        except Exception:
            logger.exception("update_extinf")
        try:
            (self.core.domain_user_agent_manager
             .apply_rules_to_channel(self.channel))
        except Exception:
            logger.exception("apply_rules_to_channel")
        try:
            self.channel.update_extvlcopt_from_headers()
        except Exception:
            logger.exception("update_extvlcopt_from_headers")
        self._update_link_buttons()
        self.player.play_url(
            self.channel.link.url,
            self.channel.link.user_agent or "",
            dict(self.channel.link.extra_headers),
            uid=self.channel.uid,
            pref_audio_lang=(
                self.channel.meta.preferred_audio_lang),
            pref_video_lang=(
                self.channel.meta.preferred_video_lang))
        self.channel_modified.emit(self.channel.uid)

    def _clear_alts(self):
        if self.channel is None:
            return
        if not self.channel.link.alternative_urls:
            return
        from paths import confirm
        if not confirm(self,
                        "Очистить список альтернатив у канала?"):
            return
        self.channel.link.alternative_urls.clear()
        self._update_link_buttons()

    def _remove_current_url(self):
        if self.channel is None or not self.channel.has_valid_url:
            return
        if not self.remove_url_btn.isEnabled():
            return
        has_alts = self._current_has_alts()
        broken = self._current_url_broken()
        from paths import confirm, warn_box
        if has_alts and not broken:
            warn_box(
                self,
                "Ссылка работает, и у канала есть "
                "альтернативы.\n"
                "Используйте «🔁 Альтернативы», чтобы "
                "переключиться.",
                "Плеер")
            return
        reason = ("ссылка не работает" if broken
                  else "у канала нет альтернатив")
        if not confirm(
                self,
                f"Удалить текущую ссылку?\n\n"
                f"{self.channel.link.url[:120]}\n\n"
                f"Причина: {reason}.\n"
                f"Канал останется в плейлисте без ссылки."):
            return
        self.channel.link.url = ""
        self.channel.link.has_url = False
        self.channel.status.reset()
        try:
            self.channel.update_extinf()
        except Exception:
            logger.exception("update_extinf")
        try:
            self.channel.update_extvlcopt_from_headers()
        except Exception:
            logger.exception("update_extvlcopt_from_headers")
        self.player.stop()
        self._update_link_buttons()
        self.channel_modified.emit(self.channel.uid)

    def _block_domain(self):
        if self.channel is None or not self.channel.has_valid_url:
            return
        host = URLUtils.extract_host(self.channel.link.url)
        if not host:
            from paths import info_box
            info_box(self,
                     "Не удалось определить домен/IP из URL.")
            return
        from dialogs import BlockDomainDialog
        from paths import info_box
        try:
            from app_core import ApplicationCore
            core = ApplicationCore.instance()
            all_tabs = core.get_all_tabs()
        except Exception:
            all_tabs = []
        all_channels: List[ChannelData] = []
        for t in all_tabs:
            if _is_qobject_valid(t):
                all_channels.extend(t.all_channels)
        dlg = BlockDomainDialog(host, all_channels, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        value, include, note, selected = dlg.get_result()
        if not value:
            return
        core.add_domain_to_blacklist(
            value, include_subdomains=include, note=note)
        if selected:
            info_box(
                self,
                f"Правило «{value}» добавлено.\n"
                f"Совпадений найдено: {len(selected)}.\n"
                f"Ссылки будут очищены при следующей очистке.")
        else:
            info_box(
                self,
                f"Правило «{value}» добавлено. "
                f"Совпадений в плейлисте не найдено.")

    def _update_epg(self):
        if not _is_qobject_valid(self):
            return
        if not self.core.config.get('player_show_epg', True):
            self.epg_label.setVisible(False)
            return
        ch = self.channel
        if ch is None or not ch.meta.tvg_id:
            self.epg_label.setText(
                "<i>EPG: у канала нет TVG-ID</i>")
            self.epg_label.setVisible(True)
            return
        try:
            now = self.core.epg_db.get_current(ch.meta.tvg_id)
        except Exception:
            logger.exception("EPG get_current")
            now = None
        if not now:
            self.epg_label.setText(
                "<i>EPG: нет данных (загрузите через меню "
                "Инструменты → Загрузить EPG)</i>")
            self.epg_label.setVisible(True)
            return

        def _fmt(dt):
            return dt.strftime("%H:%M") if dt else "?"

        title = now.title or "(без названия)"
        rng = f"{_fmt(now.start)}–{_fmt(now.stop)}"
        self.epg_label.setText(
            f"<b>Сейчас:</b> {title} "
            f"<span style='color:{hint_hex()}'>({rng})</span>")
        self.epg_label.setVisible(True)

    def _play_at(self, index: int):
        if not _is_qobject_valid(self):
            return
        if self._player_released:
            return
        if not (0 <= index < len(self.playlist)):
            return
        self.index = index
        self.channel = self.playlist[index]
        self.setWindowTitle(
            f"Ksenia Player — {self.channel.meta.name}")
        ua = self.channel.link.user_agent or ""
        extra = (dict(self.channel.link.extra_headers)
                 if self.channel.link.extra_headers else None)
        self.player.reset_aspect()
        self.player.play_url(
            self.channel.link.url, ua, extra,
            uid=self.channel.uid,
            pref_audio_lang=(
                self.channel.meta.preferred_audio_lang),
            pref_video_lang=(
                self.channel.meta.preferred_video_lang))
        current = self.aspect_combo.currentText()
        if current != "Авто":
            self.player.set_aspect_ratio(current)
        self._update_nav()
        self._update_link_buttons()
        self._update_epg()
        self.channel_changed.emit(self.channel.uid)

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
        self.next_btn.setEnabled(
            self.index < len(self.playlist) - 1)

    def _on_end(self):
        if not _is_qobject_valid(self):
            return
        if self.index < len(self.playlist) - 1:
            self.play_next()

    def _toggle_pause(self):
        if not _is_qobject_valid(self):
            return
        self.player.pause()

    def _update_play_button(self):
        if not _is_qobject_valid(self):
            return
        if self.player.is_playing():
            self.play_btn.setText("⏸ Пауза")
        else:
            self.play_btn.setText("▶ Играть")

    def _on_volume(self, v):
        if not _is_qobject_valid(self):
            return
        self.player.set_volume(v)
        self.core.config.set('player_volume', v)

    def _on_aspect(self, text):
        if not _is_qobject_valid(self):
            return
        if text == "Авто":
            self.player.set_aspect_ratio("auto")
            self.core.config.set('player_aspect', 'auto')
        else:
            self.player.set_aspect_ratio(text)
            self.core.config.set('player_aspect', text)

    def _on_tracks_updated(self, audio: list, video: list,
                            cur_aid: int, cur_vid: int):
        if not _is_qobject_valid(self):
            return
        self._updating_tracks = True
        try:
            self.audio_track_combo.clear()
            self.audio_track_combo.addItem("Авто", -1)
            for t in audio:
                tid = int(t.get("id", -1))
                self.audio_track_combo.addItem(
                    _track_label(t, tid), tid)
            idx = self.audio_track_combo.findData(cur_aid)
            if idx < 0:
                idx = 0
            self.audio_track_combo.setCurrentIndex(idx)
            self.video_track_combo.clear()
            self.video_track_combo.addItem("Авто", -1)
            for t in video:
                tid = int(t.get("id", -1))
                self.video_track_combo.addItem(
                    _track_label(t, tid), tid)
            idx = self.video_track_combo.findData(cur_vid)
            if idx < 0:
                idx = 0
            self.video_track_combo.setCurrentIndex(idx)
        finally:
            self._updating_tracks = False

    def _on_audio_track_changed(self, idx: int):
        if self._updating_tracks:
            return
        tid = self.audio_track_combo.itemData(idx)
        if tid is None or tid < 0:
            return
        self.player.set_audio_track_by_id(int(tid))

    def _on_video_track_changed(self, idx: int):
        if self._updating_tracks:
            return
        tid = self.video_track_combo.itemData(idx)
        if tid is None or tid < 0:
            return
        self.player.set_video_track_by_id(int(tid))

    def _remember_tracks(self):
        if not _is_qobject_valid(self) or self.channel is None:
            return
        aid = self.player.get_current_aid()
        vid = self.player.get_current_vid()
        audio_lang = ""
        for t in self.player.get_audio_tracks():
            if int(t.get("id", -1)) == aid:
                audio_lang = (t.get("lang") or "").strip()
                break
        video_lang = ""
        for t in self.player.get_video_tracks():
            if int(t.get("id", -1)) == vid:
                video_lang = (t.get("lang") or "").strip()
                break
        self.channel.meta.preferred_audio_lang = audio_lang
        self.channel.meta.preferred_video_lang = video_lang
        try:
            self.channel.touch()
        except Exception:
            from datetime import datetime as _dt
            self.channel.modified_date = _dt.now()
        try:
            self.channel_modified.emit(self.channel.uid)
        except Exception:
            logger.exception("channel_modified emit")
        from paths import info_box
        info_box(
            self,
            "Дорожки сохранены для канала:\n"
            f"аудио: {audio_lang or '—'}\n"
            f"видео: {video_lang or '—'}",
            "Плеер")

    def _on_status(self, st: dict):
        if not _is_qobject_valid(self):
            return
        with suppress(Exception):
            self.video_info_label.setText(
                self.player.get_video_info_text())
        if 'paused' in st:
            if st['paused']:
                self.play_btn.setText("▶ Играть")
            else:
                self.play_btn.setText("⏸ Пауза")

    def _on_error(self, msg: str):
        if not _is_qobject_valid(self):
            return
        self._update_link_buttons()

    def _safe_release(self):
        with self._release_lock:
            if self._player_released:
                return
            self._player_released = True
        if self.player is not None:
            with suppress(Exception):
                self.player.release()

    def closeEvent(self, event):
        with suppress(Exception):
            self._epg_timer.stop()
        self._safe_release()
        super().closeEvent(event)

    def reject(self):
        with suppress(Exception):
            self._epg_timer.stop()
        self._safe_release()
        super().reject()