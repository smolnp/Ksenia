# -*- coding: utf-8 -*-
"""Диалоги ЧС каналов, домен/IP, правил User-Agent."""
from __future__ import annotations

import weakref
from typing import Any, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QPushButton, QTextEdit, QTableWidgetItem, QVBoxLayout)

from blacklists import (DomainUserAgentManager,
                         DomainUserAgentRule)
from dialogs_base import (BaseDialog, _is_qobject_valid,
                           fill_channels_table, hint_label,
                           json_export_dialog, json_import_dialog,
                           make_table)
from logging_setup import logger
from models import ChannelData
from paths import confirm, info_box, warn_box
from theme import semantic_colors
from utils import URLUtils


class BlockDomainDialog(BaseDialog):
    def __init__(self, value: str,
                 all_channels: List[ChannelData], parent=None):
        super().__init__("Заблокировать домен/IP", parent,
                          size=(720, 460))
        self._all_channels = list(all_channels)
        self._normalized = URLUtils.normalize_host(value)
        self._is_ip = URLUtils.is_ip_address(self._normalized)
        self._current: List[ChannelData] = []

        layout = self.root
        self.header_label = QLabel()
        self.header_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.header_label)

        form = QFormLayout()
        self.value_edit = QLineEdit(self._normalized)
        self.value_edit.setReadOnly(True)
        form.addRow("Домен/IP:", self.value_edit)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(
            "Необязательная заметка")
        form.addRow("Заметка:", self.note_edit)
        layout.addLayout(form)

        self.subdomain_check = QCheckBox(
            "Включая поддомены "
            "(например, cdn.example.com для example.com)")
        layout.addWidget(self.subdomain_check)

        layout.addWidget(QLabel(
            "Будут очищены ссылки у каналов:"))
        self.preview = make_table(
            ["Название", "Группа", "URL"],
            edit_disabled=True,
            select_mode=(
                QAbstractItemView.SelectionMode.ExtendedSelection))
        layout.addWidget(self.preview)

        self.add_ok_cancel("Заблокировать и очистить ссылки")

        if self._is_ip:
            self.subdomain_check.setChecked(False)
            self.subdomain_check.setEnabled(False)
            self.subdomain_check.setToolTip(
                "Для IP-адресов поддомены не применяются")
        else:
            self.subdomain_check.setChecked(True)
        self.subdomain_check.toggled.connect(self._on_toggled)
        self._refresh()

    def _match(self, ch: ChannelData) -> bool:
        if not ch.link.url:
            return False
        if self._is_ip:
            return URLUtils.ip_matches(ch.link.url,
                                        self._normalized)
        return URLUtils.host_matches(
            ch.link.url, self._normalized,
            include_subdomains=self.subdomain_check.isChecked())

    def _on_toggled(self, _checked: bool):
        self._refresh()

    def _refresh(self):
        filtered = [ch for ch in self._all_channels
                    if self._match(ch)]
        self._current = filtered
        kind = "IP-адрес" if self._is_ip else "Домен"
        self.header_label.setText(
            f"<b>{kind}:</b> {self._normalized}<br>"
            f"<b>Будет очищено ссылок:</b> {len(filtered)} "
            f"(каналы сохранятся)")
        fill_channels_table(self.preview, filtered, max_url=120)

    def get_result(self) -> Tuple[str, bool, str, List[ChannelData]]:
        include = (False if self._is_ip
                   else self.subdomain_check.isChecked())
        return (self._normalized, include,
                self.note_edit.text().strip(),
                list(self._current))


class DomainBlacklistDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Менеджер чёрного списка домен/IP",
                          parent, size=(880, 600))
        self.core = core
        self._setup_ui()
        self._load_table()

    def _setup_ui(self):
        l = self.root
        l.addWidget(hint_label(
            "Единый чёрный список доменов и IP-адресов для "
            "ссылок.\n"
            "При срабатывании правила ссылка ОЧИЩАЕТСЯ, но канал "
            "сохраняется вместе с метаданными."))

        self.table = make_table(
            ["Значение", "Тип", "Поддомены", "Заметка", "Дата"],
            stretch_last=False,
            select_mode=(
                QAbstractItemView.SelectionMode.ExtendedSelection))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents)
        l.addWidget(self.table)

        bl = QHBoxLayout()
        for t, s in (
            ("Добавить", self._add_manual),
            ("Удалить", self._remove_selected),
            ("Очистить", self._clear_all),
            ("Экспорт", self._export_bl),
        ):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        l.addLayout(bl)
        self.add_close()

    def _load_table(self):
        items = self.core.get_domain_blacklist()
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            value = it.get('value', '')
            is_ip = bool(it.get('is_ip', False))
            inc = bool(it.get('include_subdomains', True))
            note = it.get('note', '') or ''
            ad = it.get('added_date', '') or ''

            vi = QTableWidgetItem(value)
            vi.setData(Qt.ItemDataRole.UserRole, value)
            self.table.setItem(i, 0, vi)

            type_item = QTableWidgetItem(
                "IP" if is_ip else "Домен")
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 1, type_item)

            sub_item = QTableWidgetItem(
                "—" if is_ip else ("Да" if inc else "Нет"))
            sub_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, sub_item)

            ni = QTableWidgetItem(note)
            ni.setToolTip(note)
            self.table.setItem(i, 3, ni)

            self.table.setItem(i, 4, QTableWidgetItem(ad))

    def _add_manual(self):
        raw, ok = QInputDialog.getText(
            self, "Добавить правило",
            "Домен или IP (можно вставить URL):",
            QLineEdit.EchoMode.Normal, "")
        if not ok or not raw.strip():
            return
        normalized = URLUtils.normalize_host(raw)
        if not normalized:
            warn_box(self, "Пустое значение")
            return

        all_channels: List[ChannelData] = []
        main_win = self.parent()
        cur_tab = (getattr(main_win, 'current_tab', None)
                   if main_win else None)
        if cur_tab is not None and _is_qobject_valid(cur_tab):
            all_channels = list(cur_tab.all_channels)
        else:
            for tab in self.core.get_all_tabs():
                if _is_qobject_valid(tab):
                    all_channels = list(tab.all_channels)
                    break

        if not all_channels:
            if not self.core.add_domain_to_blacklist(
                    normalized, include_subdomains=True,
                    note="Добавлено вручную"):
                info_box(
                    self,
                    f"Правило '{normalized}' уже существует.",
                    "Добавить правило")
                return
            self._load_table()
            return

        dlg = BlockDomainDialog(normalized, all_channels, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value, include, note, selected = dlg.get_result()
        if not value:
            return
        if not note:
            note = "Добавлено вручную"
        if not self.core.add_domain_to_blacklist(
                value, include_subdomains=include, note=note):
            info_box(self,
                     f"Правило '{value}' уже существует.",
                     "Добавить правило")
            return
        self._load_table()

    def _remove_selected(self):
        rows = sorted([r.row() for r in
                       self.table.selectionModel().selectedRows()],
                       reverse=True)
        if not rows:
            return
        for r in rows:
            it = self.table.item(r, 0)
            if it:
                value = (it.data(Qt.ItemDataRole.UserRole)
                         or it.text())
                self.core.remove_domain_from_blacklist(value)
        self._load_table()

    def _clear_all(self):
        if confirm(
                self,
                "Очистить чёрный список домен/IP?\n"
                "Уже очищенные ссылки не восстановятся."):
            self.core.clear_domain_blacklist()
            self._load_table()

    def _export_bl(self):
        items = self.core.get_domain_blacklist()
        if json_export_dialog(
                self, "Экспорт ч.с. домен/IP",
                "domain_blacklist.json", items):
            info_box(self,
                     f"Сохранено правил: {len(items)}",
                     "Экспорт")


class BlacklistDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Чёрный список каналов", parent,
                          size=(800, 600))
        self.core = core
        self._setup_ui()
        self._load_table()

    def _setup_ui(self):
        l = self.root
        l.addWidget(hint_label(
            "Блокировка каналов по имени или TVG-ID.\n"
            "При срабатывании канал УДАЛЯЕТСЯ из плейлиста."))
        self.table = make_table(
            ["Название", "TVG-ID", "Дата"],
            stretch_last=False,
            select_mode=(
                QAbstractItemView.SelectionMode.ExtendedSelection))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        l.addWidget(self.table)

        bl = QHBoxLayout()
        for t, s in (
            ("Добавить", self._add_manual),
            ("Удалить", self._remove_selected),
            ("Очистить", self._clear_all),
            ("Импорт", self._import_bl),
            ("Экспорт", self._export_bl),
        ):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        l.addLayout(bl)
        self.add_close()

    def _load_table(self):
        items = self.core.get_blacklist()
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            self.table.setItem(
                i, 0, QTableWidgetItem(it.get('name', '')))
            self.table.setItem(
                i, 1, QTableWidgetItem(it.get('tvg_id', '')))
            self.table.setItem(
                i, 2, QTableWidgetItem(
                    it.get('added_date', '')))

    def _add_manual(self):
        name, ok1 = QInputDialog.getText(
            self, "Добавить", "Название:")
        if not (ok1 and name and name.strip()):
            return
        tid, ok2 = QInputDialog.getText(
            self, "Добавить", "TVG-ID:")
        if not ok2:
            return
        if not self.core.add_to_blacklist(
                name.strip(), tid.strip()):
            info_box(self,
                     f"Правило уже существует: {name.strip()}",
                     "Добавить")
            return
        self._load_table()

    def _remove_selected(self):
        rows = sorted([r.row() for r in
                       self.table.selectionModel().selectedRows()],
                       reverse=True)
        for r in rows:
            n = self.table.item(r, 0)
            t = self.table.item(r, 1)
            if n and t:
                self.core.remove_from_blacklist(n.text(),
                                                 t.text())
        self._load_table()

    def _clear_all(self):
        if confirm(self, "Очистить?"):
            self.core.clear_blacklist()
            self._load_table()

    def _import_bl(self):
        def on_items(data: list) -> int:
            for it in data:
                if isinstance(it, dict):
                    self.core.add_to_blacklist(
                        it.get('name', ''),
                        it.get('tvg_id', ''))
            self._load_table()
            return len(data)
        json_import_dialog(
            self, "Импорт ЧС каналов", on_items)

    def _export_bl(self):
        json_export_dialog(
            self, "Экспорт ЧС каналов",
            "blacklist.json", self.core.get_blacklist())


