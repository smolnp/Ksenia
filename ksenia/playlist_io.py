# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Сериализация плейлиста: рендер, сохранение, экспорт, Stable."""
from __future__ import annotations

import csv
import json
import os
import shutil
from contextlib import suppress
from typing import Callable, List, Optional, Tuple

from constants import (
    SAVE_ENCODINGS, STABLE_CHECK_TIMEOUT_SEC,
    STABLE_LATENCY_THRESHOLD_MS)
from logging_setup import logger
from models import ChannelData
from parsers import PlaylistHeaderManager
from paths import error_box
from utils import URLUtils


def resolve_save_encoding(
        header_manager: PlaylistHeaderManager,
        config,
        explicit_save_as: bool) -> str:
    original = header_manager.original_encoding or 'utf-8'
    if not explicit_save_as:
        return original
    cfg_enc = (config.get(
        'default_save_encoding', 'auto') or 'auto').strip()
    if cfg_enc not in SAVE_ENCODINGS or cfg_enc == 'auto':
        return original
    return cfg_enc


def render_playlist_text(
        channels: List[ChannelData],
        header_manager: PlaylistHeaderManager,
        save_extvlcopt: bool) -> str:
    parts: List[str] = []
    ht = header_manager.get_header_text()
    if ht:
        parts.append(ht.rstrip('\n'))
        parts.append('')
    for ch in channels:
        parts.append(ch.link.extinf)
        if save_extvlcopt:
            parts.extend(ch.link.extvlcopt_lines)
        else:
            if ch.link.user_agent:
                parts.append(
                    f'#EXTVLCOPT:http-user-agent='
                    f'"{ch._escape(ch.link.user_agent)}"')
            for k, v in ch.link.extra_headers.items():
                if k.lower() == 'user-agent':
                    continue
                if k.lower() == 'referer':
                    parts.append(
                        f'#EXTVLCOPT:http-referrer='
                        f'"{ch._escape(v)}"')
                else:
                    parts.append(
                        f'#EXTVLCOPT:http-header='
                        f'"{k}: {ch._escape(v)}"')
        parts.append(ch.link.url or '')
    return '\n'.join(parts)


def save_playlist_file(
        filepath: str,
        channels: List[ChannelData],
        header_manager: PlaylistHeaderManager,
        config,
        explicit_save_as: bool,
        parent_widget,
        ask_encoding_fn: Callable[[str, str], bool]) -> bool:
    try:
        if (os.path.exists(filepath)
                and config.get('auto_backup_before_save', True)):
            with suppress(OSError):
                shutil.copy2(filepath, filepath + '.bak')
        enc = resolve_save_encoding(
            header_manager, config, explicit_save_as)
        if enc not in ('utf-8', 'utf-8-sig'):
            if ask_encoding_fn(
                    "Кодировка",
                    f"Файл будет сохранён в кодировке {enc}.\n"
                    f"Сохранить в UTF-8 (рекомендуется)?"):
                enc = 'utf-8'
        text = render_playlist_text(
            channels, header_manager,
            config.get('save_extvlcopt', True))
        with open(filepath, 'w', encoding=enc) as f:
            f.write(text)
        return True
    except Exception as e:
        logger.exception("save_playlist_file")
        if parent_widget is not None:
            error_box(parent_widget,
                      f"Не удалось сохранить:\n{e}")
        return False


def export_channels_csv(channels: List[ChannelData],
                        filepath: str) -> None:
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'name', 'original_name', 'group', 'tvg_id',
            'tvg_name', 'tvg_logo', 'url',
            'user_agent', 'region', 'quality',
            'timeshift', 'catchup', 'catchup_source',
            'catchup_days', 'tvg_shift', 'tvg_chno',
            'audio_track', 'alternative_urls',
            'link_source', 'url_status', 'status_code',
            'epg_source', 'preferred_audio_lang',
            'preferred_video_lang',
            'resolution', 'ttfb_ms'])
        for ch in channels:
            writer.writerow([
                ch.meta.name, ch.meta.original_name,
                ch.meta.group, ch.meta.tvg_id,
                ch.meta.tvg_name, ch.meta.tvg_logo,
                ch.link.url, ch.link.user_agent,
                ch.meta.region, ch.meta.quality,
                ch.meta.timeshift, ch.meta.catchup,
                ch.meta.catchup_source,
                ch.meta.catchup_days, ch.meta.tvg_shift,
                ch.meta.tvg_chno, ch.meta.audio_track,
                '|'.join(ch.link.alternative_urls),
                ch.link.link_source,
                ('working'
                 if ch.status.url_status is True
                 else 'broken'
                 if ch.status.url_status is False
                 else 'unknown'),
                (ch.status.status_code
                 if ch.status.status_code is not None
                 else ''),
                ch.meta.epg_source,
                ch.meta.preferred_audio_lang,
                ch.meta.preferred_video_lang,
                ch.status.resolution or '',
                (ch.status.ttfb_ms
                 if ch.status.ttfb_ms is not None
                 else ''),
            ])


def export_channels_json(channels: List[ChannelData],
                         filepath: str) -> None:
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(
            [ch.to_dict() for ch in channels],
            f, ensure_ascii=False, indent=2)


def find_stable_url(ch: ChannelData, ssm, settings
                    ) -> Optional[str]:
    candidates: List[str] = []
    ref = ssm.get(ch.stable_key(settings))
    if ref:
        candidates.append(ref)
    if ch.link.url and ch.link.url not in candidates:
        candidates.append(ch.link.url)
    for u in ch.link.alternative_urls:
        if u and u not in candidates:
            candidates.append(u)

    for u in candidates:
        if not u or not u.strip():
            continue
        if (settings.is_blacklisted(u)
                or settings.is_filtered_domain(u)):
            continue
        ok, rt, _, _, _, _ = URLUtils.check_url(
            u, STABLE_CHECK_TIMEOUT_SEC, verify_ssl=None)
        if ok is True and (rt is None
                           or rt * 1000
                           <= STABLE_LATENCY_THRESHOLD_MS):
            return u
    return None


def export_stable_to_file(
        filepath: str,
        channels: List[ChannelData],
        header_manager: PlaylistHeaderManager,
        ssm,
        settings,
        progress_cb: Optional[Callable[
            [int, int, str], None]] = None) -> Tuple[int, int]:
    written = 0
    skipped = 0
    total = len(channels)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(header_manager.get_header_text())
        for i, ch in enumerate(channels):
            if progress_cb:
                progress_cb(i, total,
                             f"Стабильный: {i + 1}/{total}")
            url = find_stable_url(ch, ssm, settings)
            if not url:
                skipped += 1
                continue
            key = ch.stable_key(settings)
            if not ssm.get(key):
                ssm.set(key, url)
            tmp = ch.copy()
            tmp.link.url = url
            tmp.link.has_url = True
            tmp.update_extinf()
            f.write(tmp.link.extinf + '\n')
            for line in tmp.link.extvlcopt_lines:
                f.write(line + '\n')
            f.write(url + '\n')
            written += 1
    ssm.save()
    return written, skipped