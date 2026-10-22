# -*- coding: utf-8 -*-
"""ColumnSpec — описание колонок таблицы каналов."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from models import ChannelData


COLUMN_CORE = "core"
COLUMN_OPTIONAL = "optional"
COLUMN_HIDDEN_BY_DEFAULT = "hidden"


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    title: str
    weight: str
    get_value: Callable[[ChannelData], str]
    sort_key: Callable[[ChannelData], Any]
    editable: bool = True
    alignment: str = "left"
    min_width: int = 60
    stretch: bool = False
    is_icon: bool = False


def _index_value(ch: ChannelData) -> str:
    if ch.original_index >= 0:
        return str(ch.original_index + 1)
    return "—"


def _index_sort(ch: ChannelData) -> Any:
    return (ch.original_index if ch.original_index >= 0
            else 10**12)


ALL_COLUMNS: List[ColumnSpec] = [
    ColumnSpec(
        key="index",
        title="№",
        weight=COLUMN_CORE,
        get_value=_index_value,
        sort_key=_index_sort,
        editable=False,
        alignment="center",
        min_width=50,
    ),
    ColumnSpec(
        key="name",
        title="Название",
        weight=COLUMN_CORE,
        get_value=lambda ch: ch.meta.name or "",
        sort_key=lambda ch: (ch.meta.name or "").lower(),
        editable=True,
        alignment="left",
        min_width=200,
        stretch=True,
    ),
    ColumnSpec(
        key="group",
        title="Группа",
        weight=COLUMN_CORE,
        get_value=lambda ch: ch.meta.group or "",
        sort_key=lambda ch: (ch.meta.group or "").lower(),
        editable=True,
        alignment="left",
        min_width=120,
    ),
    ColumnSpec(
        key="tvg_id",
        title="TVG-ID",
        weight=COLUMN_OPTIONAL,
        get_value=lambda ch: ch.meta.tvg_id or "",
        sort_key=lambda ch: (ch.meta.tvg_id or "").lower(),
        editable=True,
        alignment="left",
        min_width=100,
    ),
    ColumnSpec(
        key="logo",
        title="Логотип",
        weight=COLUMN_OPTIONAL,
        get_value=lambda ch: ch.meta.tvg_logo or "",
        sort_key=lambda ch: (ch.meta.tvg_logo or "").lower(),
        editable=True,
        alignment="left",
        min_width=100,
    ),
    ColumnSpec(
        key="catchup",
        title="Catchup",
        weight=COLUMN_HIDDEN_BY_DEFAULT,
        get_value=lambda ch: ch.meta.catchup or "",
        sort_key=lambda ch: (ch.meta.catchup or "").lower(),
        editable=True,
        alignment="left",
        min_width=80,
    ),
    ColumnSpec(
        key="url",
        title="URL",
        weight=COLUMN_CORE,
        get_value=lambda ch: ch.link.url or "",
        sort_key=lambda ch: (ch.link.url or "").lower(),
        editable=True,
        alignment="left",
        min_width=200,
        stretch=True,
    ),
]


def columns_for_channels(
        channels: List[ChannelData],
        user_visible_keys: Optional[List[str]] = None,
        hidden_keys: Optional[List[str]] = None
) -> List[ColumnSpec]:
    user_keys = set(user_visible_keys or [])
    hidden = set(hidden_keys or [])
    explicit_optional = user_visible_keys is not None

    result: List[ColumnSpec] = []
    for spec in ALL_COLUMNS:
        if spec.weight == COLUMN_CORE:
            result.append(spec)
            continue

        if spec.weight == COLUMN_OPTIONAL:
            if spec.key in hidden:
                continue
            # Принудительно добавляем TVG-ID и Логотип, даже если они пустые
            # или не переданы в user_visible_keys
            if spec.key in ("tvg_id", "logo"):
                result.append(spec)
            # Остальные опциональные колонки добавляем только при наличии данных
            # или если они явно запрошены пользователем
            elif explicit_optional and spec.key not in user_keys:
                continue
            elif any(spec.get_value(ch) for ch in channels):
                result.append(spec)
            continue

        if spec.weight == COLUMN_HIDDEN_BY_DEFAULT:
            if spec.key in hidden:
                continue
            if spec.key in user_keys and any(
                    spec.get_value(ch) for ch in channels):
                result.append(spec)
                
    return result