# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Замена ссылок, ЧС, UA и метаданные для вкладки плейлиста."""
from __future__ import annotations

from contextlib import suppress
from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QVBoxLayout)

from constants import (
    DEFAULT_GROUP, REPLACEMENT_MAX_WORKERS_DEFAULT, StatusText)
from dialogs import (
    BlockDomainDialog, LinkSelectionDialog, _is_qobject_valid)
from logging_setup import logger
from models import ChannelData
from paths import confirm, confirm_three, error_box, info_box, warn_box
from plural_ru import channels_word, replaced_verb
from utils import URLUtils, sanitize_user_agent
from workers import LinkReplacementWorker


OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)


def has_meta_to_remove(ch: ChannelData,
                       opts: Dict[str, bool]) -> bool:
    checks = (
        ('tvg_id', bool(ch.meta.tvg_id)),
        ('tvg_logo', bool(ch.meta.tvg_logo)),
        ('tvg_name', bool(ch.meta.tvg_name)),
        ('group_title', bool(
            ch.meta.group
            and ch.meta.group != DEFAULT_GROUP)),
        ('user_agent', bool(ch.link.user_agent)),
    )
    return any(opts.get(k) and has for k, has in checks)


def block_domain_from_channel(tab, channel: ChannelData):
    if not channel:
        info_box(tab, "Канал не выбран")
        return
    target = tab.find_channel_by_ref(channel)
    if target is None:
        info_box(tab, f"Канал «{channel.meta.name}» "
                      f"не найден в плейлисте.")
        return
    if not target.has_valid_url:
        info_box(tab, f"У канала «{target.meta.name}» "
                      f"нет ссылки.")
        return
    host = URLUtils.extract_host(target.link.url)
    if not host:
        info_box(tab, f"Не удалось определить домен/IP "
                      f"из ссылки:\n"
                      f"{target.link.url[:120]}")
        return

    dlg = BlockDomainDialog(host, tab.all_channels, tab)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    value, include, note, selected = dlg.get_result()
    if not value:
        return
    added = tab.core.add_domain_to_blacklist(
        value, include_subdomains=include, note=note)
    if not selected and not added:
        info_box(tab,
                 f"Правило для «{value}» уже существует, "
                 f"совпадений нет.",
                 "Готово")
    elif not selected:
        info_box(tab,
                 f"Правило «{value}» добавлено. "
                 f"Совпадений в плейлисте не найдено.",
                 "Готово")
    else:
        clean_blocked_by_domain(tab)
        info_box(tab,
                 f"Правило «{value}» добавлено.\n"
                 f"Очищено ссылок: {len(selected)}.\n"
                 f"Каналы сохранены с метаданными.",
                 "Готово")


def clean_blocked_by_domain(tab) -> int:
    if not tab.all_channels:
        return 0
    mgr = tab.core.domain_blacklist_manager
    if not mgr.get_all():
        return 0
    blocked_count = sum(
        1 for ch in tab.all_channels
        if ch.link.url and mgr.matches_url(ch.link.url))
    if blocked_count == 0:
        return 0
    tab.flush_pending_state()
    tab.save_state("Очистка ссылок по чёрному списку "
                    "доменов/IP")
    _, cleaned = tab.core.apply_domain_blacklist_clean(
        tab.all_channels)
    tab.sync_to_core()
    with tab._suppress_save():
        tab.model.refresh_all()
    tab.core.bump_tab_revision(tab.tab_id)
    tab.update_info()
    return cleaned


