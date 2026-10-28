# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Диалоги редактирования каналов и сравнения плейлистов."""
from __future__ import annotations

import csv
import os
import weakref
from datetime import datetime
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
    QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QPlainTextEdit, QPushButton, QRadioButton, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from constants import (DEFAULT_GROUP, DUPLICATE_DIALOG_MAX_ROWS,
                        M3U_FILTER)
from dialogs_base import (BaseDialog, make_table,
                           _is_qobject_valid, hint_label,
                           json_import_dialog, json_export_dialog)
from diff_utils import (DiffLine, DiffOp, channels_only,
                         diff_lines, diff_stats, normalize_lines,
                         to_unified_diff)
from logging_setup import logger
from models import ChannelData
from paths import (confirm, error_box, info_box, open_file_dialog,
                    save_file_dialog, warn_box)
from theme import is_dark, semantic_colors, semantic_hex
from undo import SimpleDuplicateFinder


def _mono_font(point_size: int = 10) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if f.pointSize() <= 0:
        f.setPointSize(point_size)
    return f


def _diff_alpha(dark_value: int, light_value: int) -> int:
    return dark_value if is_dark() else light_value


def _color_add_bg() -> QColor:
    c = QColor(semantic_colors()['working'])
    c.setAlpha(_diff_alpha(70, 110))
    return c


def _color_del_bg() -> QColor:
    c = QColor(semantic_colors()['not_working'])
    c.setAlpha(_diff_alpha(70, 110))
    return c


def _color_repl_bg() -> QColor:
    c = QColor(semantic_colors()['unchecked'])
    c.setAlpha(_diff_alpha(70, 120))
    return c


def _color_lineno_fg() -> QColor:
    return semantic_colors()['no_url']


