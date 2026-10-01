#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ksenia M3U Editor — точка входа.

Запуск:
    python -m ksenia.ksenia
    или
    python ksenia/ksenia.py
"""
from __future__ import annotations

import sys
import os
import signal
import faulthandler
from contextlib import suppress

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


def _install_exit_watchdog():
    import threading as _t

    def _watchdog():
        _t.Event().wait(3.0)
        logger.warning("[watchdog] Принудительное завершение (sys.exit)")
        try:
            sys.exit(1)
        except SystemExit:
            pass
        _t.Event().wait(1.0)
        logger.warning("[watchdog] os._exit(0) — жёсткий выход")
        os._exit(0)

    _t.Thread(target=_watchdog, daemon=True,
              name="exit-watchdog").start()


def main():
    from PyQt6.QtWidgets import QApplication

    import argparse as _argparse
    _parser = _argparse.ArgumentParser(add_help=False)
    _parser.add_argument('--headless', action='store_true')
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
    _install_exit_watchdog()
    sys.exit(rc)


if __name__ == "__main__":
    main()
