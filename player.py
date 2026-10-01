# -*- coding: utf-8 -*-
"""EmbeddedVlcPlayer, EmbeddedPlayerDialog + патч."""

from __future__ import annotations
import sys
import re
from contextlib import suppress
from typing import Dict, List, Optional, Tuple
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSlider, QCheckBox,
    QTreeWidget, QTreeWidgetItem, QSizePolicy, QLayout)
from constants import (VLC_INSTANCE_USER_AGENT,
    VLC_PLAYER_DEFAULT_VOLUME, VLC_PLAYER_DEFAULT_WIDTH,
    VLC_PLAYER_DEFAULT_HEIGHT, STREAMING_PROTOCOLS)
from models import ChannelData
from paths import (logger, error_box, warn_box, info_box,
    save_file_dialog)
from dialogs import BaseDialog, _is_qobject_valid

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

def is_vlc_available() -> bool:
    return _HAS_VLC_MODULE

def get_vlc_error() -> str:
    if _HAS_VLC_MODULE:
        return ""
    if _VLC_IMPORT_ERROR:
        return f"Не удалось импортировать python-vlc: {_VLC_IMPORT_ERROR}"
    return "Модуль python-vlc не установлен."

class EmbeddedVlcPlayer(QWidget):
    playback_error = pyqtSignal(str)
    end_reached = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setAutoFillBackground(True)
        self.setStyleSheet("background-color: black;")

        self._vlc_instance = None
        self._media_player = None
        self._current_url = ""
        self._current_user_agent = ""
        self._is_playing = False
        self._event_attached = False

        self._init_vlc()

    def _init_vlc(self):
        if not _HAS_VLC_MODULE:
            return
        try:
            args = ["--quiet"]
            if sys.platform.startswith("linux"):
                args.insert(0, "--no-xlib")
            self._vlc_instance = vlc.Instance(*args)
            if self._vlc_instance is None:
                logger.error("VLC Instance вернул None")
                self._vlc_instance = None
                self._media_player = None
                return
            self._vlc_instance.set_user_agent(
                VLC_INSTANCE_USER_AGENT,
                VLC_INSTANCE_USER_AGENT,
            )
            self._media_player = self._vlc_instance.media_player_new()
        except Exception:
            logger.exception("Ошибка инициализации VLC")
            self._vlc_instance = None
            self._media_player = None

    def is_vlc_ready(self) -> bool:
        return self._media_player is not None

    def _attach_end_event(self):
        if self._event_attached or self._media_player is None:
            return
        try:
            events = self._media_player.event_manager()
            events.event_attach(
                vlc.EventType.MediaPlayerEndReached,
                lambda *a: self.end_reached.emit())
            self._event_attached = True
        except Exception:
            logger.exception("event_attach EndReached")

    def play_url(self, url: str, user_agent: str = "",
                 extra_headers: Optional[Dict[str, str]] = None):
        if not self.is_vlc_ready():
            self.playback_error.emit("VLC не инициализирован")
            return
        if not url or not url.strip():
            self.playback_error.emit("Пустой URL")
            return
        try:
            self.stop()
            self._current_url = url
            self._current_user_agent = user_agent or ""

            media = self._vlc_instance.media_new(url)
            if user_agent:
                media.add_option(f":http-user-agent={user_agent}")
            if extra_headers:
                for k, v in extra_headers.items():
                    if k.lower() == 'user-agent':
                        continue
                    if k.lower() == 'referer':
                        media.add_option(f":http-referrer={v}")
                    else:
                        media.add_option(f":http-header={k}: {v}")

            self._media_player.set_media(media)

            if sys.platform.startswith("linux"):
                self._media_player.set_xwindow(int(self.winId()))
            elif sys.platform == "win32":
                self._media_player.set_hwnd(int(self.winId()))
            elif sys.platform == "darwin":
                self._media_player.set_nsobject(int(self.winId()))

            self._attach_end_event()
            self._media_player.play()
            self._is_playing = True
        except Exception as e:
            logger.exception("Ошибка воспроизведения VLC")
            self.playback_error.emit(str(e))

    def stop(self):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.stop()
        self._is_playing = False
        self._current_url = ""

    def pause(self):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.pause()

    def set_volume(self, volume: int):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.audio_set_volume(max(0, min(100, volume)))

    def get_metadata(self, meta_type: int) -> Optional[str]:
        if self._media_player is None:
            return None
        with suppress(Exception):
            media = self._media_player.get_media()
            if media is None:
                return None
            v = media.get_meta(meta_type)
            return v if v else None
        return None

    def get_audio_tracks(self) -> List[Tuple[int, str]]:
        if self._media_player is None:
            return []
        with suppress(Exception):
            desc = self._media_player.audio_get_track_description()
            if desc:
                return [(t[0], t[1].decode('utf-8', errors='replace')
                         if isinstance(t[1], bytes) else str(t[1]))
                        for t in desc]
        return []

    def get_video_tracks(self) -> List[Tuple[int, str]]:
        if self._media_player is None:
            return []
        with suppress(Exception):
            desc = self._media_player.video_get_track_description()
            if desc:
                return [(t[0], t[1].decode('utf-8', errors='replace')
                         if isinstance(t[1], bytes) else str(t[1]))
                        for t in desc]
        return []

    def get_subtitle_tracks(self) -> List[Tuple[int, str]]:
        if self._media_player is None:
            return []
        with suppress(Exception):
            desc = self._media_player.video_get_spu_description()
            if desc:
                return [(t[0], t[1].decode('utf-8', errors='replace')
                         if isinstance(t[1], bytes) else str(t[1]))
                        for t in desc]
        return []

    def set_audio_track(self, track_id: int):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.audio_set_track(track_id)

    def set_video_track(self, track_id: int):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.video_set_track(track_id)

    def set_subtitle_track(self, track_id: int):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.video_set_spu(track_id)

    def set_aspect_ratio(self, ratio: str):
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.video_set_aspect_ratio(ratio or None)

    def take_snapshot(self, path: str) -> bool:
        if self._media_player is None:
            return False
        with suppress(Exception):
            return bool(self._media_player.video_take_snapshot(0, path, 0, 0))
        return False

    def get_media_info(self) -> Dict[str, str]:
        info: Dict[str, str] = {}
        if _HAS_VLC_MODULE:
            for label, mtype in (("Title", vlc.Meta.Title),
                                 ("Artist", vlc.Meta.Artist),
                                 ("Album", vlc.Meta.Album),
                                 ("NowPlaying", vlc.Meta.NowPlaying)):
                v = self.get_metadata(mtype)
                if v:
                    info[label] = v
        if self._current_url:
            info["URL"] = self._current_url[:120]
        if self._current_user_agent:
            info["User-Agent"] = self._current_user_agent[:80]
        return info

    def closeEvent(self, event):
        self.stop()
        if self._media_player is not None:
            with suppress(Exception):
                self._media_player.release()
            self._media_player = None
        if self._vlc_instance is not None:
            with suppress(Exception):
                self._vlc_instance.release()
            self._vlc_instance = None
        super().closeEvent(event)