class PlaylistHeaderDialog(BaseDialog):
    def __init__(self, header_manager, parent=None):
        super().__init__("Редактор заголовка плейлиста",
                          parent, size=(600, 450))
        self.header_manager = header_manager.copy()
        self._setup_ui()
        self._load_current()

    def _setup_ui(self):
        layout = self.root
        g = QGroupBox("Основные")
        gl = QFormLayout(g)
        self.playlist_name_edit = QLineEdit()
        gl.addRow("Название плейлиста:",
                   self.playlist_name_edit)
        layout.addWidget(g)

        eg = QGroupBox("Источники EPG (url-tvg)")
        el = QVBoxLayout(eg)
        info = QLabel("✓ EPG-источники сохраняются в заголовок.")
        info.setWordWrap(True)
        info.setForegroundRole(
            info.palette().ColorRole.PlaceholderText)
        el.addWidget(info)
        self.epg_list = QListWidget()
        el.addWidget(self.epg_list)
        bl = QHBoxLayout()
        for t, s in (
            ("Добавить", self._add_epg),
            ("Редактировать", self._edit_epg),
            ("Удалить", self._remove_epg),
        ):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl.addWidget(b)
        el.addLayout(bl)
        layout.addWidget(eg)

        cg = QGroupBox("Пользовательские атрибуты")
        cl = QVBoxLayout(cg)
        self.custom_attrs_table = make_table(["Ключ", "Значение"])
        cl.addWidget(self.custom_attrs_table)
        cbl = QHBoxLayout()
        for t, s in (
            ("Добавить", self._add_attr),
            ("Редактировать", self._edit_attr),
            ("Удалить", self._remove_attr),
        ):
            b = QPushButton(t)
            b.clicked.connect(s)
            cbl.addWidget(b)
        cl.addLayout(cbl)
        layout.addWidget(cg)

        pg = QGroupBox("Предпросмотр")
        pl = QVBoxLayout(pg)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setFont(_mono_font())
        self.preview_text.setMaximumHeight(100)
        self.preview_text.setReadOnly(True)
        pl.addWidget(self.preview_text)
        layout.addWidget(pg)

        self.add_ok_cancel()

    def _load_current(self):
        self.playlist_name_edit.setText(
            self.header_manager.playlist_name)
        self.epg_list.clear()
        for s in self.header_manager.epg_sources:
            self.epg_list.addItem(s)
        self._update_custom_attrs()
        self._update_preview()

    def _update_custom_attrs(self):
        attrs = self.header_manager.custom_attributes
        self.custom_attrs_table.setRowCount(len(attrs))
        for i, (k, v) in enumerate(attrs.items()):
            self.custom_attrs_table.setItem(
                i, 0, QTableWidgetItem(k))
            self.custom_attrs_table.setItem(
                i, 1, QTableWidgetItem(v))

    def _update_preview(self):
        self.preview_text.setPlainText(
            self.header_manager.get_header_text())

    def _add_epg(self):
        url, ok = QInputDialog.getText(
            self, "Добавить EPG", "URL:",
            QLineEdit.EchoMode.Normal,
            "http://example.com/epg.xml")
        if ok and url:
            self.epg_list.addItem(url)

    def _edit_epg(self):
        it = self.epg_list.currentItem()
        if not it:
            return
        url, ok = QInputDialog.getText(
            self, "Редактировать EPG", "URL:",
            QLineEdit.EchoMode.Normal, it.text())
        if ok and url:
            it.setText(url)

    def _remove_epg(self):
        it = self.epg_list.currentItem()
        if it:
            self.epg_list.takeItem(self.epg_list.row(it))

    def _add_attr(self):
        k, ok1 = QInputDialog.getText(
            self, "Добавить", "Ключ:")
        if ok1 and k:
            v, ok2 = QInputDialog.getText(
                self, "Добавить", f"Значение для '{k}':")
            if ok2:
                self.header_manager.add_custom_attribute(k, v)
                self._update_custom_attrs()
                self._update_preview()

    def _edit_attr(self):
        row = self.custom_attrs_table.currentRow()
        if row < 0:
            return
        ki = self.custom_attrs_table.item(row, 0)
        vi = self.custom_attrs_table.item(row, 1)
        if not (ki and vi):
            return
        nk, ok1 = QInputDialog.getText(
            self, "Редактировать", "Новый ключ:",
            QLineEdit.EchoMode.Normal, ki.text())
        if not (ok1 and nk):
            return
        nv, ok2 = QInputDialog.getText(
            self, "Редактировать",
            f"Значение для '{nk}':",
            QLineEdit.EchoMode.Normal, vi.text())
        if ok2:
            self.header_manager.remove_custom_attribute(ki.text())
            self.header_manager.add_custom_attribute(nk, nv)
            self._update_custom_attrs()
            self._update_preview()

    def _remove_attr(self):
        row = self.custom_attrs_table.currentRow()
        if row < 0:
            return
        ki = self.custom_attrs_table.item(row, 0)
        if ki:
            self.header_manager.remove_custom_attribute(ki.text())
            self._update_custom_attrs()
            self._update_preview()

    def accept(self):
        epg = [self.epg_list.item(i).text()
               for i in range(self.epg_list.count())]
        self.header_manager.update_epg_sources(epg)
        self.header_manager.set_playlist_name(
            self.playlist_name_edit.text())
        super().accept()


class RemoveMetadataDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__("Удаление метаданных", parent,
                          size=(400, 360))
        l = self.root
        l.addWidget(QLabel("Выберите метаданные:"))

        g = QGroupBox("Параметры")
        gl = QVBoxLayout(g)
        self.tvg_id_check = QCheckBox("Удалить tvg-id")
        self.tvg_name_check = QCheckBox("Удалить tvg-name")
        self.tvg_logo_check = QCheckBox("Удалить tvg-logo")
        self.group_title_check = QCheckBox("Удалить group-title")
        self.user_agent_check = QCheckBox("Удалить User Agent")
        for w in (self.tvg_id_check, self.tvg_name_check,
                  self.tvg_logo_check, self.group_title_check,
                  self.user_agent_check):
            gl.addWidget(w)
        l.addWidget(g)

        sg = QGroupBox("Область")
        sl = QVBoxLayout(sg)
        self._rb_current = QRadioButton("Текущий канал", sg)
        self._rb_selected = QRadioButton("Выбранные", sg)
        self._rb_all = QRadioButton("Все каналы", sg)
        sl.addWidget(self._rb_current)
        sl.addWidget(self._rb_selected)
        sl.addWidget(self._rb_all)
        l.addWidget(sg)

        self._scope_group = QButtonGroup(sg)
        self._scope_group.addButton(self._rb_current, 0)
        self._scope_group.addButton(self._rb_selected, 1)
        self._scope_group.addButton(self._rb_all, 2)
        self._rb_current.setChecked(True)

        self.add_ok_cancel()

    def get_options(self) -> Dict[str, bool]:
        return {
            'tvg_id': self.tvg_id_check.isChecked(),
            'tvg_name': self.tvg_name_check.isChecked(),
            'tvg_logo': self.tvg_logo_check.isChecked(),
            'group_title': self.group_title_check.isChecked(),
            'user_agent': self.user_agent_check.isChecked(),
        }

    def get_scope(self) -> str:
        cid = self._scope_group.checkedId()
        if cid == 0:
            return "current"
        if cid == 1:
            return "selected"
        return "all"


