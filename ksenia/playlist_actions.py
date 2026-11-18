# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Действия над каналами вкладки плейлиста и контекстное меню."""
from __future__ import annotations

from contextlib import suppress
from typing import Callable, List, Optional, Set

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QMenu,
    QVBoxLayout)

from constants import DEFAULT_GROUP
from dialogs import _is_qobject_valid, make_action
from models import ChannelData
from paths import confirm, info_box, logger, warn_box
from utils import URLUtils
from plural_ru import channels_word, links_word


OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok |
                QDialogButtonBox.StandardButton.Cancel)


def has_any_metadata(ch: ChannelData) -> bool:
    return bool(ch.meta.tvg_id or ch.meta.tvg_logo
                or ch.meta.tvg_name
                or (ch.meta.group
                    and ch.meta.group != DEFAULT_GROUP)
                or ch.link.user_agent)


def delete_by_predicate(tab, predicate: Callable[[ChannelData], bool],
                        description: str, confirm_msg: str) -> int:
    targets = [ch for ch in tab.all_channels if predicate(ch)]
    if not targets:
        return 0
    if not confirm(tab, confirm_msg):
        return 0
    del_uids = {ch.uid for ch in targets}
    with tab._edit_channels(description):
        tab.all_channels[:] = [ch for ch in tab.all_channels
                                if ch.uid not in del_uids]
        tab._normalize_order()
    return len(targets)


def delete_channel(tab, row: int = -1):
    if row == -1:
        if tab.current_channel:
            uids = {tab.current_channel.uid}
        else:
            return
    else:
        ch = tab.channel_for_row(row)
        if ch is None:
            return
        uids = {ch.uid}

    n = len(uids)
    if n == 1:
        ch = next((c for c in tab.all_channels
                   if c.uid in uids), None)
        msg = (f"Удалить канал '{ch.meta.name}'?" if ch
               else "Удалить канал?")
    else:
        msg = f"Удалить выбранные {n} " \
              f"{channels_word(n)}?"
    if not confirm(tab, msg):
        return
    tab.save_state("Удаление канала")
    tab.all_channels = [ch for ch in tab.all_channels
                         if ch.uid not in uids]
    tab._normalize_order()
    tab.sync_to_core()
    new_uids = {ch.uid for ch in tab.all_channels}
    tab.selected_channels = [
        ch for ch in tab.selected_channels
        if ch.uid in new_uids]
    if (tab.current_channel is not None
            and tab.current_channel.uid not in new_uids):
        tab.current_channel = None
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)
    tab.groups_changed.emit()


def delete_selected_channels(tab):
    if not tab.selected_channels:
        return
    uids = {ch.uid for ch in tab.selected_channels}
    n = len(uids)
    if n == 1:
        ch = next((c for c in tab.all_channels
                   if c.uid in uids), None)
        msg = (f"Удалить канал '{ch.meta.name}'?" if ch
               else "Удалить канал?")
    else:
        msg = f"Удалить выбранные {n} " \
              f"{channels_word(n)}?"
    if not confirm(tab, msg):
        return
    tab.save_state("Удаление канала")
    tab.all_channels = [ch for ch in tab.all_channels
                         if ch.uid not in uids]
    tab._normalize_order()
    tab.sync_to_core()
    tab.selected_channels = []
    tab.current_channel = None
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=False)
    tab.groups_changed.emit()


def delete_channels_without_urls(tab):
    n = delete_by_predicate(
        tab,
        lambda ch: not ch.has_valid_url,
        "Удаление каналов без ссылок",
        "Удалить каналы без ссылок?")
    if n:
        info_box(tab,
                 f"Удалено {n} {channels_word(n)}",
                 "Готово")


def delete_channels_without_metadata(tab):
    n = delete_by_predicate(
        tab,
        lambda ch: not has_any_metadata(ch),
        "Удаление каналов без метаданных",
        "Удалить каналы без метаданных?")
    if n:
        info_box(tab,
                 f"Удалено {n} {channels_word(n)}",
                 "Готово")


def delete_channels_without_groups(tab):
    n = delete_by_predicate(
        tab,
        lambda ch: not ch.meta.group
                   or ch.meta.group == DEFAULT_GROUP,
        "Удаление каналов без группы",
        "Удалить каналы без группы (group-title пустой "
        "или «Без группы»)?")
    if n:
        info_box(tab,
                 f"Удалено {n} {channels_word(n)}",
                 "Готово")


