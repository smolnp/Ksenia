# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""HelpBrowser, HelpDialog, SupportDialog."""
from __future__ import annotations

import os
import re
from contextlib import suppress

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import (
    QDesktopServices, QKeySequence, QPalette, QShortcut)
from PyQt6.QtWidgets import (
    QApplication, QDialogButtonBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget)

from constants import (APP_VERSION, CLOSE_BB, DONATION_URL,
                        DONATION_WALLET)
from dialogs_base import BaseDialog
from logging_setup import logger
from paths import error_box, open_external


_HEADING_RE = re.compile(
    r'<h([2-3])([^>]*)>(.*?)</h\1>',
    re.DOTALL | re.IGNORECASE)
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']',
                          re.IGNORECASE)


def _generate_toc(html: str) -> str:
    counter = [0]
    items = []

    def _replace(m):
        level = int(m.group(1))
        attrs = m.group(2) or ""
        inner = m.group(3) or ""
        id_match = _ID_ATTR_RE.search(attrs)
        if id_match:
            anchor = id_match.group(1)
        else:
            counter[0] += 1
            anchor = f"section-{counter[0]}"
            attrs = attrs + f' id="{anchor}"'
        title = re.sub(r"<[^>]+>", "", inner).strip()
        if title:
            indent = "" if level == 2 else "&nbsp;" * 4
            items.append(
                f'<li><a href="#{anchor}">'
                f'{indent}{title}</a></li>')
        return f'<h{level}{attrs}>{inner}</h{level}>'

    html_with_ids = _HEADING_RE.sub(_replace, html)
    if not items:
        return html_with_ids
    toc_html = '<ul>' + ''.join(items) + '</ul>'
    return re.sub(
        r'<div\s+id\s*=\s*["\']toc["\']\s*>\s*</div>',
        f'<div id="toc">{toc_html}</div>',
        html_with_ids, count=1, flags=re.IGNORECASE)