class MassEditDialog(BaseDialog):
    def __init__(self, count: int, parent=None):
        super().__init__(f"Массовая правка ({count} каналов)",
                          parent, size=(520, 320))
        l = self.root
        l.addWidget(QLabel(
            f"<b>Выбрано каналов:</b> {count}"))
        g = QGroupBox("Что изменить")
        gl = QVBoxLayout(g)
        self.ua_check = QCheckBox("User-Agent")
        self.tvg_id_check = QCheckBox("TVG-ID")
        self.tvg_logo_check = QCheckBox("TVG-Logo")
        self.group_check = QCheckBox("Group-title")
        for w in (self.ua_check, self.tvg_id_check,
                  self.tvg_logo_check, self.group_check):
            gl.addWidget(w)
        l.addWidget(g)
        self.ua_edit = QLineEdit()
        self.ua_edit.setPlaceholderText(
            "Оставьте пустым, чтобы удалить")
        self.tvg_id_edit = QLineEdit()
        self.tvg_logo_edit = QLineEdit()
        self.group_edit = QLineEdit()
        form = QFormLayout()
        form.addRow("User-Agent:", self.ua_edit)
        form.addRow("TVG-ID:", self.tvg_id_edit)
        form.addRow("TVG-Logo:", self.tvg_logo_edit)
        form.addRow("Group-title:", self.group_edit)
        l.addLayout(form)
        self.add_ok_cancel()

    def get_changes(self) -> Dict[str, str]:
        result = {}
        if self.ua_check.isChecked():
            result['user_agent'] = self.ua_edit.text().strip()
        if self.tvg_id_check.isChecked():
            result['tvg_id'] = self.tvg_id_edit.text().strip()
        if self.tvg_logo_check.isChecked():
            result['tvg_logo'] = self.tvg_logo_edit.text().strip()
        if self.group_check.isChecked():
            result['group'] = self.group_edit.text().strip()
        return result

    def accept(self):
        if not any((self.ua_check.isChecked(),
                    self.tvg_id_check.isChecked(),
                    self.tvg_logo_check.isChecked(),
                    self.group_check.isChecked())):
            warn_box(self, "Выберите хотя бы одно поле")
            return
        super().accept()