def clear_urls(tab, channels: List[ChannelData],
               description: str, confirm_msg: str = "",
               only_broken: bool = False) -> int:
    if only_broken:
        targets = [ch for ch in channels
                   if ch.status.url_status is False]
    else:
        targets = [ch for ch in channels if ch.has_valid_url]
    if not targets:
        return 0
    if confirm_msg and not confirm(tab, confirm_msg):
        return 0
    with tab._edit_channels(description):
        for ch in targets:
            ch.clear_url()
            ch.update_extinf()
    return len(targets)


def remove_broken_url(tab, row: int):
    ch = tab.channel_for_row(row)
    if ch is None or ch.status.url_status is not False:
        return
    clear_urls(tab, [ch], "Удаление битой ссылки",
               only_broken=True)


def remove_selected_broken_urls(tab):
    n = clear_urls(
        tab, tab.selected_channels, "Удаление битых ссылок",
        only_broken=True)
    if n:
        info_box(tab,
                 f"Удалено {n} {links_word(n)}",
                 "Готово")


def remove_all_broken_urls(tab):
    n = clear_urls(
        tab, tab.all_channels, "Удаление всех битых",
        confirm_msg="Удалить все битые ссылки?",
        only_broken=True)
    if n:
        info_box(tab,
                 f"Удалено {n} {links_word(n)}",
                 "Готово")


def remove_selected_urls(tab):
    if not tab.selected_channels:
        return
    n = clear_urls(
        tab, tab.selected_channels,
        "Удаление ссылок у выбранных",
        confirm_msg=f"Удалить ссылки у "
                    f"{len(tab.selected_channels)} "
                    f"{channels_word(len(tab.selected_channels))}?")
    if n:
        info_box(tab,
                 f"Удалено {n} {links_word(n)}",
                 "Готово")


def remove_all_urls(tab):
    if not tab.all_channels:
        return
    n = clear_urls(
        tab, tab.all_channels, "Удаление всех ссылок",
        confirm_msg=f"Удалить все ссылки из "
                    f"{len(tab.all_channels)}?")
    if n:
        info_box(tab,
                 f"Удалено {n} {links_word(n)}",
                 "Готово")


def new_channel(tab):
    tab.save_state("Создание нового канала")
    ch = ChannelData()
    ch.meta.name = "Новый канал"
    ch.meta.group = DEFAULT_GROUP
    ch.original_index = -1
    ch.update_extinf()
    tab.all_channels.append(ch)
    tab._normalize_order()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)
    row = tab.model.row_of_uid(ch.uid)
    if row >= 0:
        tab.table.setCurrentIndex(tab.model.index(row, 1))
        tab.table.edit(tab.model.index(row, 1))
    tab.groups_changed.emit()


def move_channel_up(tab, row: int = -1):
    ch = (tab.current_channel if row == -1
          else tab.channel_for_row(row))
    if ch is None:
        return
    try:
        idx = tab.all_channels.index(ch)
    except ValueError:
        return
    if idx > 0:
        with tab._edit_channels("Перемещение вверх"):
            (tab.all_channels[idx],
             tab.all_channels[idx - 1]) = \
                (tab.all_channels[idx - 1],
                 tab.all_channels[idx])
            tab._normalize_order()


def move_channel_down(tab, row: int = -1):
    ch = (tab.current_channel if row == -1
          else tab.channel_for_row(row))
    if ch is None:
        return
    try:
        idx = tab.all_channels.index(ch)
    except ValueError:
        return
    if idx < len(tab.all_channels) - 1:
        with tab._edit_channels("Перемещение вниз"):
            (tab.all_channels[idx],
             tab.all_channels[idx + 1]) = \
                (tab.all_channels[idx + 1],
                 tab.all_channels[idx])
            tab._normalize_order()


def move_selected_up(tab):
    if not tab.selected_channels:
        return
    indices = []
    for ch in tab.selected_channels:
        try:
            indices.append(tab.all_channels.index(ch))
        except ValueError:
            continue
    if not indices:
        return
    indices.sort()
    tab.save_state("Перемещение вверх")
    for i, idx in enumerate(indices):
        real_idx = idx - i
        if real_idx > 0:
            (tab.all_channels[real_idx],
             tab.all_channels[real_idx - 1]) = \
                (tab.all_channels[real_idx - 1],
                 tab.all_channels[real_idx])
    tab._normalize_order()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)