class DomainUserAgentEditDialog(BaseDialog):
    def __init__(self, parent=None,
                 rule: Optional[DomainUserAgentRule] = None):
        title = ("Правило User-Agent" if not rule
                 else "Редактирование")
        super().__init__(title, parent, size=(600, 300))
        self.rule = rule
        l = self.root
        form = QFormLayout()
        self.domain_edit = QLineEdit()
        self.domain_edit.setPlaceholderText(
            "example.com или cdn.example.com (без https://)")
        form.addRow("Домен:", self.domain_edit)
        self.user_agent_edit = QTextEdit()
        self.user_agent_edit.setPlaceholderText(
            "WINK/1.40.1 (AndroidTV/9) HlsWinkPlayer\n\n"
            "Оставьте пустым, чтобы УДАЛИТЬ User-Agent для "
            "каналов с этим доменом")
        self.user_agent_edit.setMaximumHeight(120)
        form.addRow("User-Agent:", self.user_agent_edit)
        l.addLayout(form)
        l.addWidget(hint_label(
            "Пустое поле UA = удалить User-Agent для всех "
            "каналов с этим доменом.\n"
            "Домен указывайте БЕЗ схемы: example.com, "
            "а не https://example.com."))
        self.add_ok_cancel()
        if rule:
            self.domain_edit.setText(rule.domain)
            self.user_agent_edit.setPlainText(rule.user_agent)

    def get_rule(self) -> Tuple[str, str]:
        return (self.domain_edit.text().strip(),
                self.user_agent_edit.toPlainText().strip())

    def accept(self):
        raw = self.domain_edit.text().strip()
        if not raw:
            warn_box(self, "Введите домен")
            return
        normalized = URLUtils.normalize_host(raw)
        if not normalized:
            warn_box(self, "Не удалось разобрать домен")
            return
        self.domain_edit.setText(normalized)
        super().accept()


