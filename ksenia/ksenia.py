# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Ksenia M3U Editor — точка входа.

Про `--windowed`-сборку PyInstaller:
    В этом режиме у приложения НЕТ консоли, `sys.stderr`
    и `sys.stdout` равны None. Любой `print(..., file=sys.stderr)`
    или `faulthandler.enable()` без явного file падает с
    `RuntimeError: sys.stderr is None`. Поэтому все записи в
    stderr обёрнуты в `_safe_stderr_write`, а faulthandler
    в `--windowed` пишет в файл `faulthandler.log`.

Иконка приложения:
    Берётся из `assets/ksenia.ico` рядом с `.exe`
    (для `--onedir`-сборки) или из `assets/` рядом с
    `ksenia.py` (при запуске из исходников).
"""
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


def _get_app_dir() -> str:
    """Каталог приложения: для `--onedir`-сборки — рядом
    с `.exe`, иначе — рядом с `ksenia.py`."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _safe_stderr_write(msg: str) -> None:
    """Пишет в sys.stderr, если он существует.

    В `--windowed`-сборке PyInstaller `sys.stderr is None`,
    и обычный `sys.stderr.write(...)` бросает AttributeError.
    Эта обёртка молча игнорирует отсутствие stderr.
    """
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
            sys.stderr.flush()
    except Exception:
        pass


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


def _setup_faulthandler() -> None:
    """Включает faulthandler, не падая без stderr."""
    try:
        if sys.stderr is not None:
            faulthandler.enable()
            return
    except Exception:
        pass

    try:
        d = _get_ksenia_config_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "faulthandler.log")
        fh = open(path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=fh, all_threads=True)
        globals()['_faulthandler_file'] = fh
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


def _gui_error(title: str, message: str) -> None:
    """Показывает MessageBox, если GUI уже инициализирован."""
    shown = False
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            QMessageBox.critical(None, title, message)
            shown = True
    except Exception:
        pass
    if not shown:
        _safe_stderr_write(f"{title}: {message}\n")


def _check_requirements(headless: bool) -> None:
    """Проверяет, что PyQt6 и requests импортируются."""
    missing = []
    try:
        import PyQt6  # noqa
    except ImportError:
        missing.append("PyQt6 (pip install PyQt6)")
    try:
        import requests  # noqa
    except ImportError:
        missing.append("requests (pip install requests)")

    if not missing:
        return

    msg = ("Ksenia M3U Editor: missing dependencies:\n"
           + "".join(f"  - {m}\n" for m in missing)
           + "\nInstall: pip install PyQt6 requests urllib3\n"
             "         and mpv (system package)\n")
    _safe_stderr_write(msg)

    if headless:
        sys.exit(2)

    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        _tmp_app = (QApplication.instance()
                    or QApplication([sys.argv[0]]))
        QMessageBox.critical(
            None, "Ksenia M3U Editor",
            "Не найдены зависимости:\n"
            + "\n".join(f"• {m}" for m in missing))
    except Exception:
        pass
    sys.exit(2)


def _setup_pyinstaller_paths() -> None:
    """Хук для `--onedir`-сборки: `assets/` и `help/`
    лежат рядом с `.exe`. Сейчас ничего не делает —
    стандартное поведение PyInstaller корректно."""
    if getattr(sys, 'frozen', False):
        pass


def main() -> None:
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
        except SystemExit:
            raise
        except Exception as e:
            from logging_setup import logger
            logger.exception("CLI error")
            _safe_stderr_write(f"Ошибка: {e}\n")
            sys.exit(1)

    # ── GUI ──
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

    # ── Иконка приложения (панель задач, alt-tab) ──
    _icon_path = os.path.join(_get_app_dir(), 'assets',
                              'ksenia.ico')
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))
    else:
        logger.debug("Application icon not found: %s",
                     _icon_path)

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

    try:
        window = MainWindow()
        window.show()
    except Exception as e:
        logger.exception("Не удалось создать главное окно")
        _gui_error(
            "Ksenia M3U Editor",
            f"Не удалось создать главное окно:\n{e}\n\n"
            f"Подробности в логе: "
            f"{_get_ksenia_config_dir()}\\editor.log")
        sys.exit(1)

    sys.exit(app.exec())


_setup_pycache_prefix()
_setup_faulthandler()
_setup_pyinstaller_paths()


if __name__ == "__main__":
    main()