def move_selected_down(tab):
    if not tab.selected_channels:
        return
    indices = []
    for ch in tab.selected_channels:
        try:
            indices.append(tab.all_channels.index(ch))
        except ValueError:
            continue
    if not indices:
        return
    indices.sort(reverse=True)
    tab.save_state("Перемещение вниз")
    last = len(tab.all_channels) - 1
    for i, idx in enumerate(indices):
        real_idx = idx + i
        if real_idx < last:
            (tab.all_channels[real_idx],
             tab.all_channels[real_idx + 1]) = \
                (tab.all_channels[real_idx + 1],
                 tab.all_channels[real_idx])
    tab._normalize_order()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)


def on_model_request_move(tab, uids: List[int],
                          ref_uid: Optional[int],
                          insert_after: int = 0):
    if not uids:
        return
    moving_set = set(uids)
    moving = [ch for ch in tab.all_channels
              if ch.uid in moving_set]
    if not moving:
        return
    if ref_uid is not None and ref_uid in moving_set:
        return

    ref_channel = None
    if ref_uid is not None:
        ref_channel = next(
            (ch for ch in tab.all_channels
             if ch.uid == ref_uid), None)

    if tab._state_save_timer.isActive():
        tab._state_save_timer.stop()
        tab._do_save_state()

    remaining = [ch for ch in tab.all_channels
                 if ch.uid not in moving_set]

    if ref_channel is not None:
        if ref_channel not in remaining:
            return
        ref_idx = remaining.index(ref_channel)
        insert_idx = ref_idx + (1 if insert_after else 0)
    else:
        insert_idx = len(remaining)

    tab.all_channels = (remaining[:insert_idx] + moving
                        + remaining[insert_idx:])

    h = tab.table.horizontalHeader()
    if h.sortIndicatorSection() != 0:
        h.blockSignals(True)
        h.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        h.blockSignals(False)

    tab._normalize_order()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)

    tab._pending_state_desc = \
        "Перемещение каналов (перетаскивание)"
    tab._do_save_state()

    tab._restore_selection(
        moving_set, None,
        tab.table.verticalScrollBar().value())


def rename_groups(tab):
    if not tab.selected_channels:
        return
    dlg = QDialog(tab)
    dlg.setWindowTitle("Пакетное переименование")
    dlg.resize(400, 200)
    l = QVBoxLayout(dlg)
    l.addWidget(QLabel(
        f"Выбрано: {len(tab.selected_channels)}"))
    groups = {ch.meta.group for ch in tab.selected_channels}
    cur = (next(iter(groups)) if len(groups) == 1
           else "РАЗНЫЕ")
    l.addWidget(QLabel(f"<b>{cur}</b>"))
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    l.addWidget(sep)
    l.addWidget(QLabel("Новая группа:"))
    edit = QLineEdit()
    if len(groups) == 1:
        edit.setText(cur)
    edit.setPlaceholderText("Введите название")
    l.addWidget(edit)
    bb = QDialogButtonBox(OK_CANCEL_BB)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    l.addWidget(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    ng = edit.text().strip()
    if not ng or ng == cur:
        return
    with tab._edit_channels("Переименование групп"):
        for ch in tab.selected_channels:
            ch.meta.group = ng
            ch.update_extinf()


def copy_channel(tab):
    w = tab.parent_window
    if w is None:
        return
    if len(tab.selected_channels) > 1:
        copy_selected_channels(tab)
        w.set_action(
            f"✓ Скопировано каналов: "
            f"{len(tab.selected_channels)}")
    elif tab.current_channel is not None:
        w.copied_channel = tab.current_channel.copy()
        w.copied_channels = None
        w.set_action("✓ Скопирован 1 канал")
    else:
        w.copied_channel = None
        w.copied_channels = None


def cut_channel(tab):
    if not tab.current_channel and not tab.selected_channels:
        return
    if len(tab.selected_channels) > 1:
        copy_selected_channels(tab)
        delete_selected_channels(tab)
        return
    if not tab.current_channel:
        return
    target = tab.current_channel
    copy_channel(tab)
    uids = {target.uid}
    if not confirm(tab,
                   f"Вырезать канал '{target.meta.name}'?"):
        return
    tab.save_state("Вырезание канала")
    tab.all_channels = [ch for ch in tab.all_channels
                         if ch.uid not in uids]
    tab._normalize_order()
    tab.sync_to_core()
    tab.selected_channels = []
    tab.current_channel = None
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=False)
    tab.groups_changed.emit()


def cut_selected_channels(tab):
    if tab.selected_channels:
        copy_selected_channels(tab)
        delete_selected_channels(tab)


