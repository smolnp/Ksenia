# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Проверка ссылок, фоновое разрешение и EPG для вкладки плейлиста."""
from __future__ import annotations

import queue
from contextlib import suppress
from datetime import datetime
from typing import List, Optional, Set

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QInputDialog, QLineEdit

from constants import (
    EPG_ALLOWED_META_FIELDS, FFPROBE_TIMEOUT_SEC)
from dialogs import _is_qobject_valid
from logging_setup import logger
from models import ChannelData, LinkQuality
from paths import confirm, error_box, info_box
from plural_ru import channels_word, links_word
from workers import (
    BackgroundResolutionWorker,
    EPGLoaderWorker, EPGMetadataApplyWorker,
    LinkReplacementWorker, ResolutionWorker)


def resolve_epg_urls_for_load(tab) -> Optional[List[str]]:
    urls = [u for u in tab.header_manager.epg_sources
            if u and u.strip()]
    if urls:
        return urls

    default_url = tab.core.get_default_epg_url()
    if default_url:
        if confirm(
                tab,
                "В шапке плейлиста нет EPG (url-tvg).\n\n"
                f"Загрузить EPG по умолчанию?\n{default_url}",
                "Загрузка EPG"):
            return [default_url]
        url, ok = QInputDialog.getText(
            tab, "Загрузка EPG", "URL EPG (XMLTV):",
            QLineEdit.EchoMode.Normal, default_url)
    else:
        url, ok = QInputDialog.getText(
            tab, "Загрузка EPG",
            "URL EPG (XMLTV):\n\n"
            "Встроенного EPG нет — укажите свой источник "
            "(или задайте в «Настройки → Общие»).",
            QLineEdit.EchoMode.Normal, "")
    if not ok or not url or not url.strip():
        return None
    return [url.strip()]


def load_epg_async(tab):
    if not _is_qobject_valid(tab):
        return
    if tab._epg_worker and tab._epg_worker.isRunning():
        info_box(tab, "Загрузка EPG уже идёт")
        return
    urls = resolve_epg_urls_for_load(tab)
    if not urls:
        return
    w = tab.parent_window
    if w is not None:
        w.set_action("⏳ Загрузка EPG...")
    from constants import EPG_LOAD_TIMEOUT_SEC
    worker = EPGLoaderWorker(
        urls, tab.core.epg_db, timeout=EPG_LOAD_TIMEOUT_SEC)
    tab._epg_worker = worker

    def on_progress(value, total, text):
        w2 = tab.parent_window
        if w2 is not None:
            w2.show_progress(value, total, text)

    def on_loaded(count, errors):
        if tab._epg_worker is worker:
            tab._epg_worker = None
        w2 = tab.parent_window
        if w2 is not None:
            w2.hide_progress()
        if not _is_qobject_valid(tab):
            return
        msg = f"Загружено {count} программ"
        if errors:
            shown = errors[:5]
            msg += "\n\nОшибки:\n" + "\n".join(shown)
            if len(errors) > 5:
                msg += f"\n... и ещё {len(errors) - 5}"
        info_box(tab, msg, "EPG")
        tab.core.epg_updated.emit()

    def on_error(msg):
        if tab._epg_worker is worker:
            tab._epg_worker = None
        w2 = tab.parent_window
        if w2 is not None:
            w2.hide_progress()
        if _is_qobject_valid(tab):
            error_box(tab, msg, "Ошибка EPG")

    worker.progress.connect(
        on_progress, Qt.ConnectionType.QueuedConnection)
    worker.epg_loaded.connect(
        on_loaded, Qt.ConnectionType.QueuedConnection)
    worker.error.connect(
        on_error, Qt.ConnectionType.QueuedConnection)
    worker.start()