def add_to_blacklist(tab, row: int = -1):
    if row == -1:
        if not tab.current_channel:
            return
        ch = tab.current_channel
    else:
        ch = tab.channel_for_row(row)
        if ch is None:
            return
    dlg = QDialog(tab)
    dlg.setWindowTitle("Добавить в чёрный список каналов")
    dlg.resize(400, 200)
    l = QVBoxLayout(dlg)
    l.addWidget(QLabel(f"Канал: {ch.meta.name}"))
    l.addWidget(QLabel(f"TVG-ID: {ch.meta.tvg_id}"))
    g = QGroupBox("Параметры")
    gl = QVBoxLayout(g)
    nc = QCheckBox("По названию")
    nc.setChecked(True)
    gl.addWidget(nc)
    tc = QCheckBox("По TVG-ID")
    tc.setChecked(bool(ch.meta.tvg_id))
    gl.addWidget(tc)
    l.addWidget(g)
    bb = QDialogButtonBox(OK_CANCEL_BB)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    l.addWidget(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    name = ch.meta.name if nc.isChecked() else ""
    tid = ch.meta.tvg_id if tc.isChecked() else ""
    if not name and not tid:
        return
    tab.core.add_to_blacklist(name, tid)


def add_selected_to_blacklist(tab):
    if not tab.selected_channels:
        return
    chs = tab.selected_channels.copy()
    dlg = QDialog(tab)
    dlg.setWindowTitle("Добавить в чёрный список каналов")
    dlg.resize(450, 300)
    l = QVBoxLayout(dlg)
    l.addWidget(QLabel(f"Добавить {len(chs)} "
                       f"{channels_word(len(chs))}:"))
    lw = QListWidget()
    for c in chs[:100]:
        lw.addItem(f"{c.meta.name} [{c.meta.tvg_id}]"
                   if c.meta.tvg_id else c.meta.name)
    if len(chs) > 100:
        lw.addItem(f"... и ещё {len(chs) - 100}")
    l.addWidget(lw)
    g = QGroupBox("Параметры")
    gl = QVBoxLayout(g)
    nc = QCheckBox("По названию")
    nc.setChecked(True)
    gl.addWidget(nc)
    tc = QCheckBox("По TVG-ID")
    tc.setChecked(True)
    gl.addWidget(tc)
    l.addWidget(g)
    bb = QDialogButtonBox(OK_CANCEL_BB)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    l.addWidget(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    if not nc.isChecked() and not tc.isChecked():
        return
    added = 0
    for c in chs:
        name = c.meta.name if nc.isChecked() else ""
        tid = (c.meta.tvg_id
               if tc.isChecked() and c.meta.tvg_id else "")
        if not name and not tid:
            continue
        if tab.core.add_to_blacklist(name, tid):
            added += 1
    if added > 0:
        info_box(tab,
                 f"Добавлено {added} {channels_word(added)}",
                 "Готово")


def edit_user_agent(tab, row: int):
    ch = tab.channel_for_row(row)
    if not ch:
        return
    new_ua, ok = QInputDialog.getText(
        tab, "Правка User Agent", "User Agent:",
        QLineEdit.EchoMode.Normal, ch.link.user_agent or "")
    if not ok:
        return
    tab.save_state("Изменение User Agent")
    ua = sanitize_user_agent(new_ua)
    ch.link.user_agent = ua
    if ua:
        ch.link.extra_headers['User-Agent'] = ua
    else:
        for k in list(ch.link.extra_headers.keys()):
            if k.lower() == 'user-agent':
                del ch.link.extra_headers[k]
    ch.update_extvlcopt_from_headers()
    ch.touch()
    tab.sync_to_core()
    for r in range(tab.model.rowCount()):
        c = tab.model.channel_at(r)
        if c is not None and c.uid == ch.uid:
            tab.model.refresh_row(r)
            break


def apply_blacklist(tab) -> int:
    filtered, removed = tab.core.apply_blacklist_to_channels(
        tab.all_channels)
    if removed > 0:
        with tab._edit_channels(
                "Применение чёрного списка каналов"):
            tab.all_channels[:] = filtered
            tab._normalize_order()
    return removed


def remove_metadata(tab, opts: Dict[str, bool],
                    scope: str = "all"):
    if scope == "current":
        if not tab.current_channel:
            return
        chs = [tab.current_channel]
    elif scope == "selected":
        if not tab.selected_channels:
            return
        chs = tab.selected_channels.copy()
    else:
        chs = tab.all_channels.copy()
    if not chs:
        return
    chs = [ch for ch in chs
           if has_meta_to_remove(ch, opts)]
    if not chs:
        info_box(tab,
                 "Нет каналов с указанными метаданными")
        return
    if not confirm(tab,
                   f"Удалить из {len(chs)} "
                   f"{channels_word(len(chs))}?"):
        return
    tab.save_state("Удаление метаданных")
    for ch in chs:
        if opts.get('tvg_id'):
            ch.meta.tvg_id = ""
        if opts.get('tvg_logo'):
            ch.meta.tvg_logo = ""
        if opts.get('tvg_name'):
            ch.meta.tvg_name = ""
        if opts.get('group_title'):
            ch.meta.group = DEFAULT_GROUP
        if opts.get('user_agent'):
            ch.link.user_agent = ""
            for k in list(ch.link.extra_headers.keys()):
                if k.lower() == 'user-agent':
                    del ch.link.extra_headers[k]
            ch.update_extvlcopt_from_headers()
        ch.update_extinf()
        ch.touch()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)
    tab.groups_changed.emit()


def apply_alternative_url(tab, channel: ChannelData, url: str):
    if not url or not url.strip():
        return
    tab.save_state("Применение альтернативной ссылки")
    if (channel.link.url and channel.link.url != url
            and channel.link.url
            not in channel.link.alternative_urls):
        channel.link.alternative_urls.append(channel.link.url)
    if url in channel.link.alternative_urls:
        channel.link.alternative_urls.remove(url)
    channel.link.url = url
    channel.link.has_url = True
    channel.link.is_repaired = False
    channel.status.reset()
    try:
        tab.core.domain_user_agent_manager\
            .apply_rules_to_channel(channel)
    except Exception:
        logger.exception("apply_rules_to_channel")
    channel.touch()
    tab.sync_to_core()
    with tab._suppress_save():
        tab.model.refresh_all()


def add_link_from_current(tab):
    if not tab.current_channel:
        return
    for r in range(tab.model.rowCount()):
        ch = tab.channel_for_row(r)
        if ch is tab.current_channel:
            add_link_from_sources(tab, r)
            return
    for r in range(tab.model.rowCount()):
        ch = tab.channel_for_row(r)
        if ch and ch.uid == tab.current_channel.uid:
            add_link_from_sources(tab, r)
            return


def add_link_from_sources(tab, row: int):
    ch = tab.channel_for_row(row)
    if ch is None:
        return
    if ch.has_valid_url:
        info_box(tab, f"'{ch.meta.name}' уже имеет ссылку.")
        return
    settings = tab.core.get_replacement_settings()
    alts = tab.core.link_source_manager.find_matches_for_channel(
        ch, settings,
        config=tab.core.config,
        only_working=False,
        epg_db=tab.core.epg_db)
    if not alts:
        info_box(tab, f"Не найдено для '{ch.meta.name}'.")
        return
    dlg = LinkSelectionDialog(ch.meta.name, alts, tab)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    selected = dlg.get_selected()
    if selected:
        do_apply_link(tab, ch, selected)


def do_apply_link(tab, channel: ChannelData,
                   source_ch: ChannelData):
    if not source_ch.has_valid_url:
        return
    if not tab.core.config.get('allow_duplicate_urls', False):
        target_url = source_ch.link.url
        for ch in tab.all_channels:
            if ch.uid != channel.uid and ch.link.url == target_url:
                warn_box(
                    tab,
                    f"Ссылка уже используется каналом "
                    f"«{ch.meta.name}».\n\n"
                    f"Включите «Разрешать одинаковые ссылки "
                    f"у разных каналов» в общих настройках, "
                    f"если это нужно.")
                return
    tab.save_state("Добавление ссылки из источников")
    if (channel.link.url
            and channel.link.url != source_ch.link.url):
        if channel.link.url not in channel.link.alternative_urls:
            channel.link.alternative_urls.append(
                channel.link.url)
    channel.link.url = source_ch.link.url
    channel.link.has_url = True
    channel.link.is_repaired = False
    channel.status.reset()
    channel.touch()
    channel.link.link_source = source_ch.link.link_source
    if source_ch.link.user_agent:
        if tab.core.should_remove_user_agent(channel.link.url):
            channel.link.user_agent = ""
            for k in list(channel.link.extra_headers.keys()):
                if k.lower() == 'user-agent':
                    del channel.link.extra_headers[k]
        else:
            channel.link.user_agent = source_ch.link.user_agent
            channel.link.extra_headers['User-Agent'] = \
                source_ch.link.user_agent
    else:
        ua = tab.core.get_user_agent_for_url(channel.link.url)
        if ua:
            channel.link.user_agent = ua
            channel.link.extra_headers['User-Agent'] = ua
    channel.update_extinf()
    channel.update_extvlcopt_from_headers()
    tab.sync_to_core()
    with tab._suppress_save():
        tab.model.refresh_all()


def replace_link_from_sources(tab, row: int):
    ch = tab.channel_for_row(row)
    if ch is None:
        return
    settings = tab.core.get_replacement_settings()
    alts = tab.core.link_source_manager.find_matches_for_channel(
        ch, settings,
        config=tab.core.config,
        only_working=False,
        epg_db=tab.core.epg_db)
    if not alts:
        info_box(tab, f"Не найдено для '{ch.meta.name}'.")
        return
    dlg = LinkSelectionDialog(ch.meta.name, alts, tab)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    selected = dlg.get_selected()
    if selected:
        do_apply_link(tab, ch, selected)


def replace_broken_links(tab):
    if not tab.all_channels:
        return
    settings = tab.core.get_replacement_settings()
    to_process = [ch for ch in tab.all_channels
                  if ch.needs_replacement(settings)]
    if not to_process:
        info_box(tab, "Нет каналов с битыми ссылками.")
        return
    replace_links_async(tab, to_process)


def replace_selected_links(tab):
    if not tab.selected_channels:
        return
    settings = tab.core.get_replacement_settings()
    candidates: List[Tuple[ChannelData,
                            List[ChannelData]]] = []
    for ch in tab.selected_channels:
        alts = tab.core.link_source_manager \
            .find_matches_for_channel(
                ch, settings,
                config=tab.core.config,
                only_working=False,
                epg_db=tab.core.epg_db)
        if alts:
            candidates.append((ch, alts))
    if not candidates:
        info_box(tab, "Не найдено альтернатив")
        return
    all_alts: List[ChannelData] = []
    seen_urls: Set[str] = set()
    for _, alts in candidates:
        for a in alts:
            if a.link.url and a.link.url not in seen_urls:
                seen_urls.add(a.link.url)
                all_alts.append(a)
    if not all_alts:
        return
    dlg = LinkSelectionDialog(
        f"{len(candidates)} каналов", all_alts, tab)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    selected = dlg.get_selected()
    if not selected:
        return
    tab.save_state("Замена выбранных ссылок")
    applied = 0
    for ch, _ in candidates:
        if (not ch.has_valid_url
                or ch.link.url == selected.link.url):
            continue
        if ch.link.url not in ch.link.alternative_urls:
            ch.link.alternative_urls.append(ch.link.url)
        ch.link.url = selected.link.url
        ch.link.has_url = True
        ch.link.is_repaired = False
        ch.status.reset()
        ch.touch()
        if selected.link.user_agent:
            ch.link.user_agent = selected.link.user_agent
            ch.link.extra_headers['User-Agent'] = \
                selected.link.user_agent
        ch.update_extinf()
        ch.update_extvlcopt_from_headers()
        applied += 1
    tab.sync_to_core()
    with tab._suppress_save():
        tab.model.refresh_all()
    info_box(tab,
             f"Заменено у {applied} "
             f"{channels_word(applied)}",
             "Готово")


def replace_single_link(tab, row: int):
    replace_link_from_sources(tab, row)


def replace_all_links(tab):
    if not tab.all_channels:
        return

    sources = tab.core.link_source_manager.get_enabled_sources()
    if not sources:
        info_box(
            tab,
            "Нет включённых источников.\n\n"
            "Откройте «Инструменты → Управление источниками» "
            "и добавьте\n"
            "хотя бы один источник. Затем нажмите "
            "«🔄 Обновить всё»,\n"
            "чтобы загрузить каналы и наполнить кэш "
            "живых ссылок.",
            "Исправление ссылок")
        return

    settings = tab.core.get_replacement_settings()
    to_process = [ch for ch in tab.all_channels
                  if ch.needs_replacement(settings)]
    if not to_process:
        info_box(tab, "Нет каналов для замены.")
        return

    if not tab.core.link_source_manager.has_alive_index():
        reply = confirm_three(
            tab,
            "Кэш живых ссылок источников пуст.\n\n"
            "Сначала для каждой битой ссылки будет "
            "выполнена автопочинка (http↔https, редиректы).\n"
            "Затем, если починка не удалась, будет "
            "использован кэш живых ссылок.\n\n"
            "Кэш пуст. Нажмите «Да», чтобы открыть "
            "менеджер источников и запустить "
            "«🔄 Обновить всё».\n"
            "«Нет» — продолжить (вероятно, только "
            "автопочинка).\n"
            "«Отмена» — прервать.",
            "Кэш пуст")
        if reply == 'yes':
            w = tab.parent_window
            if w is not None:
                w._manage_sources()
            return
        if reply == 'cancel':
            return

    replace_links_async(tab, to_process)


def replace_links_async(tab, channels: List[ChannelData]):
    if not _is_qobject_valid(tab):
        return
    if (tab._replacement_worker
            and tab._replacement_worker.isRunning()):
        info_box(tab, "Замена уже запущена")
        return
    settings = tab.core.get_replacement_settings()
    to_process = [ch for ch in channels
                  if ch.needs_replacement(settings)]
    if not to_process:
        info_box(tab, "Нет каналов для замены.")
        return
    w = tab.parent_window
    if w is not None:
        w.set_action(f"⏳ Замена ссылок: "
                      f"{len(to_process)} каналов")
    max_workers = int(tab.core.config.get(
        'replacement_max_workers',
        REPLACEMENT_MAX_WORKERS_DEFAULT))
    tab._replacement_worker = LinkReplacementWorker(
        to_process, tab.core.link_source_manager, settings,
        config=tab.core.config, max_workers=max_workers,
        epg_db=tab.core.epg_db)

    uid_map: Dict[int, ChannelData] = {
        ch.uid: ch for ch in tab.all_channels}
    repaired_uids: Set[int] = set()

    def on_channel_updated(uid: int, old_url: str,
                            new_url: str, name: str,
                            from_alive: bool):
        if not _is_qobject_valid(tab):
            return
        ch = uid_map.get(uid)
        if ch is None:
            return
        s = settings
        if s.keep_backup_links and old_url and old_url.strip():
            if old_url not in ch.link.alternative_urls:
                ch.link.alternative_urls.insert(0, old_url)
                ch.link.alternative_urls = \
                    ch.link.alternative_urls[
                        :s.max_alternative_urls]
        ch.link.url = new_url
        ch.link.has_url = True
        if uid not in repaired_uids:
            ch.link.is_repaired = False
        ch.status.reset()
        try:
            tab.core.domain_user_agent_manager\
                .apply_rules_to_channel(ch)
            ch.update_extvlcopt_from_headers()
        except Exception:
            logger.exception("apply UA after replacement")
        ch.touch()

    def on_channel_repaired(uid: int, old_url: str,
                             new_url: str):
        repaired_uids.add(uid)
        ch = uid_map.get(uid)
        if ch is None:
            return
        ch.link.is_repaired = True
        try:
            ch.status.status_text = StatusText.REPAIRED
        except Exception:
            pass
        logger.info(
            "Канал «%s»: автопочинка %s -> %s",
            ch.meta.name or "?", (old_url or "")[:60],
            (new_url or "")[:60])

    def on_progress(value, maximum, text):
        w2 = tab.parent_window
        if w2 is not None:
            w2.show_progress(value, maximum, text)

    def on_done(replaced, total):
        w2 = tab.parent_window
        if w2 is not None:
            w2.hide_progress()
        if not _is_qobject_valid(tab):
            return
        with tab._suppress_save():
            tab.undo_manager.save_state(
                tab.all_channels, "Замена ссылок")
        tab.sync_to_core()
        with tab._suppress_save():
            tab.model.refresh_all()
        tab.undo_state_changed.emit(
            tab.undo_manager.can_undo(),
            tab.undo_manager.can_redo())
        repaired_n = len(repaired_uids)
        if replaced == 0:
            info_box(
                tab,
                f"Заменено 0 из {total}.\n\n"
                "Автопочинка не смогла починить ссылки, "
                "а в кэше живых ссылок не нашлось замены.\n\n"
                "Проверьте:\n"
                "• источники обновлялись с проверкой ссылок?\n"
                "• имена каналов совпадают с именами "
                "в источниках?\n\n"
                "Откройте «Инструменты → Управление "
                "источниками» и нажмите «🔄 Обновить всё».",
                "Замена завершена")
        else:
            extra = ""
            if repaired_n:
                extra = (f"\n🔧 Автопочинка: "
                         f"{repaired_n} "
                         f"{channels_word(repaired_n)} "
                         f"починено без обращения к кэшу.")
            info_box(
                tab,
                f"{replaced_verb(replaced).capitalize()} "
                f"{replaced} из {total}.{extra}",
                "Замена завершена")
        tab._replacement_worker = None

    def on_error(msg):
        w2 = tab.parent_window
        if w2 is not None:
            w2.hide_progress()
        if _is_qobject_valid(tab):
            error_box(tab, msg, "Ошибка замены")
        tab._replacement_worker = None

    tab._replacement_worker.channel_updated.connect(
        on_channel_updated, Qt.ConnectionType.QueuedConnection)
    tab._replacement_worker.channel_repaired.connect(
        on_channel_repaired, Qt.ConnectionType.QueuedConnection)
    tab._replacement_worker.progress.connect(
        on_progress, Qt.ConnectionType.QueuedConnection)
    tab._replacement_worker.replacement_done.connect(
        on_done, Qt.ConnectionType.QueuedConnection)
    tab._replacement_worker.error.connect(
        on_error, Qt.ConnectionType.QueuedConnection)
    tab._replacement_worker.start()