class DomainUserAgentDialog(BaseDialog):
    rules_updated = pyqtSignal()

    def __init__(self, manager: DomainUserAgentManager,
                 playlist_tab=None, parent=None):
        super().__init__("Правила User-Agent по доменам", parent,
                          size=(800, 500))
        self.manager = manager
        self._playlist_tab_ref = (weakref.ref(playlist_tab)
                                   if playlist_tab else None)
        self._loading = False
        layout = self.root
        layout.addWidget(hint_label(
            "Автоматическая установка User-Agent для каналов по "
            "домену.\n"
            "Пустое поле UA = удалить User-Agent у всех каналов "
            "с этим доменом."))

        self.rules_table = make_table(
            ["Домен", "User-Agent", "Вкл"],
            stretch_last=False,
            select_mode=(
                QAbstractItemView.SelectionMode.SingleSelection))
        h = self.rules_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.rules_table)

        bl = QHBoxLayout()
        for t, s in (
            ("Добавить", self._add),
            ("Редактировать", self._edit),
            ("Удалить", self._remove),
        ):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        bl.addStretch()
        layout.addLayout(bl)

        apply_btn = QPushButton(
            "Применить ко всем каналам текущего плейлиста")
        apply_btn.clicked.connect(self._apply_all)
        layout.addWidget(apply_btn)

        self.add_close()
        self._load_rules()

    def _load_rules(self):
        self._loading = True
        try:
            rules = self.manager.get_all_rules()
            self.rules_table.setRowCount(len(rules))
            colors = semantic_colors()
            for i, r in enumerate(rules):
                di = QTableWidgetItem(r.domain)
                di.setData(Qt.ItemDataRole.UserRole, r.domain)
                self.rules_table.setItem(i, 0, di)
                ua = r.user_agent
                disp = (ua[:60] + "..." if len(ua) > 60
                        else ua)
                it = QTableWidgetItem(
                    disp if ua else "(удалить)")
                if not ua:
                    it.setForeground(colors['unchecked'])
                it.setToolTip(ua)
                self.rules_table.setItem(i, 1, it)
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                             Qt.ItemFlag.ItemIsEnabled)
                chk.setCheckState(
                    Qt.CheckState.Checked if r.enabled
                    else Qt.CheckState.Unchecked)
                self.rules_table.setItem(i, 2, chk)
        finally:
            self._loading = False

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != 2:
            return
        di = self.rules_table.item(item.row(), 0)
        if not di:
            return
        domain = (di.data(Qt.ItemDataRole.UserRole)
                  or di.text())
        enabled = (item.checkState() == Qt.CheckState.Checked)
        if self.manager.set_enabled(domain, enabled):
            self.rules_updated.emit()

    def _add(self):
        dlg = DomainUserAgentEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            domain, ua = dlg.get_rule()
            if domain and self.manager.add_rule(domain, ua):
                self._load_rules()
                self.rules_updated.emit()

    def _edit(self):
        row = self.rules_table.currentRow()
        if row < 0:
            return
        di = self.rules_table.item(row, 0)
        if not di:
            return
        domain = (di.data(Qt.ItemDataRole.UserRole)
                  or di.text())
        rule = self.manager.get_rule(domain)
        if not rule:
            return
        dlg = DomainUserAgentEditDialog(self, rule)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            nd, nu = dlg.get_rule()
            if nd:
                if nd != rule.domain:
                    self.manager.remove_rule(rule.domain)
                self.manager.add_rule(nd, nu)
                self._load_rules()
                self.rules_updated.emit()

    def _remove(self):
        row = self.rules_table.currentRow()
        if row < 0:
            return
        it = self.rules_table.item(row, 0)
        if not it:
            return
        domain = (it.data(Qt.ItemDataRole.UserRole)
                  or it.text())
        if confirm(self, f"Удалить правило для '{domain}'?"):
            if self.manager.remove_rule(domain):
                self._load_rules()
                self.rules_updated.emit()

    def _apply_all(self):
        tab = (self._playlist_tab_ref()
               if self._playlist_tab_ref else None)
        if not tab or not _is_qobject_valid(tab):
            warn_box(self, "Нет открытого плейлиста")
            return
        if not confirm(self,
                        "Применить правила ко всем каналам?"):
            return

        tab.flush_pending_state()
        tab.undo_manager.save_state(
            tab.all_channels, "Применение UA-правил")
        tab._pending_state_desc = ""

        modified = self.manager.apply_rules_to_channels(
            tab.all_channels)
        if modified > 0:
            tab.sync_to_core()
            with tab._suppress_save():
                tab.model.refresh_all()
            tab.update_info()
            tab.groups_changed.emit()
            info_box(self, f"Обновлено {modified} каналов",
                     "Успех")
        else:
            info_box(self, "Изменений не требуется")