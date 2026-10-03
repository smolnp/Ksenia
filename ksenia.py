#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ksenia M3U Editor — точка входа.

Ранняя настройка:
  • PYTHONPYCACHEPREFIX → <config>/ksenia/pycache
  • Удаление локального __pycache__ рядом с ksenia.py
"""

from __future__ import annotations

import faulthandler
import os
import shutil
import signal
import sys
from contextlib import suppress


def _get_ksenia_config_dir() -> str:
    """Дублирует Paths.get_config_dir() без импорта проекта."""
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
    if os.environ.get('PYTHONPYCACHEPREFIX'):
        return
    if '--no-pycache-prefix' in sys.argv:
        return
    try:
        pycache_dir = os.path.join(_get_ksenia_config_dir(), "pycache")
        os.makedirs(pycache_dir, exist_ok=True)
        os.environ['PYTHONPYCACHEPREFIX'] = pycache_dir
    except Exception:
        pass


def _cleanup_local_pycache() -> None:
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        pycache = os.path.join(here, "__pycache__")
        if os.path.isdir(pycache):
            shutil.rmtree(pycache, ignore_errors=True)
    except Exception:
        pass


_setup_pycache_prefix()
_cleanup_local_pycache()


faulthandler.enable()

from ksenia_window import MainWindow, parse_cli_args, run_cli
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