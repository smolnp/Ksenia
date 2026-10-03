#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ksenia M3U Editor — точка входа.

Ранняя настройка:
  • PYTHONPYCACHEPREFIX → <config>/ksenia/pycache
    Все .pyc-файлы проекта пишутся в конфиг-папку Ksenia,
    а не в __pycache__ рядом с исходниками.
  • Удаление локального __pycache__ рядом с ksenia.py
    (страховка от прямого запуска `python ksenia.py`).
"""

from __future__ import annotations

import os
import sys
import shutil
import signal
import faulthandler
from contextlib import suppress


# =====================================================================
# 1. Ранняя настройка путей Python-кэша (.pyc) ДО импорта проекта.
# =====================================================================
# PYTHONPYCACHEPREFIX влияет только на модули, импортированные ПОСЛЕ
# его установки. Поэтому ставим максимально рано — до импорта
# ksenia_window / config / constants / paths / models / dialogs / и т.д.
#
# Если переменная уже задана извне (лаунчер, IDE, systemd) — уважаем её.
# =====================================================================

def _get_ksenia_config_dir() -> str:
    """Дублирует Paths.get_config_dir() из paths.py без импорта проекта."""
    if sys.platform.startswith("linux"):
        config_home = os.environ.get(
            'XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
        return os.path.join(config_home, "ksenia")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Ksenia")
    appdata = os.environ.get('APPDATA')
    if appdata:
        return os.path.join(appdata, "Ksenia")
    return os.path.expanduser("~/.ksenia")


def _setup_pycache_prefix() -> None:
    """Устанавливает PYTHONPYCACHEPREFIX в конфиг-папку Ksenia.

    Все .pyc проекта пойдут в <config>/ksenia/pycache/,
    а не в __pycache__ рядом с исходниками.

    Отключается флагом --no-pycache-prefix.
    """
    if os.environ.get('PYTHONPYCACHEPREFIX'):
        return  # уже задано извне (лаунчер, IDE, systemd) — не трогаем
    if '--no-pycache-prefix' in sys.argv:
        return
    try:
        pycache_dir = os.path.join(_get_ksenia_config_dir(), "pycache")
        os.makedirs(pycache_dir, exist_ok=True)
        os.environ['PYTHONPYCACHEPREFIX'] = pycache_dir
    except Exception:
        # Не критично — останется стандартное поведение Python.
        pass


def _cleanup_local_pycache() -> None:
    """Удаляет __pycache__ рядом с ksenia.py (страховка).

    Сам ksenia.py компилируется интерпретатором ДО выполнения первой
    строки, поэтому __pycache__/ksenia.cpython-XXX.pyc успевает
    появиться. Эта функция его убирает. Остальные модули проекта
    уже идут в PYTHONPYCACHEPREFIX и в проекте не мусорят.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        pycache = os.path.join(here, "__pycache__")
        if os.path.isdir(pycache):
            shutil.rmtree(pycache, ignore_errors=True)
    except Exception:
        pass


# --- Применяем настройку ДО импорта любых модулей проекта ---
_setup_pycache_prefix()
_cleanup_local_pycache()


# =====================================================================
# 2. Дальше — оригинальная точка входа Ksenia (без изменений логики).
# =====================================================================

faulthandler.enable()

from ksenia_window import parse_cli_args, run_cli, MainWindow
from constants import APP_VERSION
from paths import logger


def _install_signal_handlers(app):
    def handler(signum, frame):
        logger.info(f"Получен сигнал {signum}, завершение...")
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(ValueError, OSError):
            signal.signal(sig, handler)


def main():
    from PyQt6.QtWidgets import QApplication

    import argparse as _argparse
    _parser = _argparse.ArgumentParser(add_help=False)
    _parser.add_argument('--headless', action='store_true')
    _parser.add_argument('--no-pycache-prefix', action='store_true')
    _known, _ = _parser.parse_known_args()
    if _known.headless:
        args = parse_cli_args()
        try:
            rc = run_cli(args)
        except Exception as e:
            logger.exception("CLI error")
            print(f"Ошибка: {e}", file=sys.stderr)
            rc = 1
        sys.exit(rc)

    app = QApplication([sys.argv[0]])
    app.setApplicationName(f"Ksenia M3U Editor {APP_VERSION}")
    app.setOrganizationName("Ksenia")
    app.setQuitOnLastWindowClosed(True)
    _install_signal_handlers(app)
    window = MainWindow()
    window.show()
    rc = app.exec()
    sys.exit(rc)


if __name__ == "__main__":
    main()