class EmbeddedPlayerDialog(BaseDialog):
    def __init__(self, channel: ChannelData, parent=None,
                 playlist: Optional[List[ChannelData]] = None):
        super().__init__(
            f"Ksenia Player — {channel.meta.name}",
            parent,
            size=(VLC_PLAYER_DEFAULT_WIDTH, VLC_PLAYER_DEFAULT_HEIGHT),
        )
        from ksenia_window import ApplicationCore
        self.core = ApplicationCore.instance()

        src = playlist if playlist else [channel]
        self.playlist: List[ChannelData] = [ch for ch in src if ch.has_valid_url]
        if not self.playlist:
            self.playlist = [channel]
        self.index = next(
            (i for i, ch in enumerate(self.playlist) if ch.uid == channel.uid),
            0)
        self.channel = self.playlist[self.index]

        self.setWindowModality(Qt.WindowModality.NonModal)

        self.player = EmbeddedVlcPlayer(self)
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

        controls.addWidget(QLabel("Громкость:"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        saved_volume = int(self.core.config.get('vlc_volume',
                                                  VLC_PLAYER_DEFAULT_VOLUME))
        self.volume_slider.setValue(saved_volume)
        self.volume_slider.setFixedWidth(150)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        controls.addWidget(self.volume_slider)

        self.snapshot_btn = QPushButton("📷")
        self.snapshot_btn.setToolTip("Снимок кадра")
        self.snapshot_btn.clicked.connect(self._take_snapshot)
        controls.addWidget(self.snapshot_btn)

        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(["По умолчанию", "16:9", "4:3", "1:1"])
        self.aspect_combo.currentTextChanged.connect(self._on_aspect_changed)
        controls.addWidget(self.aspect_combo)

        self.info_btn = QPushButton("ℹ")
        self.info_btn.setToolTip("Медиа-инфо (I)")
        self.info_btn.clicked.connect(self._show_media_info)
        controls.addWidget(self.info_btn)

        controls.addStretch()
        self.status_label = QLabel("Готов")
        controls.addWidget(self.status_label, 1)
        self.root.addLayout(controls)

        tracks_layout = QHBoxLayout()
        self.audio_combo = QComboBox()
        self.audio_combo.addItem("Аудио: по умолчанию", -1)
        self.audio_combo.currentIndexChanged.connect(self._on_audio_track_changed)
        tracks_layout.addWidget(self.audio_combo)

        self.video_combo = QComboBox()
        self.video_combo.addItem("Видео: по умолчанию", -1)
        self.video_combo.currentIndexChanged.connect(self._on_video_track_changed)
        tracks_layout.addWidget(self.video_combo)

        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("Субтитры: выкл", -1)
        self.subtitle_combo.currentIndexChanged.connect(self._on_subtitle_track_changed)
        tracks_layout.addWidget(self.subtitle_combo)
        self.root.addLayout(tracks_layout)

        self.fullscreen_check = QCheckBox("Полноэкранный режим")
        self.fullscreen_check.setChecked(
            bool(self.core.config.get('vlc_start_fullscreen', False)))
        self.fullscreen_check.toggled.connect(self._on_fullscreen_toggled)
        self.root.addWidget(self.fullscreen_check)

        self.add_close()

        self.player.playback_error.connect(self._on_playback_error)
        self.player.end_reached.connect(self._on_end_reached)

        self._shortcuts: List[QShortcut] = []
        sc_next = QShortcut(QKeySequence("Ctrl+Right"), self)
        sc_next.activated.connect(self.play_next)
        self._shortcuts.append(sc_next)
        sc_prev = QShortcut(QKeySequence("Ctrl+Left"), self)
        sc_prev.activated.connect(self.play_previous)
        self._shortcuts.append(sc_prev)
        sc_info = QShortcut(QKeySequence("I"), self)
        sc_info.activated.connect(self._show_media_info)
        self._shortcuts.append(sc_info)
        sc_aspect_16_9 = QShortcut(QKeySequence("A"), self)
        sc_aspect_16_9.activated.connect(
            lambda: self.aspect_combo.setCurrentText("16:9"))
        self._shortcuts.append(sc_aspect_16_9)
        sc_aspect_default = QShortcut(QKeySequence("Z"), self)
        sc_aspect_default.activated.connect(
            lambda: self.aspect_combo.setCurrentText("По умолчанию"))
        self._shortcuts.append(sc_aspect_default)

        self.player.set_volume(saved_volume)
        self._play_at(self.index)

        if self.fullscreen_check.isChecked():
            self.showFullScreen()

    def _play_at(self, index: int):
        if not _is_qobject_valid(self):
            return
        if not (0 <= index < len(self.playlist)):
            return
        self.index = index
        self.channel = self.playlist[index]
        self.setWindowTitle(f"Ksenia Player — {self.channel.meta.name}")
        ua = self.channel.link.user_agent or ""
        extra = dict(self.channel.link.extra_headers) \
            if self.channel.link.extra_headers else None
        self.player.play_url(self.channel.link.url, ua, extra)
        self.status_label.setText(f"[{index + 1}/{len(self.playlist)}] "
                                  f"{self.channel.meta.name}")
        self._update_nav_buttons()
        QTimer.singleShot(500, self._refresh_tracks)

    def play_next(self):
        if self.index < len(self.playlist) - 1:
            self._play_at(self.index + 1)

    def play_previous(self):
        if self.index > 0:
            self._play_at(self.index - 1)

    def _update_nav_buttons(self):
        if not _is_qobject_valid(self):
            return
        self.prev_btn.setEnabled(self.index > 0)
        self.next_btn.setEnabled(self.index < len(self.playlist) - 1)

    def _on_end_reached(self):
        if not _is_qobject_valid(self):
            return
        if self.index < len(self.playlist) - 1:
            self.play_next()

    def _toggle_pause(self):
        if not _is_qobject_valid(self):
            return
        self.player.pause()

    def _on_volume_changed(self, value: int):
        if not _is_qobject_valid(self):
            return
        self.player.set_volume(value)
        self.core.config.set('vlc_volume', value)

    def _on_fullscreen_toggled(self, checked: bool):
        if not _is_qobject_valid(self):
            return
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()
        self.core.config.set('vlc_start_fullscreen', bool(checked))

    def _on_aspect_changed(self, text: str):
        if not _is_qobject_valid(self):
            return
        ratio = "" if text == "По умолчанию" else text
        self.player.set_aspect_ratio(ratio)

    def _on_audio_track_changed(self, idx: int):
        if not _is_qobject_valid(self) or idx <= 0:
            return
        track_id = self.audio_combo.itemData(idx)
        if track_id is not None and track_id >= 0:
            self.player.set_audio_track(track_id)

    def _on_video_track_changed(self, idx: int):
        if not _is_qobject_valid(self) or idx <= 0:
            return
        track_id = self.video_combo.itemData(idx)
        if track_id is not None and track_id >= 0:
            self.player.set_video_track(track_id)

    def _on_subtitle_track_changed(self, idx: int):
        if not _is_qobject_valid(self):
            return
        track_id = self.subtitle_combo.itemData(idx)
        if track_id is not None:
            self.player.set_subtitle_track(track_id)

    def _refresh_tracks(self):
        if not _is_qobject_valid(self):
            return
        audio = self.player.get_audio_tracks()
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("Аудио: по умолчанию", -1)
        for tid, name in audio:
            self.audio_combo.addItem(f"Аудио: {name}", tid)
        self.audio_combo.setVisible(len(audio) > 1)
        self.audio_combo.blockSignals(False)

        video = self.player.get_video_tracks()
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        self.video_combo.addItem("Видео: по умолчанию", -1)
        for tid, name in video:
            self.video_combo.addItem(f"Видео: {name}", tid)
        self.video_combo.setVisible(len(video) > 1)
        self.video_combo.blockSignals(False)

        subs = self.player.get_subtitle_tracks()
        self.subtitle_combo.blockSignals(True)
        self.subtitle_combo.clear()
        self.subtitle_combo.addItem("Субтитры: выкл", -1)
        for tid, name in subs:
            self.subtitle_combo.addItem(f"Субтитры: {name}", tid)
        self.subtitle_combo.setVisible(len(subs) > 1)
        self.subtitle_combo.blockSignals(False)

        title = None
        now = None
        if _HAS_VLC_MODULE:
            title = self.player.get_metadata(vlc.Meta.Title)
            now = self.player.get_metadata(vlc.Meta.NowPlaying)
        parts = [f"[{self.index + 1}/{len(self.playlist)}] {self.channel.meta.name}"]
        if title:
            parts.append(title)
        if now and now != title:
            parts.append(now)
        self.status_label.setText(" — ".join(parts))

    def _take_snapshot(self):
        if not _is_qobject_valid(self):
            return
        default = f"snapshot_{self.channel.meta.name[:30]}.png"
        default = re.sub(r'[<>:"/\\|?*]', '_', default)
        fp = save_file_dialog(self, "Снимок кадра", default,
                              "PNG (*.png);;Все файлы (*.*)")
        if not fp:
            return
        if self.player.take_snapshot(fp):
            info_box(self, f"Снимок сохранён:\n{fp}", "Снимок")
        else:
            warn_box(self, "Не удалось сделать снимок.\n"
                           "Убедитесь, что поток воспроизводится.")

    def _show_media_info(self):
        if not _is_qobject_valid(self):
            return
        info = self.player.get_media_info()
        if not info:
            info_box(self, "Нет доступной информации", "Медиа-инфо")
            return
        text = "\n".join(f"{k}: {v}" for k, v in info.items())
        info_box(self, text, "Медиа-инфо")

    def _on_playback_error(self, msg: str):
        if not _is_qobject_valid(self):
            return
        self.status_label.setText(f"Ошибка: {msg}")
        error_box(self, msg, "Ошибка VLC")

    def closeEvent(self, event):
        for sh in self._shortcuts:
            with suppress(TypeError, RuntimeError):
                sh.activated.disconnect()
            sh.setParent(None)
            sh.deleteLater()
        self._shortcuts.clear()
        if _is_qobject_valid(self):
            with suppress(Exception):
                self.player.stop()
            with suppress(Exception):
                self.player.closeEvent(event)
        super().closeEvent(event)

def _apply_player_patch():
    """Подменяет EmbeddedVlcPlayer и EmbeddedPlayerDialog на патченные."""
    try:
        from PyQt6.QtCore import QSize as _QSize, QTimer as _QTimer
        from PyQt6.QtWidgets import (
            QTreeWidget as _QTreeWidget,
            QTreeWidgetItem as _QTreeWidgetItem,
            QSizePolicy as _QSizePolicy,
            QLayout as _QLayout,
        )
    except Exception as _e:
        logger.warning(f"[PATCH] Импорт Qt не удался: {_e}")
        return False

    g = globals()
    if g.get('_KSENIA_PATCH_APPLIED'):
        logger.info('[PATCH] Уже применён — пропуск')
        return True

    _DEFAULT_W = 960
    _DEFAULT_H = 600
    _MIN_W = 640
    _MIN_H = 480

    _HAS = g.get('_HAS_VLC_MODULE', False)
    _VLC = g.get('vlc', None)
    _VALID = g.get('_is_qobject_valid', lambda x: x is not None)
    _BASE = g.get('BaseDialog')
    _OLD_PLAYER = g.get('EmbeddedVlcPlayer')
    try:
        from ksenia_window import ApplicationCore as _CORE_CLS
    except Exception:
        _CORE_CLS = g.get('ApplicationCore')
    _ERR_BOX = g.get('error_box')
    _INFO_BOX = g.get('info_box')
    _VLC_DEFAULT_VOL = g.get('VLC_PLAYER_DEFAULT_VOLUME', 100)

    if _BASE is None or _OLD_PLAYER is None:
        logger.warning("[PATCH] Не найдены базовые классы — патч не применён")
        return False

    from contextlib import suppress as _suppress

    def _fmt_duration(ms):
        if not ms or ms <= 0:
            return "—"
        s = int(ms) // 1000
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _fmt_bytes(b):
        if not b or b <= 0:
            return "0 B"
        units = ("B", "KB", "MB", "GB")
        f = float(b)
        for u in units:
            if f < 1024.0:
                return f"{f:.1f} {u}"
            f /= 1024.0
        return f"{f:.1f} TB"

    class PatchedVlcPlayer(_OLD_PLAYER):

        def play_url(self, url, user_agent="", extra_headers=None):
            """Неблокирующее переключение — без stop(), с set_media(None)."""
            if not self.is_vlc_ready():
                self.playback_error.emit("VLC не инициализирован")
                return
            if not url or not url.strip():
                self.playback_error.emit("Пустой URL")
                return
            try:
                
                with _suppress(Exception):
                    self._media_player.set_media(None)

                self._current_url = url
                self._current_user_agent = user_agent or ""

                media = self._vlc_instance.media_new(url)

                for opt in (":network-caching=800",
                            ":live-caching=800",
                            ":http-reconnect=true",
                            ":ipv4-timeout=5000"):
                    with _suppress(Exception):
                        media.add_option(opt)

                if user_agent:
                    media.add_option(f":http-user-agent={user_agent}")
                if extra_headers:
                    for k, v in extra_headers.items():
                        if k.lower() == 'user-agent':
                            continue
                        if k.lower() == 'referer':
                            media.add_option(f":http-referrer={v}")
                        else:
                            media.add_option(f":http-header={k}: {v}")

                self._media_player.set_media(media)
                if sys.platform.startswith("linux"):
                    self._media_player.set_xwindow(int(self.winId()))
                elif sys.platform == "win32":
                    self._media_player.set_hwnd(int(self.winId()))
                elif sys.platform == "darwin":
                    self._media_player.set_nsobject(int(self.winId()))

                self._attach_end_event()
                self._media_player.play()
                self._is_playing = True
            except Exception as e:
                logger.exception("Ошибка воспроизведения VLC")
                self.playback_error.emit(str(e))

        def get_media_info(self):
            info = {}
            if not _HAS or self._media_player is None or _VLC is None:
                return info

            meta_map = {
                "Title":       _VLC.Meta.Title,
                "Artist":      _VLC.Meta.Artist,
                "Album":       _VLC.Meta.Album,
                "Genre":       _VLC.Meta.Genre,
                "Copyright":   _VLC.Meta.Copyright,
                "Description": _VLC.Meta.Description,
                "Rating":      _VLC.Meta.Rating,
                "Date":        _VLC.Meta.Date,
                "Setting":     _VLC.Meta.Setting,
                "URL":         _VLC.Meta.URL,
                "Language":    _VLC.Meta.Language,
                "NowPlaying":  _VLC.Meta.NowPlaying,
                "Publisher":   _VLC.Meta.Publisher,
                "EncodedBy":   _VLC.Meta.EncodedBy,
                "ArtworkURL":  _VLC.Meta.ArtworkURL,
                "TrackID":     _VLC.Meta.TrackID,
                "TrackTotal":  _VLC.Meta.TrackTotal,
                "Director":    _VLC.Meta.Director,
                "Season":      _VLC.Meta.Season,
                "Episode":     _VLC.Meta.Episode,
                "ShowName":    _VLC.Meta.ShowName,
                "Actors":      _VLC.Meta.Actors,
            }
            for label, mtype in meta_map.items():
                with _suppress(Exception):
                    v = self.get_metadata(mtype)
                    if v:
                        info[f"Meta/{label}"] = v

            with _suppress(Exception):
                media = self._media_player.get_media()
                if media is not None:
                    dur = media.get_duration()
                    if dur and dur > 0:
                        info["Media/Duration"] = _fmt_duration(dur)
                    mrl = media.get_mrl()
                    if mrl:
                        info["Media/MRL"] = mrl[:200]

            with _suppress(Exception):
                states = {
                    _VLC.State.NothingSpecial: "NothingSpecial",
                    _VLC.State.Opening:        "Opening",
                    _VLC.State.Buffering:      "Buffering",
                    _VLC.State.Playing:        "Playing",
                    _VLC.State.Paused:         "Paused",
                    _VLC.State.Stopped:        "Stopped",
                    _VLC.State.Ended:          "Ended",
                    _VLC.State.Error:          "Error",
                }
                st = self._media_player.get_state()
                info["Player/State"] = states.get(st, str(st))
                info["Player/IsPlaying"] = str(self._media_player.is_playing())
                info["Player/CanPause"] = str(self._media_player.can_pause())
                info["Player/CanSeek"] = str(self._media_player.can_seek())
                vol = self._media_player.audio_get_volume()
                if vol is not None and vol >= 0:
                    info["Player/Volume"] = str(vol)

            with _suppress(Exception):
                t = self._media_player.get_time()
                if t is not None and t >= 0:
                    info["Playback/Time"] = _fmt_duration(t)
                pos = self._media_player.get_position()
                if pos is not None and pos >= 0:
                    info["Playback/Position"] = f"{pos * 100:.1f}%"

            with _suppress(Exception):
                tr = self._media_player.audio_get_track()
                desc = self._media_player.audio_get_track_description()
                for tid, name in desc or []:
                    if tid == tr:
                        nm = (name.decode('utf-8', 'replace')
                              if isinstance(name, bytes) else str(name))
                        info["Audio/Track"] = f"{tid}: {nm}"
                        break

            with _suppress(Exception):
                ch = self._media_player.audio_get_channel()
                if ch is not None:
                    info["Audio/Channels"] = str(ch)

            with _suppress(Exception):
                codec = self._media_player.audio_get_codec()
                if codec:
                    info["Audio/Codec"] = str(codec)

            with _suppress(Exception):
                tr = self._media_player.video_get_track()
                desc = self._media_player.video_get_track_description()
                for tid, name in desc or []:
                    if tid == tr:
                        nm = (name.decode('utf-8', 'replace')
                              if isinstance(name, bytes) else str(name))
                        info["Video/Track"] = f"{tid}: {nm}"
                        break

            with _suppress(Exception):
                size = self._media_player.video_get_size(0)
                if size and size[0] and size[1]:
                    info["Video/Size"] = f"{size[0]}x{size[1]}"

            with _suppress(Exception):
                fps = self._media_player.get_fps()
                if fps and fps > 0:
                    info["Video/FPS"] = f"{fps:.2f}"

            with _suppress(Exception):
                ar = self._media_player.video_get_aspect_ratio()
                if ar:
                    info["Video/Aspect"] = str(ar)

            with _suppress(Exception):
                spu = self._media_player.video_get_spu()
                desc = self._media_player.video_get_spu_description()
                for tid, name in desc or []:
                    if tid == spu:
                        nm = (name.decode('utf-8', 'replace')
                              if isinstance(name, bytes) else str(name))
                        info["Subtitle/Track"] = f"{tid}: {nm}"
                        break

            with _suppress(Exception):
                stats = self._media_player.get_stats()
                if stats:
                    for k, v in stats.items():
                        if k in ("read_bytes", "demux_read_bytes",
                                 "input_bitrate", "demux_bitrate"):
                            continue
                        info[f"Stats/{k}"] = str(v)
                    rb = stats.get("read_bytes")
                    if rb:
                        info["Stats/read_bytes"] = _fmt_bytes(int(rb))
                    ib = stats.get("input_bitrate")
                    if ib:
                        info["Stats/input_bitrate"] = f"{ib * 1000:.0f} kbps"

            if self._current_url:
                info["Stream/URL"] = self._current_url[:200]
            if self._current_user_agent:
                info["Stream/User-Agent"] = self._current_user_agent[:120]

            return info

    class MediaInfoDialog(_BASE):
        GROUP_ORDER = (
            "Player", "Playback", "Media", "Stream",
            "Video", "Audio", "Subtitle", "Stats",
            "Meta", "Channel", "Общее",
        )

        def __init__(self, player, channel=None, parent=None):
            super().__init__("Медиа-инфо", parent, size=(640, 700))
            from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton
            self.player = player
            self.channel = channel

            self.tree = _QTreeWidget()
            self.tree.setHeaderLabels(["Параметр", "Значение"])
            self.tree.setColumnWidth(0, 240)
            self.tree.setAlternatingRowColors(True)
            self.tree.setUniformRowHeights(True)
            self.root.addWidget(self.tree, 1)

            row = QHBoxLayout()
            self.refresh_btn = QPushButton("🔄 Обновить")
            self.refresh_btn.clicked.connect(self.refresh)
            row.addWidget(self.refresh_btn)

            self.copy_btn = QPushButton("📋 Скопировать всё")
            self.copy_btn.clicked.connect(self._copy_all)
            row.addWidget(self.copy_btn)

            self.pause_btn = QPushButton("⏸ Пауза авто")
            self.pause_btn.setCheckable(True)
            self.pause_btn.toggled.connect(self._on_pause)
            row.addWidget(self.pause_btn)

            row.addStretch()
            self.root.addLayout(row)

            self.add_close()

            self._timer = _QTimer(self)
            self._timer.setInterval(1000)
            self._timer.timeout.connect(self.refresh)
            self._timer.start()

            self.refresh()

        def _on_pause(self, paused):
            if paused:
                self._timer.stop()
                self.pause_btn.setText("▶ Возобновить")
            else:
                self._timer.start()
                self.pause_btn.setText("⏸ Пауза авто")

        def _collect(self):
            info = {}
            with _suppress(Exception):
                info.update(self.player.get_media_info())
            ch = self.channel
            if ch is not None:
                info["Channel/Name"] = ch.meta.name or "—"
                info["Channel/Group"] = ch.meta.group or "—"
                if ch.meta.tvg_id:
                    info["Channel/TVG-ID"] = ch.meta.tvg_id
                if ch.meta.tvg_name:
                    info["Channel/TVG-Name"] = ch.meta.tvg_name
                if ch.meta.tvg_logo:
                    info["Channel/TVG-Logo"] = ch.meta.tvg_logo
                if ch.link.url:
                    info["Channel/URL"] = ch.link.url[:200]
                if ch.link.user_agent:
                    info["Channel/User-Agent"] = ch.link.user_agent[:120]
                if ch.link.link_source:
                    info["Channel/Source"] = ch.link.link_source
            return info

        def refresh(self):
            info = self._collect()
            groups = {}
            for k, v in info.items():
                if "/" in k:
                    cat, field = k.split("/", 1)
                else:
                    cat, field = "Общее", k
                groups.setdefault(cat, []).append((field, str(v)))

            expanded = set()
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                if top.isExpanded():
                    expanded.add(top.text(0))

            self.tree.clear()
            ordered = [c for c in self.GROUP_ORDER if c in groups]
            for c in groups:
                if c not in ordered:
                    ordered.append(c)

            for cat in ordered:
                top = _QTreeWidgetItem([cat, ""])
                top.setExpanded(cat in expanded or cat in ("Player", "Playback"))
                f = top.font(0)
                f.setBold(True)
                top.setFont(0, f)
                for field, val in groups[cat]:
                    child = _QTreeWidgetItem([field, val])
                    child.setToolTip(1, val)
                    top.addChild(child)
                self.tree.addTopLevelItem(top)

        def _copy_all(self):
            info = self._collect()
            text = "\n".join(f"{k}: {v}" for k, v in info.items())
            from PyQt6.QtWidgets import QApplication as _QA
            _QA.clipboard().setText(text)
            if _INFO_BOX:
                _INFO_BOX(self, "Скопировано в буфер обмена.", "Медиа-инфо")

        def closeEvent(self, event):
            self._timer.stop()
            super().closeEvent(event)

    class PatchedPlayerDialog(_BASE):

        def __init__(self, channel, parent=None, playlist=None):
            super().__init__(
                f"Ksenia Player — {channel.meta.name}",
                parent,
                size=(_DEFAULT_W, _DEFAULT_H),
            )
            from PyQt6.QtWidgets import (
                QHBoxLayout, QVBoxLayout, QPushButton, QLabel,
                QSlider, QComboBox,
            )
            from PyQt6.QtCore import Qt as _Qt
            from PyQt6.QtGui import QKeySequence as _QKS, QShortcut as _QSC

            from ksenia_window import ApplicationCore
            self.core = ApplicationCore.instance()

            src = playlist if playlist else [channel]
            self.playlist = [ch for ch in src if ch.has_valid_url] or [channel]
            self.index = next(
                (i for i, ch in enumerate(self.playlist)
                 if ch.uid == channel.uid), 0)
            self.channel = self.playlist[self.index]

            self.setWindowModality(_Qt.WindowModality.NonModal)

            self._normal_size = _QSize(_DEFAULT_W, _DEFAULT_H)
            self._resize_locked = False
            self.setSizeGripEnabled(False)
            with _suppress(Exception):
                self.root.setSizeConstraint(
                    _QLayout.SizeConstraint.SetNoConstraint)
            self.setMinimumSize(_MIN_W, _MIN_H)

            self.player = PatchedVlcPlayer(self)
            self.player.setMinimumSize(320, 240)
            self.player.setSizePolicy(
                _QSizePolicy.Policy.Expanding,
                _QSizePolicy.Policy.Expanding)
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

            self.volume_slider = QSlider(_Qt.Orientation.Horizontal)
            self.volume_slider.setRange(0, 100)
            saved_vol = int(self.core.config.get('vlc_volume', _VLC_DEFAULT_VOL))
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
            self.info_btn = QPushButton("ℹ Медиа-инфо")
            self.info_btn.setToolTip("Медиа-инфо (I)")
            self.info_btn.clicked.connect(self._show_media_info)
            controls.addWidget(self.info_btn)

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

            self.subtitle_combo = QComboBox()
            self.subtitle_combo.addItem("Субтитры: выкл", -1)
            self.subtitle_combo.currentIndexChanged.connect(self._on_sub)
            tracks.addWidget(self.subtitle_combo)
            self.root.addLayout(tracks)

            self.add_close()

            self.player.playback_error.connect(self._on_error)
            self.player.end_reached.connect(self._on_end)

            self._shortcuts = []
            for keys, slot in (
                ("Ctrl+Right", self.play_next),
                ("Ctrl+Left", self.play_previous),
                ("I", self._show_media_info),
                ("A", lambda: self.aspect_combo.setCurrentText("16:9")),
                ("Z", lambda: self.aspect_combo.setCurrentText("По умолчанию")),
                ("Space", self._toggle_pause),
            ):
                sc = _QSC(_QKS(keys), self)
                sc.activated.connect(slot)
                self._shortcuts.append(sc)

            self.player.set_volume(saved_vol)
            self._play_at(self.index)
            self._info_dialog = None

        def resizeEvent(self, event):
            """Блокирует авторесайз во время смены канала."""
            if (getattr(self, '_resize_locked', False)
                    and not self.isMaximized()
                    and not self.isFullScreen()):
                if event.size() != self._normal_size:
                    super().resizeEvent(event)
                    _QTimer.singleShot(0, self._restore_size)
                    return
            super().resizeEvent(event)

        def _restore_size(self):
            if not _VALID(self) or self.isMaximized():
                return
            if self.size() != self._normal_size:
                self.resize(self._normal_size)

        def _unlock_resize(self):
            self._resize_locked = False
            self._restore_size()

        def _play_at(self, index):
            if not _VALID(self):
                return
            if not (0 <= index < len(self.playlist)):
                return
            self.index = index
            self.channel = self.playlist[index]
            self.setWindowTitle(f"Ksenia Player — {self.channel.meta.name}")

            self._resize_locked = True
            if not self.isMaximized() and self.size() != self._normal_size:
                self.resize(self._normal_size)

            ua = self.channel.link.user_agent or ""
            extra = (dict(self.channel.link.extra_headers)
                     if self.channel.link.extra_headers else None)
            self.player.play_url(self.channel.link.url, ua, extra)
            self._update_nav()
            _QTimer.singleShot(500, self._refresh_tracks)
            _QTimer.singleShot(1500, self._refresh_tracks)
            _QTimer.singleShot(2000, self._unlock_resize)

        def play_next(self):
            if self.index < len(self.playlist) - 1:
                _QTimer.singleShot(
                    50, lambda: self._play_at(self.index + 1))

        def play_previous(self):
            if self.index > 0:
                _QTimer.singleShot(
                    50, lambda: self._play_at(self.index - 1))

        def _update_nav(self):
            if not _VALID(self):
                return
            self.prev_btn.setEnabled(self.index > 0)
            self.next_btn.setEnabled(self.index < len(self.playlist) - 1)

        def _on_end(self):
            if not _VALID(self):
                return
            if self.index < len(self.playlist) - 1:
                self.play_next()

        def _toggle_pause(self):
            if _VALID(self):
                self.player.pause()

        def _on_volume(self, v):
            if not _VALID(self):
                return
            self.player.set_volume(v)
            self.core.config.set('vlc_volume', v)

        def _on_aspect(self, text):
            if not _VALID(self):
                return
            self.player.set_aspect_ratio(
                "" if text == "По умолчанию" else text)

        def _on_audio(self, idx):
            if not _VALID(self) or idx <= 0:
                return
            tid = self.audio_combo.itemData(idx)
            if tid is not None and tid >= 0:
                self.player.set_audio_track(tid)

        def _on_video(self, idx):
            if not _VALID(self) or idx <= 0:
                return
            tid = self.video_combo.itemData(idx)
            if tid is not None and tid >= 0:
                self.player.set_video_track(tid)

        def _on_sub(self, idx):
            if not _VALID(self):
                return
            tid = self.subtitle_combo.itemData(idx)
            if tid is not None:
                self.player.set_subtitle_track(tid)

        def _refresh_tracks(self):
            if not _VALID(self):
                return
            
            if getattr(self, '_resize_locked', False):
                return
            self.setUpdatesEnabled(False)
            try:
                audio = self.player.get_audio_tracks()
                self.audio_combo.blockSignals(True)
                self.audio_combo.clear()
                self.audio_combo.addItem("Аудио: по умолчанию", -1)
                for tid, name in audio:
                    self.audio_combo.addItem(f"Аудио: {name}", tid)
                self.audio_combo.setVisible(len(audio) > 1)
                self.audio_combo.blockSignals(False)

                video = self.player.get_video_tracks()
                self.video_combo.blockSignals(True)
                self.video_combo.clear()
                self.video_combo.addItem("Видео: по умолчанию", -1)
                for tid, name in video:
                    self.video_combo.addItem(f"Видео: {name}", tid)
                self.video_combo.setVisible(len(video) > 1)
                self.video_combo.blockSignals(False)

                subs = self.player.get_subtitle_tracks()
                self.subtitle_combo.blockSignals(True)
                self.subtitle_combo.clear()
                self.subtitle_combo.addItem("Субтитры: выкл", -1)
                for tid, name in subs:
                    self.subtitle_combo.addItem(f"Субтитры: {name}", tid)
                self.subtitle_combo.setVisible(len(subs) > 1)
                self.subtitle_combo.blockSignals(False)
            finally:
                self.setUpdatesEnabled(True)

        def _show_media_info(self):
            if not _VALID(self):
                return
            if self._info_dialog is not None and self._info_dialog.isVisible():
                self._info_dialog.raise_()
                self._info_dialog.activateWindow()
                self._info_dialog.refresh()
                return
            from PyQt6.QtCore import Qt as _Qt
            dlg = MediaInfoDialog(self.player, self.channel, self)
            dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda: setattr(self, '_info_dialog', None))
            self._info_dialog = dlg
            dlg.show()

        def _on_error(self, msg):
            if _VALID(self) and _ERR_BOX:
                _ERR_BOX(self, msg, "Ошибка VLC")

        def closeEvent(self, event):
            for sc in self._shortcuts:
                with _suppress(Exception):
                    sc.activated.disconnect()
                sc.setParent(None)
                sc.deleteLater()
            self._shortcuts.clear()
            if _VALID(self):
                with _suppress(Exception):
                    self.player.stop()
            if self._info_dialog is not None:
                with _suppress(Exception):
                    self._info_dialog.close()
                self._info_dialog = None
            super().closeEvent(event)

    g['PatchedVlcPlayer'] = PatchedVlcPlayer
    g['PatchedPlayerDialog'] = PatchedPlayerDialog
    g['MediaInfoDialog'] = MediaInfoDialog
    g['EmbeddedVlcPlayer'] = PatchedVlcPlayer
    g['EmbeddedPlayerDialog'] = PatchedPlayerDialog
    g['_KSENIA_PATCH_APPLIED'] = True

    logger.info("[PATCH] Патч плеера применён (v2)")
    return True

_apply_player_patch()
