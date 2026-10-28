# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Диалоги источников ссылок и выбора ссылки."""
from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QPushButton, QProgressBar,
    QSpinBox, QTableWidgetItem, QVBoxLayout, QWidget)

from constants import (
    M3U_FILTER, OK_CANCEL_BB,
    SOURCES_FINDER_MAX_GITHUB_DEFAULT,
    SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT,
    SOURCES_FINDER_UPDATE_INTERVAL_HOURS)
from dialogs_base import (BaseDialog, _NumericItem,
                           _is_qobject_valid, hint_label,
                           json_export_dialog, json_import_dialog,
                           make_table)
from logging_setup import logger
from paths import (confirm, error_box, info_box,
                    open_file_dialog, warn_box)
from sources import LinkSource, LinkSourceManager
from theme import semantic_colors
from utils import URLUtils, _StopToken
from workers import SourceCheckConfig, SourcesRefreshWorker


@dataclass
class PlaylistCreationOptions:
    sources: List[LinkSource]
    dedupe_by_url: bool = True
    preserve_order: bool = True
    apply_ua: bool = True
    apply_blacklist: bool = True
    apply_domain_blacklist: bool = True
    apply_epg: bool = True


class LinkSourceEditDialog(BaseDialog):
    def __init__(self, parent=None,
                 source: Optional[LinkSource] = None,
                 existing_names: Optional[Set[str]] = None):
        title = ("Правка источника" if source
                 else "Добавление источника")
        super().__init__(title, parent, size=(500, 600))
        self.source = source
        self.existing_names = existing_names or set()
        self._setup_ui()
        if source:
            self._load_data()

    def _setup_ui(self):
        layout = self.root
        layout.addWidget(hint_label(
            "Источник — это ваш локальный файл или URL, "
            "который вы добавили сами. Приложение не содержит "
            "встроенных ссылок на IPTV."))
        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("Название:", self.name_edit)
        self.type_combo = QComboBox()
        self.type_combo.addItems(
            ["Локальный файл", "Онлайн источник"])
        self.type_combo.currentIndexChanged.connect(
            self._on_type_changed)
        form.addRow("Тип:", self.type_combo)
        self.path_edit = QLineEdit()
        form.addRow("Путь/URL:", self.path_edit)
        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.clicked.connect(self._browse)
        form.addRow("", self.browse_btn)
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 10)
        self.priority_spin.setValue(5)
        form.addRow("Приоритет (1-10):", self.priority_spin)
        self.encoding_edit = QLineEdit("utf-8")
        form.addRow("Кодировка:", self.encoding_edit)
        layout.addLayout(form)

        g = QGroupBox("Дополнительно")
        gl = QVBoxLayout(g)
        self.enabled_check = QCheckBox("Включен")
        self.enabled_check.setChecked(True)
        gl.addWidget(self.enabled_check)
        self.auto_update_check = QCheckBox(
            "Автоматическое обновление")
        gl.addWidget(self.auto_update_check)
        ul = QHBoxLayout()
        ul.addWidget(QLabel("Интервал (часы):"))
        self.update_interval_spin = QSpinBox()
        self.update_interval_spin.setRange(1, 168)
        self.update_interval_spin.setValue(24)
        ul.addWidget(self.update_interval_spin)
        ul.addStretch()
        gl.addLayout(ul)
        layout.addWidget(g)

        fg = QGroupBox("Фильтрация при загрузке")
        fl = QVBoxLayout(fg)
        self.apply_blacklist_check = QCheckBox(
            "Применять чёрный список каналов")
        self.apply_blacklist_check.setChecked(True)
        fl.addWidget(self.apply_blacklist_check)
        self.apply_domain_blacklist_check = QCheckBox(
            "Очищать ссылки по ЧС домен/IP "
            "(каналы сохраняются)")
        self.apply_domain_blacklist_check.setChecked(True)
        fl.addWidget(self.apply_domain_blacklist_check)
        layout.addWidget(fg)

        self.add_ok_cancel()

    def _load_data(self):
        s = self.source
        if not s:
            return
        self.name_edit.setText(s.name)
        self.type_combo.setCurrentIndex(
            0 if s.source_type == "local" else 1)
        self.path_edit.setText(s.path)
        self.priority_spin.setValue(s.priority)
        self.encoding_edit.setText(s.encoding)
        self.enabled_check.setChecked(s.enabled)
        self.auto_update_check.setChecked(s.auto_update)
        self.update_interval_spin.setValue(
            s.update_interval_hours)
        self.apply_blacklist_check.setChecked(
            s.apply_blacklist)
        self.apply_domain_blacklist_check.setChecked(
            s.apply_domain_blacklist)
        self._on_type_changed(self.type_combo.currentIndex())

    def _on_type_changed(self, idx: int):
        self.browse_btn.setEnabled(idx == 0)

    def _browse(self):
        fp = open_file_dialog(self, "Файл", M3U_FILTER)
        if fp:
            self.path_edit.setText(fp)

    def _validate(self) -> bool:
        name = self.name_edit.text().strip()
        if not name:
            warn_box(self, "Введите название")
            return False
        if self.source is None and name in self.existing_names:
            warn_box(self,
                     f"Источник '{name}' уже существует")
            return False
        path = self.path_edit.text().strip()
        if not path:
            warn_box(self, "Введите путь/URL")
            return False
        if self.type_combo.currentIndex() == 0:
            if not os.path.exists(path):
                if not confirm(self,
                                "Файл не существует. Продолжить?"):
                    return False
        else:
            p = urlparse(path)
            if p.scheme not in ('http', 'https') or not p.netloc:
                warn_box(self,
                         "URL должен быть http(s)://host/path")
                return False
        return True

    def _create(self) -> LinkSource:
        s = (LinkSource() if not self.source
             else self.source.copy())
        s.name = self.name_edit.text().strip()
        s.source_type = (
            "local" if self.type_combo.currentIndex() == 0
            else "online")
        s.path = self.path_edit.text().strip()
        s.priority = self.priority_spin.value()
        s.encoding = (self.encoding_edit.text().strip()
                      or "utf-8")
        s.enabled = self.enabled_check.isChecked()
        s.auto_update = self.auto_update_check.isChecked()
        s.update_interval_hours = \
            self.update_interval_spin.value()
        s.apply_blacklist = \
            self.apply_blacklist_check.isChecked()
        s.apply_domain_blacklist = \
            self.apply_domain_blacklist_check.isChecked()
        return s

    def get_source(self) -> Optional[LinkSource]:
        if not self._validate():
            return None
        return self._create()

    def accept(self):
        if self._validate():
            super().accept()