def copy_selected_channels(tab):
    w = tab.parent_window
    if tab.selected_channels and w is not None:
        w.copied_channels = [ch.copy()
                              for ch in tab.selected_channels]
        w.copied_channel = None


def paste_channel(tab, row: int = -1):
    w = tab.parent_window
    if w is None:
        return

    if w.copied_channels:
        paste_selected_channels(tab, row)
        return

    if not w.copied_channel:
        return

    tab.save_state("Вставка канала")
    ch = w.copied_channel.copy()
    ch.original_index = -1

    if (ch.link.url
            and not tab.core.config.get(
                'allow_duplicate_urls', False)):
        for existing in tab.all_channels:
            if existing.link.url == ch.link.url:
                warn_box(
                    tab,
                    f"Ссылка уже используется каналом "
                    f"«{existing.meta.name}».\n\n"
                    f"Включите «Разрешать одинаковые ссылки "
                    f"у разных каналов» в общих настройках, "
                    f"если это нужно.")
                return

    ref_channel = (tab.channel_for_row(row)
                   if row >= 0 else None)
    if ref_channel is None:
        ref_channel = tab.current_channel

    if (ref_channel is not None
            and ref_channel in tab.all_channels):
        idx = tab.all_channels.index(ref_channel) + 1
        tab.all_channels.insert(idx, ch)
    else:
        tab.all_channels.append(ch)

    tab._normalize_order()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)
    tab.groups_changed.emit()


def paste_selected_channels(tab, row: int = -1):
    w = tab.parent_window
    if w is None or not w.copied_channels:
        return
    tab.save_state("Вставка каналов")

    ref_channel = (tab.channel_for_row(row)
                   if row >= 0 else None)
    if ref_channel is None:
        ref_channel = tab.current_channel

    if (ref_channel is not None
            and ref_channel in tab.all_channels):
        base_idx = tab.all_channels.index(ref_channel) + 1
    else:
        base_idx = len(tab.all_channels)

    allow_dup = bool(tab.core.config.get(
        'allow_duplicate_urls', False))
    if not allow_dup:
        existing_urls = {
            ch.link.url for ch in tab.all_channels
            if ch.link.url
        }
        conflicts: List[str] = []
        for ch in w.copied_channels:
            url = ch.link.url
            if url and url in existing_urls:
                conflicts.append(
                    f"«{ch.meta.name}» → {url[:60]}")
        if conflicts:
            warn_box(
                tab,
                "В буфере есть каналы со ссылками, которые "
                "уже используются в этом плейлисте:\n\n"
                + "\n".join(conflicts[:10])
                + (f"\n… и ещё {len(conflicts) - 10}"
                   if len(conflicts) > 10 else "")
                + "\n\nВключите «Разрешать одинаковые ссылки "
                  "у разных каналов» в общих настройках, "
                  "если это нужно.")
            return

    for offset, ch in enumerate(w.copied_channels):
        new = ch.copy()
        new.original_index = -1
        tab.all_channels.insert(base_idx + offset, new)

    tab._normalize_order()
    tab.sync_to_core()
    with tab._suppress_save():
        tab._rebuild_model_safely(preserve_selection=True)
    tab.groups_changed.emit()

    w.set_action(
        f"✓ Вставлено каналов: {len(w.copied_channels)}")


def show_context_menu(tab, position):
    menu = QMenu(tab)
    rows = sorted({idx.row() for idx in
                   tab.table.selectionModel().selectedRows()})
    if rows:
        if len(rows) == 1:
            build_single_row_menu(tab, menu, rows[0])
        else:
            build_multi_row_menu(tab, menu, set(rows))
    else:
        build_empty_menu(tab, menu)
    menu.exec(tab.table.mapToGlobal(position))


