# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Qt-константы: YES_NO, OK_CANCEL_BB, CLOSE_BB.

P0.7-fix: эти константы должны жить в отдельном модуле, потому
что constants.py НЕ должен импортировать QtWidgets (headless),
а paths.py и dialogs_base.py не могут импортировать друг друга
(циклический импорт).

qt_constants.py импортирует ТОЛЬКО PyQt6.QtWidgets — никаких
зависимостей от проекта. Поэтому его безопасно импортировать
из любого GUI-модуля.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QDialogButtonBox, QMessageBox


# ── QMessageBox ──
YES_NO = (QMessageBox.StandardButton.Yes
          | QMessageBox.StandardButton.No)

YES_NO_CANCEL = (QMessageBox.StandardButton.Yes
                 | QMessageBox.StandardButton.No
                 | QMessageBox.StandardButton.Cancel)

OK_ONLY = QMessageBox.StandardButton.Ok


# ── QDialogButtonBox ──
OK_CANCEL_BB = (QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel)
CLOSE_BB = QDialogButtonBox.StandardButton.Close
OK_BB = QDialogButtonBox.StandardButton.Ok
CANCEL_BB = QDialogButtonBox.StandardButton.Cancel