class SourcesFinderDialog(BaseDialog):
    def __init__(self, core=None, parent=None):
        super().__init__("🔍 Поиск источников автоматически",
                          parent, size=(760, 560))
        self.core = core
        self._worker = None
        self._found: List[Dict[str, Any]] = []
        self._worker_cls = None
        try:
            from generator import SourcesFinderWorker
            self._worker_cls = SourcesFinderWorker
        except ImportError as e:
            logger.warning("generator unavailable: %s", e)
        self._setup_ui()

    def _setup_ui(self):
        l = self.root
        l.addWidget(hint_label(
            "Поиск публичных плейлистов выполняется через "
            "GitHub API в момент запуска.\n"
            "Приложение не содержит встроенных ссылок: "
            "всё, что вы видите в результатах, получено "
            "live-запросом.\n"
            "Найденные источники будут добавлены в менеджер "
            "с флагом «Автообновление»."))

        g = QGroupBox("Что искать")
        gl = QVBoxLayout(g)
        self.github_check = QCheckBox(
            "GitHub Search API (по запросу)")
        self.github_check.setChecked(True)
        gl.addWidget(self.github_check)
        self.m3uguide_check = QCheckBox(
            "Репозиторий m3u.guide (через GitHub API)")
        self.m3uguide_check.setChecked(True)
        gl.addWidget(self.m3uguide_check)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Макс. GitHub:"))
        self.max_github_spin = QSpinBox()
        self.max_github_spin.setRange(1, 100)
        try:
            if self.core is not None:
                val = int(self.core.config.get(
                    'sources_finder_max_github',
                    SOURCES_FINDER_MAX_GITHUB_DEFAULT))
            else:
                val = SOURCES_FINDER_MAX_GITHUB_DEFAULT
        except Exception:
            val = SOURCES_FINDER_MAX_GITHUB_DEFAULT
        self.max_github_spin.setValue(val)
        opts.addWidget(self.max_github_spin)

        opts.addWidget(QLabel("Макс. m3u.guide:"))
        self.max_m3uguide_spin = QSpinBox()
        self.max_m3uguide_spin.setRange(1, 50)
        try:
            if self.core is not None:
                val = int(self.core.config.get(
                    'sources_finder_max_m3uguide',
                    SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT))
            else:
                val = SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT
        except Exception:
            val = SOURCES_FINDER_MAX_M3UGUIDE_DEFAULT
        self.max_m3uguide_spin.setValue(val)
        opts.addWidget(self.max_m3uguide_spin)
        opts.addStretch()
        gl.addLayout(opts)
        l.addWidget(g)

        self.progress_label = QLabel("Готов к поиску")
        l.addWidget(self.progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        l.addWidget(self.progress_bar)

        l.addWidget(QLabel("Найденные источники:"))
        self.table = make_table(
            ["✓", "Название", "Тип", "URL"],
            stretch_last=False,
            select_rows=False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        l.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        self.search_btn = QPushButton("🔍 Начать поиск")
        self.search_btn.clicked.connect(self._start_search)
        self.search_btn.setEnabled(self._worker_cls is not None)
        btn_row.addWidget(self.search_btn)

        self.stop_btn = QPushButton("⏹ Остановить")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_search)
        btn_row.addWidget(self.stop_btn)

        self.select_all_btn = QPushButton("Выбрать все")
        self.select_all_btn.clicked.connect(
            lambda: self._set_all(True))
        btn_row.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Снять все")
        self.deselect_all_btn.clicked.connect(
            lambda: self._set_all(False))
        btn_row.addWidget(self.deselect_all_btn)
        btn_row.addStretch()
        l.addLayout(btn_row)

        from PyQt6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Добавить выбранные и загрузить")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

        if self._worker_cls is None:
            self.progress_label.setText(
                "⚠️ Модуль generator.py не найден — "
                "поиск недоступен")

    def _set_all(self, checked: bool):
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 0)
            if it is not None:
                it.setCheckState(
                    Qt.CheckState.Checked if checked
                    else Qt.CheckState.Unchecked)

    def _worker_is_running(self) -> bool:
        if self._worker is None:
            return False
        try:
            return bool(self._worker.isRunning())
        except RuntimeError:
            self._worker = None
            return False

    def _start_search(self):
        if self._worker_cls is None:
            warn_box(self, "Модуль generator.py не найден")
            return
        if self._worker_is_running():
            return
        self._worker = None
        self._found = []
        self.table.setRowCount(0)
        self.progress_label.setText("⏳ Поиск...")
        self.progress_bar.setVisible(True)
        self.search_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._worker = self._worker_cls(
            include_github=self.github_check.isChecked(),
            include_m3uguide=self.m3uguide_check.isChecked(),
            max_github=self.max_github_spin.value(),
            max_m3uguide=self.max_m3uguide_spin.value(),
        )
        self._worker.progress.connect(
            self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.found.connect(
            self._on_found, Qt.ConnectionType.QueuedConnection)
        self._worker.search_finished.connect(
            self._on_finished,
            Qt.ConnectionType.QueuedConnection)
        self._worker.error.connect(
            self._on_error, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    def _stop_search(self):
        if self._worker_is_running():
            with suppress(Exception):
                self._worker.stop()

    def _on_progress(self, msg: str):
        self.progress_label.setText(msg)

    def _on_found(self, items: list):
        self._found = list(items)
        self._fill_table()

    def _on_finished(self, items: list):
        self._worker = None
        self._found = list(items)
        self._fill_table()
        self.progress_bar.setVisible(False)
        self.search_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_label.setText(
            f"✓ Найдено источников: {len(self._found)}")

    def _on_error(self, msg: str):
        self._worker = None
        self.progress_bar.setVisible(False)
        self.search_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_label.setText(f"✗ Ошибка: {msg}")
        QTimer.singleShot(
            0, lambda m=msg: error_box(
                self, m, "Ошибка поиска"))

    def _fill_table(self):
        self.table.setRowCount(len(self._found))
        for i, item in enumerate(self._found):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                         Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(i, 0, chk)

            name_item = QTableWidgetItem(
                item.get('name') or '')
            name_item.setData(
                Qt.ItemDataRole.UserRole, item)
            self.table.setItem(i, 1, name_item)

            type_item = QTableWidgetItem(
                item.get('source') or '')
            type_item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, type_item)

            url_item = QTableWidgetItem(item.get('url') or '')
            url_item.setToolTip(item.get('url') or '')
            self.table.setItem(i, 3, url_item)

    def get_selected_items(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if (chk is None
                    or chk.checkState() != Qt.CheckState.Checked):
                continue
            name_item = self.table.item(row, 1)
            if name_item is None:
                continue
            data = name_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                result.append(data)
        return result

    def closeEvent(self, event):
        w = self._worker
        self._worker = None
        if w is not None:
            with suppress(Exception):
                try:
                    if w.isRunning():
                        w.stop()
                        if not w.wait(3000):
                            logger.warning(
                                "SourcesFinderDialog: worker did "
                                "not finish in 3 sec, terminating")
                            w.terminate()
                            w.wait(1000)
                except RuntimeError:
                    pass
        super().closeEvent(event)


class LinkSourceManagerDialog(BaseDialog):
    sources_updated = pyqtSignal()

    def __init__(self, source_manager: LinkSourceManager,
                 config, parent=None):
        super().__init__("Менеджер источников", parent,
                          size=(1150, 680))
        self.source_manager = source_manager
        self.config = config
        self._refresh_worker: Optional[SourcesRefreshWorker] = None
        self._loading = False
        self._setup_ui()
        self._load_sources()

    def _setup_ui(self):
        layout = self.root
        layout.addWidget(hint_label(
            "Источники ссылок — это ваши плейлисты: локальные "
            "файлы или URL, которые вы добавили сами.\n"
            "Приложение не содержит встроенных ссылок на "
            "IPTV.\n\n"
            "Кнопка «🔍 Найти источники автоматически» ищет "
            "публичные плейлисты через GitHub API в момент "
            "выполнения — конкретные URL не зашиты в программу."))

        self.sources_table = make_table(
            ["Вкл", "Название", "Тип", "Путь/URL", "Приоритет",
             "Авто", "Обновлено", "Всего", "Рабочих"],
            stretch_last=False,
            select_mode=(
                QAbstractItemView.SelectionMode.SingleSelection))
        self.sources_table.setSortingEnabled(True)
        self.sources_table.itemChanged.connect(
            self._on_item_changed)
        h = self.sources_table.horizontalHeader()
        for i in range(9):
            h.setSectionResizeMode(
                i, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.sources_table)

        bl1 = QHBoxLayout()
        for t, s in (
            ("Добавить", self._add),
            ("Редактировать", self._edit),
            ("Удалить", self._remove),
            ("Импорт", self._import),
            ("Экспорт", self._export),
        ):
            b = QPushButton(t)
            b.clicked.connect(s)
            bl1.addWidget(b)
        bl1.addStretch()
        layout.addLayout(bl1)

        bl2 = QHBoxLayout()
        self.auto_find_btn = QPushButton(
            "🔍 Найти источники автоматически")
        self.auto_find_btn.setToolTip(
            "Найти публичные плейлисты через GitHub API "
            "(live-поиск)\n"
            "и добавить их как источники с автообновлением.")
        self.auto_find_btn.clicked.connect(
            self._auto_find_sources)
        bl2.addWidget(self.auto_find_btn)

        self.remove_generated_btn = QPushButton(
            "🧹 Удалить найденные")
        self.remove_generated_btn.setToolTip(
            "Удалить все источники, добавленные авто-поиском.")
        self.remove_generated_btn.clicked.connect(
            self._remove_generated)
        bl2.addWidget(self.remove_generated_btn)

        bl2.addStretch()

        self.refresh_all_btn = QPushButton("🔄 Обновить всё")
        self.refresh_all_btn.clicked.connect(self._refresh_all)
        bl2.addWidget(self.refresh_all_btn)
        layout.addLayout(bl2)

        self._progress_label = QLabel("")
        layout.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        self._progress_bar.setTextVisible(True)
        layout.addWidget(self._progress_bar)

        self.add_close()

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading or item.column() != 0:
            return
        name_item = self.sources_table.item(item.row(), 1)
        if not name_item:
            return
        src = self.source_manager.get_source_by_name(
            name_item.text())
        if not src:
            return
        src.enabled = (
            item.checkState() == Qt.CheckState.Checked)
        self.source_manager._persist()
        self.sources_updated.emit()

    def _load_sources(self):
        from datetime import datetime
        self._loading = True
        try:
            self.sources_table.setSortingEnabled(False)
            sources = self.source_manager.get_all_sources()
            self.sources_table.setRowCount(len(sources))
            colors = semantic_colors()
            for i, s in enumerate(sources):
                ei = QTableWidgetItem()
                ei.setFlags(Qt.ItemFlag.ItemIsUserCheckable |
                            Qt.ItemFlag.ItemIsEnabled)
                ei.setCheckState(
                    Qt.CheckState.Checked if s.enabled
                    else Qt.CheckState.Unchecked)
                self.sources_table.setItem(i, 0, ei)

                name_text = s.name
                if s.generated:
                    name_text = f"✨ {name_text}"
                ni = QTableWidgetItem(name_text)
                ni.setData(Qt.ItemDataRole.UserRole, s.name)
                if s.generated:
                    ni.setToolTip(
                        "Источник добавлен авто-поиском")
                self.sources_table.setItem(i, 1, ni)

                self.sources_table.setItem(
                    i, 2, QTableWidgetItem(
                        "Локальный"
                        if s.source_type == "local"
                        else "Онлайн"))

                pi = QTableWidgetItem(s.path)
                pi.setToolTip(s.path)
                self.sources_table.setItem(i, 3, pi)

                pr_item = QTableWidgetItem(str(s.priority))
                pr_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignCenter)
                self.sources_table.setItem(i, 4, pr_item)

                self.sources_table.setItem(
                    i, 5, QTableWidgetItem(
                        "Да" if s.auto_update else "Нет"))

                dt = (s.last_updated.strftime("%Y-%m-%d %H:%M")
                      if s.last_updated else "Никогда")
                self.sources_table.setItem(
                    i, 6, QTableWidgetItem(dt))

                display_total = (s.total_links
                                 if s.total_links
                                 else s.raw_total_links)
                self.sources_table.setItem(
                    i, 7, _NumericItem(display_total))

                wrk = _NumericItem(int(s.total_working or 0))
                if s.working_checked_at:
                    ts = datetime.fromtimestamp(
                        s.working_checked_at)
                    wrk.setToolTip(
                        f"Проверено: "
                        f"{ts.strftime('%Y-%m-%d %H:%M')}\n"
                        f"Рабочих: {s.total_working}\n"
                        f"Всего (после ЧС): {s.total_links}")
                    if s.total_working > 0:
                        wrk.setForeground(colors['working'])
                else:
                    wrk.setToolTip("Не проверялось")
                    wrk.setForeground(colors['no_url'])
                self.sources_table.setItem(i, 8, wrk)
        finally:
            self.sources_table.setSortingEnabled(True)
            self._loading = False

    def _existing_names(self) -> Set[str]:
        return {s.name
                for s in self.source_manager.get_all_sources()}

    def _get_current_source_by_row(self, row: int
                                    ) -> Optional[LinkSource]:
        if row < 0:
            return None
        name_item = self.sources_table.item(row, 1)
        if not name_item:
            return None
        raw = name_item.data(Qt.ItemDataRole.UserRole)
        name = raw if isinstance(raw, str) else name_item.text()
        if name.startswith("✨ "):
            name = name[2:]
        return self.source_manager.get_source_by_name(name)

    def _add(self):
        dlg = LinkSourceEditDialog(
            self, existing_names=self._existing_names())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src = dlg.get_source()
            if src and self.source_manager.add_source(src):
                self._load_sources()
                self.sources_updated.emit()

    def _edit(self):
        row = self.sources_table.currentRow()
        if row < 0:
            return
        src = self._get_current_source_by_row(row)
        if not src:
            return
        dlg = LinkSourceEditDialog(self, src)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ns = dlg.get_source()
            if ns and self.source_manager.update_source(
                    src.name, ns):
                self._load_sources()
                self.sources_updated.emit()

    def _remove(self):
        row = self.sources_table.currentRow()
        if row < 0:
            return
        src = self._get_current_source_by_row(row)
        if not src:
            return
        if confirm(self, f"Удалить '{src.name}'?"):
            if self.source_manager.remove_source(src.name):
                self._load_sources()
                self.sources_updated.emit()

    def _auto_find_sources(self):
        core = None
        w = self.parent()
        if w is not None and hasattr(w, 'core'):
            core = w.core
        dlg = SourcesFinderDialog(core, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        items = dlg.get_selected_items()
        if not items:
            info_box(self, "Ничего не выбрано")
            return

        added, skipped = self._add_sources_from_items(items)
        self._load_sources()
        self.sources_updated.emit()

        if added == 0:
            info_box(self,
                     f"Новых источников не добавлено "
                     f"(пропущено: {skipped}).")
            return

        auto_load = bool(self.config.get(
            'sources_finder_auto_load_after_add', True))
        if auto_load or confirm(
                self,
                f"Добавлено источников: {added}.\n"
                f"Пропущено (уже есть): {skipped}.\n\n"
                f"Сразу загрузить их в кэш?"):
            self._refresh_all()
        else:
            info_box(self,
                     f"Источники добавлены в менеджер.\n"
                     f"Загрузите их кнопкой "
                     f"«🔄 Обновить всё».", "Готово")

    def _add_sources_from_items(self, items: List[Dict[str, Any]]
                                 ) -> Tuple[int, int]:
        existing_paths = {
            s.path
            for s in self.source_manager.get_all_sources()}
        existing_names = {
            s.name
            for s in self.source_manager.get_all_sources()}
        added = 0
        skipped = 0
        for item in items:
            url = (item.get('url') or '').strip()
            if not url or url in existing_paths:
                skipped += 1
                continue
            base_name = item.get('name') or url
            name = base_name
            suffix = 2
            while name in existing_names:
                name = f"{base_name} #{suffix}"
                suffix += 1

            src = LinkSource()
            src.name = name
            src.path = url
            src.source_type = "online"
            src.priority = int(item.get('priority') or 5)
            src.enabled = True
            src.auto_update = True
            src.update_interval_hours = int(self.config.get(
                'sources_finder_update_interval_hours',
                SOURCES_FINDER_UPDATE_INTERVAL_HOURS))
            src.encoding = "utf-8"
            src.apply_blacklist = True
            src.apply_domain_blacklist = True
            src.generated = True

            if self.source_manager.add_source(src):
                added += 1
                existing_paths.add(url)
                existing_names.add(name)
        return added, skipped

    def _remove_generated(self):
        generated = [s for s
                     in self.source_manager.get_all_sources()
                     if s.generated]
        if not generated:
            info_box(self,
                     "Нет источников, добавленных "
                     "авто-поиском.")
            return
        if not confirm(
                self,
                f"Удалить {len(generated)} источников, "
                f"добавленных авто-поиском?"):
            return
        n = self.source_manager.remove_generated_sources()
        self._load_sources()
        self.sources_updated.emit()
        info_box(self, f"Удалено: {n}", "Готово")

    def _refresh_all(self):
        sources = self.source_manager.get_enabled_sources()
        if not sources:
            info_box(self, "Нет включённых источников")
            return
        if (self._refresh_worker
                and self._refresh_worker.isRunning()):
            info_box(self, "Уже идёт")
            return

        settings_snapshot: Dict[str, Any] = {}
        bl_index = None
        settings = None
        try:
            from app_core import ApplicationCore
            core = ApplicationCore.try_get_if_gui_thread()
            if core is not None:
                settings_snapshot = \
                    core._build_settings_snapshot()
                bl_index = core.blacklist_manager.build_index()
                settings = core.get_replacement_settings()
        except Exception:
            logger.exception("_refresh_all: snapshot failed")

        self._refresh_worker = SourcesRefreshWorker(
            sources, self.source_manager, self.config,
            check_urls=True,
            settings_snapshot=settings_snapshot,
            blacklist_index=bl_index,
            settings=settings)

        def on_progress(value, total, text):
            v = max(0, min(100, int(value)))
            self._progress_bar.setValue(v)
            self._progress_bar.setFormat(f"{text} (%p%)")
            self._progress_bar.setVisible(True)
            self._progress_label.setText(text)

        def on_source_checked(name, working, total):
            self._progress_label.setText(
                f"✓ Проверено [{name}]: {working}/{total}")

        def on_channel_checked(source, name, alive, msg):
            mark = "✓" if alive else "✗"
            short = (name or "")[:60]
            self._progress_label.setText(
                f"{mark} [{source[:20]}] {short}")

        def on_done(success, total):
            self._load_sources()
            self.sources_updated.emit()
            self._progress_bar.setValue(100)
            self._progress_bar.setFormat(
                f"✓ Обновлено {success} из {total} (100%)")
            self._progress_label.setText(
                f"✓ Обновлено {success} из {total}")
            self._progress_bar.setVisible(False)
            w = self._refresh_worker
            self._refresh_worker = None
            if w is not None:
                QTimer.singleShot(0, w.deleteLater)
            QTimer.singleShot(
                0, lambda s=success, t=total:
                    info_box(
                        self,
                        f"Обновлено {s} из {t}",
                        "Успех"))

        def on_error(msg):
            self._progress_label.setText(
                f"✗ Ошибка: {msg[:80]}")
            self._progress_bar.setVisible(False)
            self._refresh_worker = None
            QTimer.singleShot(
                0, lambda m=msg: error_box(self, m))

        self._refresh_worker.progress.connect(
            on_progress, Qt.ConnectionType.QueuedConnection)
        self._refresh_worker.source_checked.connect(
            on_source_checked, Qt.ConnectionType.QueuedConnection)
        self._refresh_worker.channel_checked.connect(
            on_channel_checked, Qt.ConnectionType.QueuedConnection)
        self._refresh_worker.all_done.connect(
            on_done, Qt.ConnectionType.QueuedConnection)
        self._refresh_worker.error.connect(
            on_error, Qt.ConnectionType.QueuedConnection)
        self._progress_label.setText("⏳ Обновление...")
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("Подготовка... %p%")
        self._progress_bar.setVisible(True)
        self._refresh_worker.start()

    def _import(self):
        def on_items(data: list) -> int:
            imported = 0
            for item in data:
                if not isinstance(item, dict):
                    continue
                src = LinkSource.from_dict(item)
                if src.name and self.source_manager.add_source(src):
                    imported += 1
            self._load_sources()
            self.sources_updated.emit()
            return imported
        n = json_import_dialog(
            self, "Импорт источников", on_items)
        if n is not None:
            info_box(self, f"Импортировано {n}", "Успех")

    def _export(self):
        sources = self.source_manager.get_all_sources()
        if json_export_dialog(
                self, "Экспорт источников",
                "link_sources.json",
                [s.to_dict() for s in sources]):
            info_box(self, f"Экспортировано {len(sources)}",
                     "Успех")

    def closeEvent(self, event):
        w = self._refresh_worker
        self._refresh_worker = None
        if w is not None and w.isRunning():
            with suppress(Exception):
                w.stop()
                if not w.wait(10000):
                    logger.warning(
                        "LinkSourceManagerDialog: worker did not "
                        "finish in 10 sec")
        event.accept()


class LinkSelectionDialog(BaseDialog):
    def __init__(self, channel_name: str,
                 alts: List['ChannelData'], parent=None,
                 settings=None):
        super().__init__(
            f"Выбор ссылки для '{channel_name}'",
            parent, size=(1000, 540))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.alts = alts
        self.settings = settings
        self.selected_index = -1
        self._check_items: Dict[int, QTableWidgetItem] = {}
        self._stop_token = _StopToken()

        l = self.root
        l.addWidget(QLabel(
            f"Найдено {len(alts)} ссылок в разных источниках. "
            f"Выберите нужную:"))

        self.table = make_table(
            ["Источник", "URL", "Приоритет", "Проверка",
             "Выбрать"],
            stretch_last=False,
            select_mode=(
                QAbstractItemView.SelectionMode.SingleSelection))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents)

        from app_core import ApplicationCore
        core = ApplicationCore.instance()
        self.table.setRowCount(len(alts))
        for i, a in enumerate(alts):
            self.table.setItem(
                i, 0,
                QTableWidgetItem(a.link.link_source or "?"))
            ui = QTableWidgetItem(a.link.url)
            ui.setToolTip(a.link.url)
            self.table.setItem(i, 1, ui)

            src = core.link_source_manager.get_source_by_name(
                a.link.link_source)
            pr = src.priority if src else 5
            pi = QTableWidgetItem(str(pr))
            pi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, pi)

            ci = QTableWidgetItem("")
            ci.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 3, ci)
            self._check_items[i] = ci

            btn = QPushButton("Выбрать")
            valid = a.has_valid_url
            btn.setEnabled(valid)
            if not valid:
                btn.setToolTip("Пустой URL")
            btn.clicked.connect(
                lambda checked, idx=i: self._choose(idx))
            self.table.setCellWidget(i, 4, btn)

        l.addWidget(self.table)

        actions = QHBoxLayout()
        self.verify_btn = QPushButton("🔍 Проверить")
        self.verify_btn.setToolTip(
            "Проверить все ссылки-кандидаты по сети")
        self.verify_btn.clicked.connect(self._verify_all)
        if len(alts) <= 1:
            self.verify_btn.setEnabled(False)
        actions.addWidget(self.verify_btn)
        actions.addStretch()
        l.addLayout(actions)

        from PyQt6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel)
        bb.button(
            QDialogButtonBox.StandardButton.Cancel).setText(
            "Отмена")
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

    def _choose(self, idx: int):
        if 0 <= idx < len(self.alts):
            url = self.alts[idx].link.url
            if not url or not url.strip():
                if _is_qobject_valid(self):
                    warn_box(self,
                             "Пустой URL нельзя выбрать")
                return
        self.selected_index = idx
        self.accept()

    def _verify_all(self):
        urls = [a.link.url for a in self.alts if a.has_valid_url]
        if not urls:
            return
        if self.settings is not None:
            timeout = self.settings.get_verify_candidates_timeout()
            workers = \
                self.settings.get_verify_candidates_max_workers()
        else:
            timeout = 3
            workers = 4
        workers = max(1, min(workers, len(urls)))

        self.verify_btn.setEnabled(False)
        self.verify_btn.setText("⏳ Проверка...")
        QApplication.setOverrideCursor(
            Qt.CursorShape.WaitCursor)
        try:
            from concurrent.futures import (
                ThreadPoolExecutor, as_completed)
            results: Dict[str, tuple] = {}

            def _check(u: str):
                try:
                    ok, rt, msg, code = URLUtils.check_url(
                        u, timeout, verify_ssl=False,
                        stop_token=self._stop_token)
                except Exception:
                    logger.exception("verify %s", u[:80])
                    ok, rt = False, None
                return u, ok, rt

            with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="ls-verify") as ex:
                futures = {ex.submit(_check, u): u
                           for u in urls}
                for fut in as_completed(futures):
                    if self._stop_token.is_set():
                        for f in futures:
                            with suppress(Exception):
                                f.cancel()
                        break
                    try:
                        u, ok, rt = fut.result()
                    except Exception:
                        continue
                    results[u] = (ok, rt)
            for i, a in enumerate(self.alts):
                item = self._check_items.get(i)
                if item is None:
                    continue
                ok, rt = results.get(a.link.url, (None, None))
                if ok is True:
                    rt_txt = f"{rt:.2f}s" if rt else "ok"
                    item.setText(f"✓ {rt_txt}")
                elif ok is False:
                    item.setText("✗")
                else:
                    item.setText("—")
        finally:
            QApplication.restoreOverrideCursor()
            self.verify_btn.setText("🔍 Проверить")
            self.verify_btn.setEnabled(len(self.alts) > 1)

    def get_selected(self) -> Optional['ChannelData']:
        if 0 <= self.selected_index < len(self.alts):
            return self.alts[self.selected_index]
        return None

    def closeEvent(self, event):
        with suppress(Exception):
            self._stop_token.set()
        super().closeEvent(event)

    def reject(self):
        with suppress(Exception):
            self._stop_token.set()
        super().reject()


class PlaylistFromSourcesDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Создать плейлист из источников",
                          parent, size=(720, 680))
        self.core = core
        self._sources_snapshot: List[LinkSource] = []
        self._setup_ui()
        self._load_sources()
        self._refresh_cache_status()

    def _setup_ui(self):
        layout = self.root
        layout.addWidget(hint_label(
            "Приложение не содержит встроенных источников. "
            "Добавьте их в менеджере источников или найдите "
            "через «🔍 Найти источники автоматически»."))
        layout.addWidget(QLabel(
            "Выберите источники для нового плейлиста:"))

        self.cache_status_label = QLabel("")
        self.cache_status_label.setWordWrap(True)
        self.cache_status_label.setTextFormat(
            Qt.TextFormat.RichText)
        layout.addWidget(self.cache_status_label)

        cache_row = QHBoxLayout()
        self.refresh_cache_btn = QPushButton(
            "🔄 Обновить всё (загрузить + проверить URL)")
        self.refresh_cache_btn.setToolTip(
            "Открывает менеджер источников и запускает "
            "«🔄 Обновить всё».\n"
            "После этого normalized_index заполнится только "
            "живыми ссылками.")
        self.refresh_cache_btn.clicked.connect(
            self._open_sources_manager)
        cache_row.addWidget(self.refresh_cache_btn)
        cache_row.addStretch()
        layout.addLayout(cache_row)

        self.sources_table = make_table(
            ["Вкл", "Название", "Путь/URL"],
            stretch_last=False,
            select_rows=False)
        self.sources_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        h = self.sources_table.horizontalHeader()
        h.setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.sources_table.verticalHeader().setVisible(False)
        layout.addWidget(self.sources_table)

        options_group = QGroupBox("Параметры")
        options_layout = QFormLayout(options_group)

        self.dedupe_by_url_check = QCheckBox(
            "Удалять дубликаты по URL "
            "(оставить первый канал)")
        self.dedupe_by_url_check.setChecked(True)
        options_layout.addRow(self.dedupe_by_url_check)

        self.preserve_order_check = QCheckBox(
            "Сохранять порядок из источников "
            "(по приоритету)")
        self.preserve_order_check.setChecked(True)
        options_layout.addRow(self.preserve_order_check)

        self.apply_ua_check = QCheckBox(
            "Применять правила User-Agent по доменам")
        self.apply_ua_check.setChecked(True)
        options_layout.addRow(self.apply_ua_check)

        self.apply_bl_check = QCheckBox(
            "Применять чёрный список каналов (УДАЛЯЕТ)")
        self.apply_bl_check.setChecked(True)
        options_layout.addRow(self.apply_bl_check)

        self.apply_domain_bl_check = QCheckBox(
            "Применять ЧС домен/IP "
            "(ОЧИЩАЕТ ссылки, каналы сохраняются)")
        self.apply_domain_bl_check.setChecked(True)
        options_layout.addRow(self.apply_domain_bl_check)

        self.apply_epg_check = QCheckBox(
            "Применять метаданные из EPG (если загружен)")
        self.apply_epg_check.setChecked(True)
        options_layout.addRow(self.apply_epg_check)

        layout.addWidget(options_group)

        from PyQt6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.button(
            QDialogButtonBox.StandardButton.Ok).setText(
            "Создать")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _refresh_cache_status(self):
        try:
            st = self.core.get_normalized_index_stats()
        except Exception:
            logger.exception("get_normalized_index_stats")
            st = {}
        count = int(st.get('count', 0) or 0)
        sources = int(st.get('sources', 0) or 0)
        fresh = int(st.get('fresh_count', 0) or 0)
        is_fresh = bool(st.get('is_fresh', False))
        settings_match = bool(st.get('settings_match', False))

        if count <= 0:
            self.cache_status_label.setText(
                "<b>Кэш живых URL:</b> пуст.<br>"
                "Будет выполнена полная загрузка источников "
                "с проверкой URL (медленно).<br>"
                "<i>Совет: нажмите «🔄 Обновить всё», чтобы "
                "наполнить кэш и ускорить создание.</i>")
        elif not settings_match:
            self.cache_status_label.setText(
                f"<b>Кэш живых URL:</b> {count} записей, "
                f"{sources} источников.<br>"
                f"<span style='color:#b00020;'>Настройки "
                f"нормализации изменились — кэш устарел.</span><br>"
                f"Будет выполнена полная загрузка источников.")
        elif is_fresh:
            self.cache_status_label.setText(
                f"<b>Кэш живых URL:</b> {fresh} свежих записей "
                f"({sources} источников).<br>"
                f"<span style='color:#1a7f37;'>Плейлист будет "
                f"создан из кэша (только живые ссылки) — "
                f"быстро.</span>")
        else:
            self.cache_status_label.setText(
                f"<b>Кэш живых URL:</b> {count} записей "
                f"({sources} источников), но устарел.<br>"
                f"Будет выполнена полная загрузка источников.")

    def _open_sources_manager(self):
        try:
            from dialogs import LinkSourceManagerDialog
            w = self.parent()
            if w is None or not hasattr(w, '_track_dialog'):
                return
            dlg = w._track_dialog(LinkSourceManagerDialog(
                self.core.link_source_manager,
                self.core.config, w))
            dlg.sources_updated.connect(
                self.core.sources_updated.emit)
            dlg.exec()
        except Exception:
            logger.exception("open_sources_manager")
        finally:
            self._refresh_cache_status()

    def _load_sources(self):
        self._sources_snapshot = [
            s.copy() for s in self.core.get_sources()]
        self.sources_table.setRowCount(
            len(self._sources_snapshot))
        for i, s in enumerate(self._sources_snapshot):
            chk_item = QTableWidgetItem()
            chk_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable |
                Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(
                Qt.CheckState.Checked if s.enabled
                else Qt.CheckState.Unchecked)
            self.sources_table.setItem(i, 0, chk_item)
            self.sources_table.setItem(
                i, 1, QTableWidgetItem(s.name))
            self.sources_table.setItem(
                i, 2, QTableWidgetItem(s.path))

    def _selected_sources(self) -> List[LinkSource]:
        result = []
        for i, s in enumerate(self._sources_snapshot):
            item = self.sources_table.item(i, 0)
            if (item
                    and item.checkState()
                    == Qt.CheckState.Checked):
                result.append(s)
        return result

    def get_result(self) -> PlaylistCreationOptions:
        return PlaylistCreationOptions(
            sources=self._selected_sources(),
            dedupe_by_url=self.dedupe_by_url_check.isChecked(),
            preserve_order=self.preserve_order_check.isChecked(),
            apply_ua=self.apply_ua_check.isChecked(),
            apply_blacklist=self.apply_bl_check.isChecked(),
            apply_domain_blacklist=(
                self.apply_domain_bl_check.isChecked()),
            apply_epg=self.apply_epg_check.isChecked(),
        )

    def accept(self):
        if not self._selected_sources():
            warn_box(self, "Выберите хотя бы один источник")
            return
        super().accept()