def build_single_row_menu(tab, menu: QMenu, row: int):
    ch = tab.channel_for_row(row)
    if ch:
        if ch.has_valid_url:
            menu.addAction(make_action(
                menu, "▶ Смотреть в проигрывателе",
                lambda r=row: tab._play_in_player(r)))
            menu.addSeparator()
        menu.addAction(make_action(
            menu, "Редактировать User Agent...",
            lambda r=row: tab._edit_user_agent(r)))
        if ch.link.alternative_urls:
            alt = menu.addMenu(
                f"Альтернативные ссылки "
                f"({len(ch.link.alternative_urls)})")
            for i, u in enumerate(
                    ch.link.alternative_urls[:10]):
                a = make_action(
                    alt, f"{i + 1}. {u[:60]}...",
                    lambda checked=False, uu=u, cc=ch:
                    tab._apply_alternative_url(cc, uu))
                alt.addAction(a)
        menu.addSeparator()
        if ch.status.url_status is False:
            menu.addAction(make_action(
                menu, "Удалить битую ссылку",
                lambda r=row: tab._remove_broken_url(r)))
            menu.addSeparator()
        src_ch = tab.find_channel_by_ref(ch)
        if src_ch is not None and src_ch.has_valid_url:
            host = URLUtils.extract_host(src_ch.link.url)
            if host:
                is_ip = URLUtils.is_ip_address(host)
                label = (
                    f"🚫 Заблокировать IP «{host}»"
                    if is_ip
                    else f"🚫 Заблокировать домен «{host}»")
                menu.addAction(make_action(
                    menu, label,
                    lambda c=src_ch:
                    tab.block_domain_from_channel(c)))
            menu.addSeparator()
    menu.addAction(make_action(menu, "Новый канал",
                                tab._new_channel))
    menu.addAction(make_action(menu, "Копировать канал",
                                tab._copy_channel))
    menu.addAction(make_action(menu, "Вырезать канал",
                                tab._cut_channel))
    menu.addAction(make_action(
        menu, "Вставить канал",
        lambda r=row: tab._paste_channel(r)))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Пакетное переименование групп",
        tab._rename_groups))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Добавить в чёрный список каналов",
        lambda r=row: tab._add_to_blacklist(r)))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Проверить ссылку",
        lambda r=row: tab._check_single_url(r)))
    menu.addAction(make_action(
        menu, "Заменить ссылку из источников...",
        lambda r=row: tab.replace_single_link(r)))
    if ch and ch.has_valid_url:
        menu.addAction(make_action(
            menu, "🎬 Определить разрешение (ffprobe)",
            lambda r=row: tab._detect_resolution_for_row(r)))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Удалить канал",
        lambda r=row: tab._delete_channel(r)))


def build_multi_row_menu(tab, menu: QMenu, rows: Set[int]):
    cnt = len(rows)
    a = QAction(f"Выбрано каналов: {cnt}", menu)
    a.setEnabled(False)
    menu.addAction(a)
    menu.addSeparator()
    menu.addAction(make_action(menu, "Новый канал",
                                tab._new_channel))
    menu.addAction(make_action(
        menu, f"Копировать каналы ({cnt})",
        tab._copy_selected_channels))
    menu.addAction(make_action(
        menu, f"Вырезать каналы ({cnt})",
        tab._cut_selected_channels))
    menu.addAction(make_action(
        menu, f"Вставить каналы ({cnt})",
        lambda: tab._paste_selected_channels(min(rows))))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Пакетное переименование групп",
        tab._rename_groups))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, f"Удалить выбранные ({cnt})",
        tab._delete_selected_channels))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, f"Проверить ссылки ({cnt})",
        tab._check_selected_urls))
    menu.addAction(make_action(
        menu, f"Заменить ссылки из источников ({cnt})...",
        tab.replace_selected_links))
    menu.addAction(make_action(
        menu, f"Добавить в чёрный список каналов ({cnt})",
        tab._add_selected_to_blacklist))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, f"🎬 Определить разрешение ({cnt})...",
        tab._detect_resolution_selected))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, f"Удалить битые ссылки ({cnt})",
        tab._remove_selected_broken_urls))
    menu.addAction(make_action(
        menu, f"Удалить все ссылки ({cnt})",
        tab._remove_selected_urls))


def build_empty_menu(tab, menu: QMenu):
    menu.addAction(make_action(
        menu, "🎬 Определить разрешение у всех...",
        tab._detect_resolution_all))
    menu.addSeparator()
    menu.addAction(make_action(menu, "Новый канал",
                                tab._new_channel))
    menu.addAction(make_action(menu, "Вставить канал",
                                tab._paste_channel))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Пакетное переименование групп",
        tab._rename_groups))
    menu.addSeparator()
    menu.addAction(make_action(
        menu, "Удалить дубликаты...",
        tab.show_duplicate_finder))
    menu.addAction(make_action(
        menu, "Сравнить плейлисты...",
        tab.show_compare))
    menu.addSeparator()
    menu.addAction(make_action(
        menu,
        "Применить метаданные из EPG ко всем каналам",
        lambda: tab.apply_epg_metadata_to_all(silent=False)))