class DuplicateFinderDialog(BaseDialog):
    duplicates_removed = pyqtSignal(int)

    def __init__(self, channels: List[ChannelData],
                 parent=None,
                 use_tvg_id: bool = False,
                 keep_duplicates: bool = False,
                 settings: Any = None):
        super().__init__("Поиск дубликатов", parent,
                          size=(980, 640))
        # ВАЖНО: работаем с копией, чтобы не мутировать
        # tab.all_channels до явного подтверждения.
        self._original_channels = list(channels)
        self._working_channels = list(channels)
        self.use_tvg_id = use_tvg_id
        self.keep_duplicates = keep_duplicates
        self.settings = settings
        self._setup_ui()
        self._update_stats()

    def _setup_ui(self):
        l = self.root
        self.stats_label = QLabel()
        l.addWidget(self.stats_label)

        opts = QHBoxLayout()
        self.tvg_check = QCheckBox(
            "Учитывать TVG-ID при поиске по названию")
        self.tvg_check.setChecked(self.use_tvg_id)
        self.tvg_check.toggled.connect(self._on_tvg_toggled)
        opts.addWidget(self.tvg_check)
        self.keep_check = QCheckBox(
            "Сохранять дубликаты (только показать)")
        self.keep_check.setChecked(self.keep_duplicates)
        opts.addWidget(self.keep_check)
        opts.addStretch()

        self.export_report_btn = QPushButton("💾 Экспорт CSV-отчёта")
        self.export_report_btn.setToolTip(
            "Сохранить CSV-отчёт: key, stable_key, names, reason.")
        self.export_report_btn.clicked.connect(self._export_report)
        opts.addWidget(self.export_report_btn)

        l.addLayout(opts)

        tabs = QTabWidget()

        url_tab = QWidget()
        url_l = QVBoxLayout(url_tab)
        self.url_table = make_table(
            ["Название", "Группа", "URL"])
        url_l.addWidget(self.url_table)
        self.remove_url_btn = QPushButton(
            "Удалить дубликаты по URL")
        self.remove_url_btn.clicked.connect(self._remove_by_url)
        url_l.addWidget(self.remove_url_btn)
        tabs.addTab(url_tab, "По URL")

        name_tab = QWidget()
        name_l = QVBoxLayout(name_tab)
        self.name_table = make_table(
            ["Название", "Группа", "URL"])
        name_l.addWidget(self.name_table)
        self.remove_name_btn = QPushButton(
            "Удалить дубликаты по названию")
        self.remove_name_btn.clicked.connect(self._remove_by_name)
        name_l.addWidget(self.remove_name_btn)
        tabs.addTab(name_tab, "По названию")

        l.addWidget(tabs)
        self.add_close()

    def _on_tvg_toggled(self, checked: bool):
        self.use_tvg_id = checked
        self._update_stats()

    def _update_stats(self):
        report = SimpleDuplicateFinder.get_duplicate_report(
            self._working_channels, use_tvg_id=self.use_tvg_id,
            settings=self.settings)
        self.stats_label.setText(
            f"<b>Всего каналов:</b> "
            f"{len(self._working_channels)} | "
            f"<b>Дубликатов по URL:</b> "
            f"{report['total_url_duplicates']} | "
            f"<b>Дубликатов по названию:</b> "
            f"{report['total_name_duplicates']}")
        self._fill_dup_table(self.url_table, report['by_url'])
        self._fill_dup_table(self.name_table, report['by_name'])

    @classmethod
    def _fill_dup_table(cls, table: QTableWidget,
                        groups: List[List[ChannelData]]):
        rows_total = sum(len(g) for g in groups)
        displayed = min(rows_total, DUPLICATE_DIALOG_MAX_ROWS)
        table.setRowCount(displayed)
        row = 0
        for group in groups:
            for ch in group:
                if row >= displayed:
                    return
                table.setItem(row, 0,
                              QTableWidgetItem(ch.meta.name or ""))
                table.setItem(row, 1,
                              QTableWidgetItem(ch.meta.group or ""))
                table.setItem(row, 2,
                              QTableWidgetItem(
                                  (ch.link.url or "")[:80]))
                row += 1

    def _remove_by_url(self):
        if not confirm(
                self,
                "Удалить дубликаты по URL? "
                "Останется первый канал."):
            return
        new_list, removed = \
            SimpleDuplicateFinder.remove_duplicates_by_url(
                self._working_channels)
        if removed:
            self._working_channels = new_list
        self.duplicates_removed.emit(removed)
        self._update_stats()
        info_box(self, f"Удалено: {removed}", "Готово")

    def _remove_by_name(self):
        if not confirm(
                self,
                "Удалить дубликаты по названию? "
                "Останется первый канал."):
            return
        new_list, removed = \
            SimpleDuplicateFinder.remove_duplicates_by_name(
                self._working_channels, use_tvg_id=self.use_tvg_id,
                settings=self.settings)
        if removed:
            self._working_channels = new_list
        self.duplicates_removed.emit(removed)
        self._update_stats()
        info_box(self, f"Удалено: {removed}", "Готово")

    def get_result(self) -> List[ChannelData]:
        return list(self._working_channels)

    def _export_report(self):
        fp = save_file_dialog(
            self, "Экспорт CSV-отчёта",
            "duplicates_report.csv",
            "CSV (*.csv);;Все файлы (*.*)", ".csv")
        if not fp:
            return
        try:
            report = SimpleDuplicateFinder.get_duplicate_report(
                self._working_channels, use_tvg_id=self.use_tvg_id,
                settings=self.settings)
            rows = 0
            with open(fp, "w", encoding="utf-8-sig",
                      newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "key", "stable_key", "name", "group",
                    "tvg_id", "url", "source"])
                for group in report['by_name']:
                    for ch in group:
                        w.writerow([
                            ch.normalized_name(),
                            ch.stable_key(),
                            ch.meta.name or "",
                            ch.meta.group or "",
                            ch.meta.tvg_id or "",
                            ch.link.url or "",
                            "name",
                        ])
                        rows += 1
                for group in report['by_url']:
                    for ch in group:
                        w.writerow([
                            ch.normalized_name(),
                            ch.stable_key(),
                            ch.meta.name or "",
                            ch.meta.group or "",
                            ch.meta.tvg_id or "",
                            ch.link.url or "",
                            "url",
                        ])
                        rows += 1
            info_box(self,
                     f"Отчёт сохранён:\n{fp}\n\nСтрок: {rows}",
                     "Готово")
        except Exception as e:
            logger.exception("export_duplicates_report")
            error_box(self, str(e))