def apply_epg_metadata_to_all(tab, silent: bool = False) -> bool:
    if not _is_qobject_valid(tab):
        return False
    if not tab.core.epg_db.has_channel_info:
        if not silent:
            info_box(tab, "EPG не загружен (или не содержит "
                          "<channel>-записей).\n"
                          "Загрузите EPG через "
                          "«Инструменты → Загрузить EPG».",
                     "EPG")
        return False
    if not tab.all_channels:
        return False
    if (tab._epg_meta_worker
            and tab._epg_meta_worker.isRunning()):
        with suppress(Exception):
            tab._epg_meta_worker.stop()
            tab._epg_meta_worker.wait(2000)
        tab._epg_meta_worker = None

    tab._epg_meta_generation += 1
    generation = tab._epg_meta_generation

    if not silent:
        tab.save_state("Применение EPG-метаданных")
    w = tab.parent_window
    if w is not None and not silent:
        w.set_action("⏳ Применение EPG-метаданных...")

    worker = EPGMetadataApplyWorker(
        tab.all_channels, tab.core.epg_db,
        tab.core.config, tab.core.get_replacement_settings())
    tab._epg_meta_worker = worker
    uid_map = {ch.uid: ch for ch in tab.all_channels}

    def on_progress(value, total, text):
        w2 = tab.parent_window
        if w2 is not None and not silent:
            w2.show_progress(value, total, text)

    def on_applied(count, results):
        if tab._epg_meta_worker is worker:
            tab._epg_meta_worker = None
        if not _is_qobject_valid(tab):
            return
        if generation != tab._epg_meta_generation:
            return
        w2 = tab.parent_window
        if w2 is not None and not silent:
            w2.hide_progress()

        touched_uids: Set[int] = set()
        for uid, field, value in results:
            ch = uid_map.get(uid)
            if ch is None:
                continue
            if field == 'epg_source':
                if ch.meta.epg_source != value:
                    ch.meta.epg_source = value
                    touched_uids.add(uid)
                continue
            if field == 'epg_alternative_names':
                alts = [x for x in value.split("\x1f") if x]
                if ch.meta.epg_alternative_names != alts:
                    ch.meta.epg_alternative_names = alts
                    touched_uids.add(uid)
                continue
            if field not in EPG_ALLOWED_META_FIELDS:
                continue
            if getattr(ch.meta, field) != value:
                setattr(ch.meta, field, value)
                touched_uids.add(uid)
        modified = 0
        for uid in touched_uids:
            ch = uid_map.get(uid)
            if ch is None:
                continue
            ch.update_extinf()
            ch.touch()
            modified += 1

        if modified:
            if not silent:
                tab.undo_manager.save_state(
                    tab.all_channels,
                    "Применение EPG-метаданных")
            tab._commit(reset_view=False, save_state=False)
            if not silent:
                info_box(tab,
                         f"Обновлено {modified} "
                         f"{channels_word(modified)}",
                         "Готово")
        else:
            if not silent:
                info_box(tab, "Метаданные не изменились "
                              "(нет совпадений или "
                              "всё уже актуально).", "EPG")

    def on_error(msg):
        if tab._epg_meta_worker is worker:
            tab._epg_meta_worker = None
        w2 = tab.parent_window
        if w2 is not None and not silent:
            w2.hide_progress()
        if _is_qobject_valid(tab) and not silent:
            error_box(tab, msg, "Ошибка EPG")

    worker.progress.connect(
        on_progress, Qt.ConnectionType.QueuedConnection)
    worker.applied.connect(
        on_applied, Qt.ConnectionType.QueuedConnection)
    worker.error.connect(
        on_error, Qt.ConnectionType.QueuedConnection)
    worker.start()
    return True


def mark_channel_status(tab, uid: int, ok: bool) -> None:
    if not _is_qobject_valid(tab):
        return
    ch = next((c for c in tab.all_channels if c.uid == uid),
              None)
    if ch is None:
        return
    ch.status.url_status = bool(ok)
    ch.status.url_check_time = datetime.now()
    ch.status.link_quality = (
        LinkQuality.WORKING if ok else LinkQuality.NOT_WORKING)
    from constants import StatusText
    ch.status.status_text = (
        StatusText.WORKING if ok else StatusText.NOT_WORKING)
    ch.touch()

    with tab._suppress_save():
        try:
            for r in range(tab.model.rowCount()):
                c = tab.model.channel_at(r)
                if c is not None and c.uid == uid:
                    tab.model.refresh_row(r)
                    break
        except Exception:
            logger.exception("refresh_row")

    if ch.link.url and tab.core.cache_manager is not None:
        try:
            tab.core.cache_manager.save_check_result(
                ch.meta.name, ch.link.url, bool(ok),
                ch.status.link_response_time or 0.0,
                ch.status.status_text,
                ch.status.status_code,
                ch.status.ttfb_ms,
                ch.status.resolution or "")
        except Exception:
            logger.exception("save_check_result")

    tab.core.bump_tab_revision(tab.tab_id)
    tab.update_info()


