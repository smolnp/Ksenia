# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Ksenia M3U Editor — точка входа."""
from __future__ import annotations

import faulthandler
import os
import sys
from contextlib import suppress


def _get_ksenia_config_dir() -> str:
    if sys.platform.startswith("linux"):
        base = os.environ.get('XDG_CONFIG_HOME',
                              os.path.expanduser('~/.config'))
        return os.path.join(base, "ksenia")
    appdata = os.environ.get('APPDATA')
    if appdata:
        return os.path.join(appdata, "Ksenia")
    return os.path.expanduser("~/.ksenia")


def _setup_pycache_prefix() -> None:
    if getattr(sys, 'pycache_prefix', None):
        return
    if '--no-pycache-prefix' in sys.argv:
        return
    try:
        d = os.path.join(_get_ksenia_config_dir(), "pycache")
        os.makedirs(d, exist_ok=True)
        sys.pycache_prefix = d
    except Exception:
        pass


def _setup_qt_platform(headless: bool) -> None:
    if headless or not sys.platform.startswith("linux"):
        return
    if "QT_QPA_PLATFORM" in os.environ:
        return
    if os.environ.get("WAYLAND_DISPLAY"):
        return
    if not os.environ.get("DISPLAY"):
        return
    os.environ["QT_QPA_PLATFORM"] = "xcb"


def _check_requirements(headless: bool) -> None:
    missing = []
    try:
        import PyQt6  # noqa
    except ImportError:
        missing.append("PyQt6 (pip install PyQt6)")
    try:
        import requests  # noqa
    except ImportError:
        missing.append("requests (pip install requests)")
    if missing:
        sys.stderr.write("Ksenia M3U Editor: "
                         "missing dependencies:\n")
        for m in missing:
            sys.stderr.write(f"  - {m}\n")
        sys.stderr.write(
            "\nInstall: pip install PyQt6 requests urllib3\n"
            "         and mpv (system package)\n")
        sys.exit(2)


_setup_pycache_prefix()
faulthandler.enable()


def main():
    import argparse as _argparse
    _parser = _argparse.ArgumentParser(add_help=False)
    _parser.add_argument('--headless', action='store_true')
    _parser.add_argument('--no-pycache-prefix', action='store_true')
    _known, _ = _parser.parse_known_args()

    _check_requirements(_known.headless)
    _setup_qt_platform(_known.headless)

    if _known.headless:
        from cli import parse_cli_args, run_cli
        args = parse_cli_args()
        try:
            sys.exit(run_cli(args))
        except Exception as e:
            from logging_setup import logger
            logger.exception("CLI error")
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication
    from constants import APP_VERSION, APP_ORG, APP_NAME
    from theme import setup_icon_theme
    from ksenia_window import MainWindow
    from logging_setup import logger

    with suppress(AttributeError):
        QIcon.setFallbackThemeName("hicolor")

    app = QApplication([sys.argv[0]])
    app.setOrganizationName(APP_ORG)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(
        f"Ksenia M3U Editor {APP_VERSION}")
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName("org.ksenia.M3UEditor")
    app.setQuitOnLastWindowClosed(True)

    try:
        setup_icon_theme()
    except Exception:
        logger.exception("setup_icon_theme")

    from cli import _install_signal_handlers
    _install_signal_handlers(app)

    if sys.platform.startswith("win"):
        from PyQt6.QtCore import QTimer
        timer = QTimer(app)
        timer.start(500)
        timer.timeout.connect(lambda: None)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()