class ComparePlaylistsDialog(BaseDialog):
    def __init__(self, current_text: str,
                 current_label: str = "Текущий",
                 parent=None):
        super().__init__("Сравнение плейлистов", parent,
                          size=(1100, 700))
        self._current_text = current_text
        self._current_label = current_label
        self._other_text = ""
        self._other_label = "другой"
        self._diff: List[DiffLine] = []
        self._setup_ui()
        self._update_stats()

    def _setup_ui(self):
        l = self.root

        top = QHBoxLayout()
        self.load_btn = QPushButton(
            "Загрузить второй плейлист…")
        self.load_btn.clicked.connect(self._load_other)
        top.addWidget(self.load_btn)

        self.file_label = QLabel("Второй файл не загружен")
        top.addWidget(self.file_label, 1)

        top.addWidget(QLabel("Режим:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Построчный (как git)", "lines")
        self.mode_combo.addItem("По каналам", "channels")
        self.mode_combo.currentIndexChanged.connect(self._recompute)
        top.addWidget(self.mode_combo)

        self.ignore_ws_check = QCheckBox("Игнорировать пробелы")
        self.ignore_ws_check.toggled.connect(self._recompute)
        top.addWidget(self.ignore_ws_check)

        self.export_btn = QPushButton("Экспорт diff…")
        self.export_btn.clicked.connect(self._export_diff)
        self.export_btn.setEnabled(False)
        top.addWidget(self.export_btn)

        l.addLayout(top)

        self.stats_label = QLabel("")
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        l.addWidget(self.stats_label)

        self.unified_check = QCheckBox(
            "Показать unified diff (текстом)")
        self.unified_check.toggled.connect(
            self._on_unified_toggled)
        l.addWidget(self.unified_check)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            ["№", self._current_label,
             "№", self._other_label])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)

        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(2, 55)

        mono = _mono_font()
        self.table.setFont(mono)
        l.addWidget(self.table, 1)

        self.unified_view = QPlainTextEdit()
        self.unified_view.setReadOnly(True)
        self.unified_view.setFont(mono)
        self.unified_view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.NoWrap)
        self.unified_view.setVisible(False)
        l.addWidget(self.unified_view, 1)

        self.add_close()

    def _load_other(self):
        fp = open_file_dialog(self, "Второй плейлист",
                                M3U_FILTER)
        if not fp:
            return
        try:
            with open(fp, 'r', encoding='utf-8',
                      errors='replace') as f:
                self._other_text = f.read()
        except Exception as e:
            error_box(self, str(e))
            return
        self._other_label = os.path.basename(fp)
        self.file_label.setText(
            f"Второй файл: {self._other_label}")
        self.table.setHorizontalHeaderLabels(
            ["№", self._current_label,
             "№", self._other_label])
        self.export_btn.setEnabled(True)
        self._recompute()

    def _prepare_lines(self, text: str) -> List[str]:
        lines = normalize_lines(
            text, self.ignore_ws_check.isChecked())
        if self.mode_combo.currentData() == "channels":
            lines = channels_only(lines)
        return lines

    def _recompute(self):
        if not self._other_text:
            self._diff = []
            self.table.setRowCount(0)
            self._update_stats()
            return
        left = self._prepare_lines(self._current_text)
        right = self._prepare_lines(self._other_text)
        self._diff = diff_lines(left, right)
        self._fill_table()
        self._update_stats()
        if self.unified_check.isChecked():
            self._fill_unified()

    def _fill_table(self):
        self.table.setRowCount(len(self._diff))
        mono = self.table.font()
        add_bg = _color_add_bg()
        del_bg = _color_del_bg()
        repl_bg = _color_repl_bg()
        lineno_fg = _color_lineno_fg()
        for row, d in enumerate(self._diff):
            self._set_row(row, d, mono, add_bg, del_bg, repl_bg,
                          lineno_fg)

    def _set_row(self, row: int, d: DiffLine, font: QFont,
                 add_bg: QColor, del_bg: QColor, repl_bg: QColor,
                 lineno_fg: QColor):
        left_no = QTableWidgetItem(
            str(d.left_no) if d.left_no else "")
        left_no.setForeground(lineno_fg)
        left_no.setTextAlignment(
            Qt.AlignmentFlag.AlignRight |
            Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 0, left_no)

        left_item = QTableWidgetItem(d.left)
        left_item.setFont(font)
        self.table.setItem(row, 1, left_item)

        right_no = QTableWidgetItem(
            str(d.right_no) if d.right_no else "")
        right_no.setForeground(lineno_fg)
        right_no.setTextAlignment(
            Qt.AlignmentFlag.AlignRight |
            Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 2, right_no)

        right_item = QTableWidgetItem(d.right)
        right_item.setFont(font)
        self.table.setItem(row, 3, right_item)

        bg_left = None
        bg_right = None
        if d.op == DiffOp.DELETE:
            bg_left = del_bg
        elif d.op == DiffOp.INSERT:
            bg_right = add_bg
        elif d.op == DiffOp.REPLACE:
            bg_left = repl_bg
            bg_right = repl_bg

        if bg_left is not None:
            left_item.setBackground(QBrush(bg_left))
            left_no.setBackground(QBrush(bg_left))
        if bg_right is not None:
            right_item.setBackground(QBrush(bg_right))
            right_no.setBackground(QBrush(bg_right))

    def _fill_unified(self):
        left = self._prepare_lines(self._current_text)
        right = self._prepare_lines(self._other_text)
        text = to_unified_diff(
            left, right,
            left_label=self._current_label,
            right_label=self._other_label)
        self.unified_view.setPlainText(
            text or "(нет различий)")

    def _on_unified_toggled(self, checked: bool):
        self.table.setVisible(not checked)
        self.unified_view.setVisible(checked)
        if checked and self._other_text:
            self._fill_unified()

    def _update_stats(self):
        if self._diff:
            st = diff_stats(self._diff)
        else:
            st = {'added': 0, 'removed': 0, 'changed': 0,
                  'equal': 0, 'total': 0}
        c_add = semantic_hex('working')
        c_del = semantic_hex('not_working')
        c_chg = semantic_hex('unchecked')
        self.stats_label.setText(
            f"<b>Добавлено:</b> "
            f"<span style='color:{c_add}'>+{st['added']}</span> | "
            f"<b>Удалено:</b> "
            f"<span style='color:{c_del}'>-{st['removed']}</span> | "
            f"<b>Изменено:</b> "
            f"<span style='color:{c_chg}'>~{st['changed']}</span> | "
            f"<b>Без изменений:</b> {st['equal']} | "
            f"<b>Строк diff:</b> {st['total']}")

    def _export_diff(self):
        if not self._diff:
            warn_box(self, "Нет данных для экспорта")
            return
        fp = save_file_dialog(
            self, "Экспорт diff", "playlists.diff",
            "Diff (*.diff *.patch);;Все файлы (*.*)")
        if not fp:
            return
        try:
            left = self._prepare_lines(self._current_text)
            right = self._prepare_lines(self._other_text)
            text = to_unified_diff(
                left, right,
                left_label=self._current_label,
                right_label=self._other_label)
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(text or "")
            info_box(self, f"Diff сохранён:\n{fp}", "Экспорт")
        except Exception as e:
            logger.exception("export_diff")
            error_box(self, str(e))