def apply_resolution_from_player(tab, uid: int,
                                  w: int, h: int) -> None:
    if not _is_qobject_valid(tab):
        return
    if w <= 0 or h <= 0:
        return
    ch = next((c for c in tab.all_channels
               if c.uid == uid), None)
    if ch is None:
        return
    from ffprobe_utils import normalize_resolution
    new_res = normalize_resolution(w, h)
    if not new_res:
        return
    if ch.status.resolution == new_res:
        return
    old_res = ch.status.resolution
    ch.status.resolution = new_res
    row = tab.model.row_of_uid(uid)
    if row >= 0:
        tab.model.refresh_row(row)
    tab.update_info()
    if ch.link.url and tab.core.cache_manager is not None:
        try:
            tab.core.cache_manager.save_check_result(
                ch.meta.name, ch.link.url, True,
                ch.status.link_response_time or 0.0,
                ch.status.status_text,
                ch.status.status_code,
                ch.status.ttfb_ms, new_res)
        except Exception:
            logger.exception("save_check_result (mpv)")
    logger.info(
        "mpv: разрешение «%s»: %s → %s (%dx%d)",
        ch.meta.name or "?",
        old_res or "(пусто)", new_res, w, h)


def start_background_resolution(tab,
                                 channels: List[ChannelData]) -> None:
    if not _is_qobject_valid(tab):
        return
    if (tab._bg_resolution_worker is not None
            and tab._bg_resolution_worker.isRunning()):
        return
    from ffprobe_utils import is_ffprobe_available
    if not is_ffprobe_available():
        logger.info("ffprobe недоступен — фоновое "
                    "определение разрешения пропущено")
        return

    worker = BackgroundResolutionWorker(
        channels, max_workers=3,
        timeout=FFPROBE_TIMEOUT_SEC, delay_ms=150)
    if not worker._items:
        return
    tab._bg_resolution_worker = worker

    def on_progress(value, total, text):
        w = tab.parent_window
        if w is not None:
            w.show_progress(value, total, text)

    def on_done():
        if tab._bg_resolution_worker is worker:
            tab._bg_resolution_worker = None
        tab._resolution_timer.stop()
        drain_resolution_queue(tab)
        w = tab.parent_window
        if w is not None:
            w.hide_progress()

    worker.progress.connect(
        on_progress, Qt.ConnectionType.QueuedConnection)
    worker.worker_done.connect(
        on_done, Qt.ConnectionType.QueuedConnection)
    worker.start()
    tab._resolution_timer.start()


def drain_resolution_queue(tab) -> None:
    if not _is_qobject_valid(tab):
        return
    changed = False
    while True:
        try:
            uid, res = tab._resolution_queue.get_nowait()
        except queue.Empty:
            break
        ch = next(
            (c for c in tab.all_channels if c.uid == uid),
            None)
        if ch is None:
            continue
        if ch.status.resolution:
            continue
        ch.status.resolution = res
        row = tab.model.row_of_uid(uid)
        if row >= 0:
            tab.model.refresh_row(row)
        changed = True
        if ch.link.url and tab.core.cache_manager is not None:
            try:
                tab.core.cache_manager.save_check_result(
                    ch.meta.name, ch.link.url, True,
                    ch.status.link_response_time or 0.0,
                    ch.status.status_text,
                    ch.status.status_code,
                    ch.status.ttfb_ms, res)
            except Exception:
                logger.exception("save_check_result (bg)")
    if changed:
        tab.update_info()


