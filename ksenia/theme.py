# -*- coding: utf-8 -*-
"""theme — единый источник цветов и имени темы иконок.

Все цвета берутся из QApplication.palette(). Никаких жёстких
QColor(...) в других модулях — только semantic_colors().
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtGui import QColor, QIcon, QPalette
from PyQt6.QtWidgets import QApplication

from logging_setup import logger

_dark_cache: Optional[bool] = None
_semantic_cache: Optional[Dict[str, QColor]] = None
_semantic_hex_cache: Optional[Dict[str, str]] = None
_icon_theme: Optional[str] = None

_ICON_THEME_FALLBACKS: List[str] = [
    "breeze-dark", "breeze",
    "Papirus-Dark", "Papirus",
    "Adwaita", "gnome",
    "hicolor",
]


def _palette() -> Optional[QPalette]:
    app = QApplication.instance()
    return app.palette() if app is not None else None


def invalidate_cache() -> None:
    global _dark_cache, _semantic_cache, _semantic_hex_cache
    _dark_cache = None
    _semantic_cache = None
    _semantic_hex_cache = None
    try:
        from dup_colors import clear_dup_color_cache
        clear_dup_color_cache()
    except Exception:
        pass


def is_dark() -> bool:
    global _dark_cache
    if _dark_cache is not None:
        return _dark_cache
    pal = _palette()
    if pal is None:
        _dark_cache = False
        return False
    bg = pal.color(QPalette.ColorRole.Window)
    luma = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    _dark_cache = luma < 128
    return _dark_cache


def setup_icon_theme() -> str:
    """Устанавливает тему иконок при старте."""
    global _icon_theme
    if _icon_theme is not None:
        return _icon_theme

    current = QIcon.themeName() or ""
    if current and current != "hicolor":
        _icon_theme = current
        logger.info("Icon theme (system): %s", _icon_theme)
        return current

    for candidate in _ICON_THEME_FALLBACKS:
        QIcon.setThemeName("")  # сброс кэша Qt
        QIcon.setThemeName(candidate)
        if not QIcon.fromTheme("document-open").isNull():
            _icon_theme = candidate
            logger.info("Icon theme (fallback): %s", candidate)
            return candidate

    QIcon.setThemeName("hicolor")
    try:
        QIcon.setFallbackThemeName("hicolor")
    except AttributeError:
        pass
    _icon_theme = "hicolor"
    logger.warning("Icon theme: only hicolor available")
    return _icon_theme


def icon_theme_name() -> str:
    return _icon_theme or setup_icon_theme()


def _adjust_value(c: QColor, factor: float) -> QColor:
    h, s, v, a = c.getHsv()
    v = max(0, min(255, int(v * factor)))
    return QColor.fromHsv(h, s, v, a)


def _tinted(base: QColor, hue: int, sat: int, val: int) -> QColor:
    return QColor.fromHsv(hue % 360, max(0, min(255, sat)),
                          max(0, min(255, val)), base.alpha())


def _hexc(c: QColor) -> str:
    return c.name(QColor.NameFormat.HexRgb)


def semantic_colors() -> Dict[str, QColor]:
    global _semantic_cache
    if _semantic_cache is not None:
        return _semantic_cache

    pal = _palette()
    if pal is None:
        _semantic_cache = {
            'working': QColor(0, 140, 0),
            'not_working': QColor(200, 0, 0),
            'unchecked': QColor(120, 120, 0),
            'unsupported': QColor(0, 80, 200),
            'neutral': QColor(80, 80, 80),
            'no_url': QColor(160, 160, 160),
            'orphan_bg': QColor(255, 250, 200),
        }
        return _semantic_cache

    dark = is_dark()
    text = pal.color(QPalette.ColorRole.Text)
    placeholder = pal.color(QPalette.ColorRole.PlaceholderText)
    alternate = pal.color(QPalette.ColorRole.AlternateBase)

    if dark:
        s_green, v_green = 150, 220
        s_red, v_red = 170, 230
        s_yellow, v_yellow = 150, 220
        s_blue, v_blue = 150, 230
        orphan_sat, orphan_val = 130, 90
    else:
        s_green, v_green = 180, 120
        s_red, v_red = 200, 140
        s_yellow, v_yellow = 200, 120
        s_blue, v_blue = 180, 160
        orphan_sat, orphan_val = 90, 245

    orphan = QColor.fromHsv(50, orphan_sat, orphan_val,
                            alternate.alpha())

    _semantic_cache = {
        'working': _tinted(text, 120, s_green, v_green),
        'not_working': _tinted(text, 0, s_red, v_red),
        'unchecked': _tinted(text, 50, s_yellow, v_yellow),
        'unsupported': _tinted(text, 220, s_blue, v_blue),
        'neutral': QColor(text),
        'no_url': QColor(placeholder),
        'orphan_bg': orphan,
    }
    return _semantic_cache


def semantic_hex(name: str) -> str:
    global _semantic_hex_cache
    if _semantic_hex_cache is None:
        _semantic_hex_cache = {k: _hexc(v)
                                for k, v in semantic_colors().items()}
    return _semantic_hex_cache.get(
        name, _hexc(semantic_colors()['neutral']))


def hint_hex() -> str:
    pal = _palette()
    if pal is None:
        return "#888888"
    return _hexc(pal.color(QPalette.ColorRole.PlaceholderText))