class GroupTemplateListDialog(BaseDialog):
    template_applied = pyqtSignal(str)

    def __init__(self, manager, playlist_tab=None, parent=None):
        super().__init__("Шаблоны группировки каналов", parent,
                          size=(760, 560))
        self.manager = manager
        self._tab_ref = (weakref.ref(playlist_tab)
                         if playlist_tab else None)
        self._setup_ui()
        self._reload()

    def _setup_ui(self):
        l = self.root
        l.addWidget(hint_label(
            "Шаблон — это именованный набор правил "
            "«имя канала → group-title».\n"
            "Создайте шаблон из текущего плейлиста, затем "
            "применяйте его к любому открытому плейлисту.\n"
            "При применении меняется ТОЛЬКО group-title — "
            "имена, URL и метаданные каналов не трогаются."))

        self.table = make_table(
            ["Имя шаблона", "Правил", "Создан", "Изменён"],
            stretch_last=False,
            select_mode=(
                QAbstractItemView.SelectionMode.SingleSelection))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(
            lambda _idx: self._apply_selected())
        l.addWidget(self.table)

        btn_row = QHBoxLayout()
        for text, slot in (
            ("➕ Из текущего плейлиста…", self._create_from_tab),
            ("🔄 Применить к плейлисту", self._apply_selected),
            ("✏ Переименовать", self._rename_selected),
            ("🗑 Удалить", self._delete_selected),
            ("📥 Импорт", self._import_template),
            ("📤 Экспорт", self._export_template),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btn_row.addWidget(b)
        l.addLayout(btn_row)

        self.stats_label = QLabel("")
        self.stats_label.setWordWrap(True)
        l.addWidget(self.stats_label)

        self.add_close()

    @staticmethod
    def _fmt_ts(ts: float) -> str:
        try:
            return datetime.fromtimestamp(ts).strftime(
                "%Y-%m-%d %H:%M")
        except Exception:
            return "—"

    def _reload(self):
        templates = self.manager.get_all()
        self.table.setRowCount(len(templates))
        for i, t in enumerate(templates):
            self.table.setItem(i, 0, QTableWidgetItem(t.name))
            cnt = QTableWidgetItem(str(len(t.rules)))
            cnt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 1, cnt)
            self.table.setItem(
                i, 2, QTableWidgetItem(self._fmt_ts(t.created)))
            self.table.setItem(
                i, 3, QTableWidgetItem(self._fmt_ts(t.modified)))
        if not templates:
            self.stats_label.setText(
                "Шаблонов пока нет. Нажмите "
                "«➕ Из текущего плейлиста…», чтобы создать.")

    def _selected_name(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0:
            return None
        it = self.table.item(row, 0)
        return it.text() if it else None

    def _current_tab(self):
        tab = self._tab_ref() if self._tab_ref else None
        if tab is not None and _is_qobject_valid(tab):
            return tab
        main = self.parent()
        cur = getattr(main, 'current_tab', None) if main else None
        if cur is not None and _is_qobject_valid(cur):
            return cur
        return None

    def _create_from_tab(self):
        tab = self._current_tab()
        if tab is None or not getattr(tab, 'all_channels', None):
            warn_box(self, "Нет открытого плейлиста с каналами")
            return

        default_name = "Моя группировка"
        if getattr(tab, 'filepath', None):
            base = os.path.splitext(
                os.path.basename(tab.filepath))[0]
            if base:
                default_name = base

        name, ok = QInputDialog.getText(
            self, "Создать шаблон",
            "Имя шаблона:", QLineEdit.EchoMode.Normal,
            default_name)
        if not ok or not name.strip():
            return
        name = name.strip()

        if self.manager.exists(name):
            if not confirm(
                    self,
                    f"Шаблон «{name}» уже есть. Заменить?"):
                return

        include_only = confirm(
            self,
            "Включить в шаблон ТОЛЬКО каналы с непустым "
            "group-title?\n\n"
            "Да — каналы без группы (или с «Без группы») "
            "не попадут в шаблон.\n"
            "Нет — все каналы попадут, а у каналов без "
            "группы будет «Без группы».")

        settings = None
        try:
            from app_core import ApplicationCore
            core = ApplicationCore.instance()
            settings = core.get_replacement_settings()
        except Exception:
            logger.exception("get settings for template build")

        template, msg = self.manager.build_from_channels(
            tab.all_channels, name, settings=settings,
            include_only_existing_groups=include_only)
        if template is None:
            warn_box(self, msg)
            return

        ok, err = self.manager.save_template(
            template, overwrite=True)
        if not ok:
            error_box(self, err)
            return
        self._reload()
        info_box(
            self,
            f"Шаблон «{name}» сохранён.\n\n{msg}",
            "Шаблоны группировки")

    def _apply_selected(self):
        name = self._selected_name()
        if not name:
            warn_box(self, "Выберите шаблон в списке")
            return
        tab = self._current_tab()
        if tab is None or not getattr(tab, 'all_channels', None):
            warn_box(self, "Нет открытого плейлиста")
            return

        template = self.manager.get(name)
        if template is None:
            warn_box(self, f"Шаблон «{name}» не найден")
            return

        keep_unmatched = confirm(
            self,
            f"Применить шаблон «{name}»?\n\n"
            f"Правил в шаблоне: {len(template.rules)}\n\n"
            "Каналы, которых нет в шаблоне:\n"
            "Да — сохранить им текущий group-title;\n"
            "Нет — присвоить «Без группы».")

        settings = None
        try:
            from app_core import ApplicationCore
            core = ApplicationCore.instance()
            settings = core.get_replacement_settings()
        except Exception:
            logger.exception("get settings for template apply")

        try:
            tab.save_state(f"Применение шаблона «{name}»")
        except Exception:
            logger.exception("tab.save_state")

        stats = self.manager.apply_to_channels(
            template, tab.all_channels, settings=settings,
            keep_unmatched=keep_unmatched)

        if stats['changed'] > 0:
            try:
                tab.sync_to_core()
                with tab._suppress_save():
                    tab._rebuild_model_safely(
                        preserve_selection=True)
                tab.update_info()
                tab.groups_changed.emit()
            except Exception:
                logger.exception("refresh after template apply")

        info_box(
            self,
            f"Шаблон «{name}» применён.\n\n"
            f"Каналов изменено: {stats['changed']}\n"
            f"Совпало с шаблоном: {stats['matched']}\n"
            f"Не найдено в шаблоне: {stats['unmatched']}\n"
            f"Правил шаблона без каналов в плейлисте: "
            f"{stats['missing']}",
            "Шаблоны группировки")

        self.template_applied.emit(name)

    def _rename_selected(self):
        name = self._selected_name()
        if not name:
            return
        new_name, ok = QInputDialog.getText(
            self, "Переименовать шаблон",
            "Новое имя:", QLineEdit.EchoMode.Normal, name)
        if not ok or not new_name.strip() or new_name == name:
            return
        ok2, err = self.manager.rename_template(
            name, new_name.strip())
        if not ok2:
            error_box(self, err)
            return
        self._reload()

    def _delete_selected(self):
        name = self._selected_name()
        if not name:
            return
        if not confirm(self, f"Удалить шаблон «{name}»?"):
            return
        if self.manager.delete_template(name):
            self._reload()

    def _import_template(self):
        def on_items(data: list) -> int:
            from group_templates import GroupTemplate
            n = 0
            for it in data:
                if not isinstance(it, dict):
                    continue
                t = GroupTemplate.from_dict(it)
                if not t.name or not t.rules:
                    continue
                ok, _err = self.manager.save_template(
                    t, overwrite=True)
                if ok:
                    n += 1
            self._reload()
            return n
        n = json_import_dialog(
            self, "Импорт шаблонов", on_items)
        if n is not None:
            info_box(self, f"Импортировано: {n}", "Импорт")

    def _export_template(self):
        name = self._selected_name()
        if not name:
            warn_box(self, "Выберите шаблон")
            return
        t = self.manager.get(name)
        if t is None:
            return
        ok = json_export_dialog(
            self, "Экспорт шаблона",
            f"{name}.json", [t.to_dict()])
        if ok:
            info_box(self, "Шаблон экспортирован", "Экспорт")