class HelpBrowser(BaseDialog):
    def __init__(self, index_path: str, parent=None):
        super().__init__("Справка — Ksenia M3U Editor", parent,
                          size=(1000, 720))

        self.index_path = os.path.abspath(index_path)
        self.help_dir = os.path.dirname(self.index_path)
        self._current_file = ""
        self._theme_handler_connected = False

        if not os.path.isfile(self.index_path):
            error_box(
                self,
                f"Файл справки не найден:\n{self.index_path}",
                "Справка")
            QTimer.singleShot(0, self.reject)
            return

        nav = QHBoxLayout()
        self.btn_home = QPushButton("🏠 Домой")
        self.btn_refresh = QPushButton("🔄")
        self.btn_refresh.setToolTip("Обновить страницу (F5)")
        nav.addWidget(self.btn_home)
        nav.addWidget(self.btn_refresh)
        nav.addStretch()

        self.path_label = QLabel("")
        self.path_label.setForegroundRole(
            QPalette.ColorRole.PlaceholderText)
        f = self.path_label.font()
        f.setPointSize(max(8, f.pointSize() - 1))
        self.path_label.setFont(f)
        nav.addWidget(self.path_label)
        self.root.addLayout(nav)

        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(False)
        self.viewer.setOpenLinks(False)
        self.viewer.anchorClicked.connect(self._on_anchor)
        self.viewer.setLineWrapMode(
            QTextBrowser.LineWrapMode.WidgetWidth)
        self.root.addWidget(self.viewer, 1)

        self.btn_home.clicked.connect(self._go_home)
        self.btn_refresh.clicked.connect(self._reload)
        QShortcut(QKeySequence("F5"), self,
                  activated=self._reload)
        QShortcut(QKeySequence("Home"), self,
                  activated=self._go_home)

        self._go_home()

        app = QApplication.instance()
        if app is not None:
            try:
                app.paletteChanged.connect(
                    self._on_palette_changed)
                self._theme_handler_connected = True
            except Exception:
                logger.exception("connect paletteChanged")

        bb = QDialogButtonBox(CLOSE_BB)
        bb.rejected.connect(self.reject)
        self.root.addWidget(bb)

    def _on_palette_changed(self, *_args):
        self._reload()

    def _prepare_html(self, raw_html: str) -> str:
        html = _generate_toc(raw_html)
        extra = (
            "<style>"
            "pre, code, kbd { font-family: 'Consolas', "
            "'Menlo', 'Liberation Mono', monospace; }"
            "table { border-collapse: collapse; }"
            "th, td { padding: 4px 8px; }"
            "blockquote { margin: 8px 0; padding-left: 12px; }"
            "</style>"
        )
        if "</head>" in html.lower():
            html = re.sub(r'</head>', extra + '</head>', html,
                          count=1, flags=re.IGNORECASE)
        else:
            html = extra + html
        return html

    def _load_html_page(self, file_path: str, fragment: str = "",
                        preserve_scroll: bool = True):
        if not file_path or not os.path.isfile(file_path):
            logger.warning("Help file not found: %s", file_path)
            return
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw_html = f.read()
        except OSError:
            logger.exception("read %s", file_path)
            return

        html = self._prepare_html(raw_html)
        base_url = QUrl.fromLocalFile(
            os.path.abspath(file_path))
        self.viewer.document().setBaseUrl(base_url)
        self.viewer.setSearchPaths([
            os.path.dirname(file_path),
            self.help_dir,
        ])

        scroll = (self.viewer.verticalScrollBar().value()
                  if preserve_scroll else 0)

        self._current_file = file_path
        self.viewer.setHtml(html)
        self.path_label.setText(os.path.basename(file_path))

        if fragment:
            QTimer.singleShot(
                0, lambda f=fragment:
                    self.viewer.scrollToAnchor(f))
        else:
            QTimer.singleShot(
                0, lambda v=scroll:
                    self.viewer.verticalScrollBar().setValue(v))

    def _go_home(self):
        self._load_html_page(self.index_path,
                             preserve_scroll=False)

    def _reload(self):
        target = self._current_file or self.index_path
        self._load_html_page(target, preserve_scroll=True)

    def _on_anchor(self, url: QUrl):
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        if url.scheme() == "" and url.path() == "":
            self.viewer.scrollToAnchor(url.fragment())
            return
        if url.scheme() == "":
            base_dir = (os.path.dirname(self._current_file)
                        if self._current_file else self.help_dir)
            target = os.path.normpath(
                os.path.join(base_dir, url.path()))
            if not os.path.isfile(target):
                target = os.path.normpath(
                    os.path.join(self.help_dir, url.path()))
            if not os.path.isfile(target):
                logger.warning("Help link not found: %s",
                                url.path())
                return
            self._load_html_page(target,
                                 fragment=url.fragment(),
                                 preserve_scroll=False)
            return
        QDesktopServices.openUrl(url)

    def closeEvent(self, event):
        app = QApplication.instance()
        if (app is not None
                and self._theme_handler_connected):
            with suppress(TypeError, RuntimeError):
                app.paletteChanged.disconnect(
                    self._on_palette_changed)
            self._theme_handler_connected = False
        super().closeEvent(event)


class SupportDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__("Поддержка проекта", parent,
                          size=(500, 400))
        self.setWindowModality(
            Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(500)
        l = self.root

        title = QLabel("☕ Поддержать проект")
        f = title.font()
        f.setPointSize(16)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l.addWidget(title)

        info = QLabel(
            "Если вам нравится этот инструмент и вы хотите "
            "поддержать его развитие,\n"
            "вы можете отправить добровольное пожертвование.\n\n"
            "Спасибо за вашу поддержку! 💝")
        info.setWordWrap(True)
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l.addWidget(info)

        btn = QPushButton("💰 Отправить перевод")
        btn.setMinimumHeight(50)
        btn.clicked.connect(lambda: open_external(DONATION_URL))
        l.addWidget(btn)

        wallet_label = QLabel(
            f"Кошелёк: <b>{DONATION_WALLET}</b> (ЮMoney)")
        wallet_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wallet_label.setTextFormat(Qt.TextFormat.RichText)
        l.addWidget(wallet_label)

        self.add_close()


class HelpDialog(BaseDialog):
    def __init__(self, parent=None):
        super().__init__("Справка", parent, size=(700, 600))
        self.setMinimumWidth(700)
        self.setMinimumHeight(600)
        layout = self.root
        tabs = QTabWidget()

        general = QWidget()
        gl = QVBoxLayout(general)
        gl.addWidget(self._html_label(
            f"<h2>Ksenia M3U Editor {APP_VERSION}</h2>"
            "<p>Редактор и менеджер IPTV плейлистов в формате "
            "M3U/M3U8.</p>"
            "<h3>О проекте</h3>"
            "<table cellpadding='4'>"
            "<tr><td><b>Автор:</b></td>"
            "<td>SmolNP (Смольянинов Николай)</td></tr>"
            "<tr><td><b>Профиль автора:</b></td>"
            "<td><a href='https://github.com/smolnp'>"
            "https://github.com/smolnp</a></td></tr>"
            "<tr><td><b>Репозиторий:</b></td>"
            "<td><a href='https://github.com/smolnp/Ksenia'>"
            "https://github.com/smolnp/Ksenia</a></td></tr>"
            "<tr><td><b>Версия:</b></td>"
            f"<td>{APP_VERSION}</td></tr>"
            "<tr><td><b>Лицензия:</b></td>"
            "<td>GNU GPL v3.0</td></tr>"
            "</table>"
            "<p style='margin-top:1em;'>"
            "<b>F1</b> — открыть встроенное руководство "
            "пользователя.</p>"))
        gl.addStretch()
        tabs.addTab(general, "О проекте")

        shortcuts = QWidget()
        shl = QVBoxLayout(shortcuts)
        shl.addWidget(self._html_label(
            "<h3>Горячие клавиши</h3>"
            "<ul>"
            "<li><b>F1</b> — руководство пользователя</li>"
            "<li><b>F11</b> — полноэкранный режим</li>"
            "<li><b>Ctrl+N</b> — новый плейлист</li>"
            "<li><b>Ctrl+O</b> — открыть</li>"
            "<li><b>Ctrl+S</b> — сохранить</li>"
            "<li><b>Ctrl+Z / Ctrl+Y</b> — отменить/повторить</li>"
            "<li><b>Ctrl+C / X / V</b> — "
            "копировать/вырезать/вставить</li>"
            "<li><b>Delete</b> — удалить канал</li>"
            "</ul>"))
        shl.addStretch()
        tabs.addTab(shortcuts, "Клавиши")

        lt_tab = QWidget()
        lt_l = QVBoxLayout(lt_tab)
        lt_l.setContentsMargins(8, 8, 8, 8)
        lt_header = QLabel("GNU General Public License v3.0")
        _f = lt_header.font()
        _f.setPointSize(14)
        _f.setBold(True)
        lt_header.setFont(_f)
        lt_l.addWidget(lt_header)
        lt_text = QPlainTextEdit()
        lt_text.setReadOnly(True)
        lt_text.setPlainText(
            "This program is free software: you can redistribute "
            "it and/or modify it under the terms of the GNU "
            "General Public License as published by the Free "
            "Software Foundation, either version 3 of the License, "
            "or (at your option) any later version.\n\n"
            "This program is distributed in the hope that it will "
            "be useful, but WITHOUT ANY WARRANTY; without even the "
            "implied warranty of MERCHANTABILITY or FITNESS FOR A "
            "PARTICULAR PURPOSE. See the GNU General Public "
            "License for more details.\n\n"
            "You should have received a copy of the GNU General "
            "Public License along with this program. If not, see "
            "<https://www.gnu.org/licenses/>.\n")
        lt_l.addWidget(lt_text, 1)
        lt_link = QLabel(
            "<a href='https://www.gnu.org/licenses/"
            "gpl-3.0.html'>"
            "Полный текст лицензии GPLv3</a>")
        lt_link.setOpenExternalLinks(True)
        lt_l.addWidget(lt_link)
        tabs.addTab(lt_tab, "Лицензия GPLv3")

        sup_tab = QWidget()
        spl = QVBoxLayout(sup_tab)
        spl.addWidget(QLabel(
            "Поддержать развитие проекта:"))
        b = QPushButton("☕ Поддержать проект")
        b.clicked.connect(lambda: SupportDialog(self).exec())
        spl.addWidget(b)
        spl.addStretch()
        tabs.addTab(sup_tab, "Поддержка")

        layout.addWidget(tabs)
        self.add_close()

    @staticmethod
    def _html_label(html: str) -> QLabel:
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(True)
        return lbl