def check_urls_async(tab, channels: List[ChannelData]):
    if not _is_qobject_valid(tab):
        return
    worker_existing = getattr(tab, '_src_check_worker', None)
    if (worker_existing is not None
            and worker_existing.isRunning()):
        info_box(tab, "Проверка уже запущена")
        return
    to_check = [ch for ch in channels if ch.has_valid_url]
    if not to_check:
        info_box(tab, "Нет ссылок для проверки")
        return

    if tab.core.config.get('limit_check_enabled', False):
        try:
            limit = int(tab.core.config.get(
                'max_channels_to_check', 2000))
        except (ValueError, TypeError):
            limit = 2000
        if len(to_check) > limit:
            if not confirm(
                    tab,
                    f"Будет проверено только {limit} из "
                    f"{len(to_check)} каналов (лимит в "
                    f"«Настройки → Общие»). Продолжить?"):
                return
            to_check = to_check[:limit]

    from workers import SourceUrlCheckWorker, SourceCheckConfig
    settings = tab.core.get_replacement_settings()
    w = tab.parent_window
    if w is not None:
        w.set_action(
            f"⏳ Проверка ссылок: {len(to_check)} каналов")

    timeout = max(int(settings.check_timeout or 3), 3)
    check_cfg = SourceCheckConfig.from_app_config(
        tab.core.config)
    check_cfg = SourceCheckConfig(
        max_workers=check_cfg.max_workers,
        timeout=timeout,
        trust_sec=0,
        batch_size=check_cfg.batch_size,
        verify_ssl=check_cfg.verify_ssl,
    )
    settings_snapshot = tab.core._build_settings_snapshot()
    try:
        bl_index = tab.core.blacklist_manager.build_index()
    except Exception:
        logger.exception("build_index")
        bl_index = None

    worker = SourceUrlCheckWorker(
        source_name="__playlist__",
        channels=to_check,
        cache_manager=tab.core.cache_manager,
        check_config=check_cfg,
        stop_token=None,
        settings_snapshot=settings_snapshot,
        blacklist_index=bl_index,
        enable_ffprobe=False,
    )
    tab._src_check_worker = worker

    def on_progress(name, cur, tot):
        w2 = tab.parent_window
        if w2 is not None:
            w2.show_progress(cur, max(1, tot),
                              f"Проверено: {cur}/{tot}")

    def on_done(name, working, total):
        if getattr(tab, '_src_check_worker', None) is worker:
            tab._src_check_worker = None
        w2 = tab.parent_window
        if w2 is not None:
            w2.hide_progress()
        if not _is_qobject_valid(tab):
            return
        with tab._suppress_save():
            tab.undo_manager.save_state(
                tab.all_channels, "Проверка ссылок")
        tab.sync_to_core()
        with tab._suppress_save():
            tab.model.refresh_all()
        tab.core.bump_tab_revision(tab.tab_id)
        tab.update_info()
        info_box(
            tab,
            f"Проверка завершена.\n"
            f"Работают: {working} | "
            f"Не работают: {total - working} | "
            f"Всего проверено: {total}",
            "Проверка ссылок")
        if not worker.is_stopped():
            start_background_resolution(tab, to_check)

    def on_error(msg):
        if getattr(tab, '_src_check_worker', None) is worker:
            tab._src_check_worker = None
        w2 = tab.parent_window
        if w2 is not None:
            w2.hide_progress()
        if _is_qobject_valid(tab):
            error_box(tab, msg, "Ошибка проверки")

    worker.source_check_progress.connect(on_progress)
    worker.source_check_done.connect(on_done)
    worker.error.connect(on_error)
    worker.start()


def check_selected_urls(tab):
    if not tab.selected_channels:
        info_box(tab, "Не выбрано ни одного канала")
        return
    with_urls = [ch for ch in tab.selected_channels
                 if ch.has_valid_url]
    if not with_urls:
        info_box(tab, "У выбранных каналов нет ссылок")
        return
    check_urls_async(tab, with_urls)


def check_all_urls(tab):
    with_urls = [ch for ch in tab.all_channels
                 if ch.has_valid_url]
    if not with_urls:
        info_box(tab, "Нет каналов со ссылками")
        return
    if not confirm(tab,
                   f"Проверить {len(with_urls)} "
                   f"{links_word(len(with_urls))}?"):
        return
    check_urls_async(tab, with_urls)


def check_single_url(tab, row: int):
    ch = tab.channel_for_row(row)
    if ch and ch.link.url:
        check_urls_async(tab, [ch])