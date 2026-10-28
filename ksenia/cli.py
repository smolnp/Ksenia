# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""CLI/headless режим. Без QApplication — только QCoreApplication."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from contextlib import suppress
from typing import List, Optional

from PyQt6.QtCore import QCoreApplication, QTranslator

from constants import (APP_VERSION, APP_ORG, APP_NAME,
    STABLE_CHECK_MAX_WORKERS, STABLE_CHECK_TIMEOUT_SEC,
    STABLE_LATENCY_THRESHOLD_MS, MIN_SQLITE_VERSION)
from logging_setup import logger
from models import ChannelData
from sources import LinkSource
from utils import URLUtils


def _check_sqlite_version() -> None:
    v = sqlite3.sqlite_version_info
    if v < MIN_SQLITE_VERSION:
        raise RuntimeError(
            f"SQLite {v} слишком старый. Требуется ≥ "
            f"{'.'.join(map(str, MIN_SQLITE_VERSION))}")


def parse_cli_args(argv: Optional[List[str]] = None
                   ) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ksenia",
        description=f"Ksenia M3U Editor {APP_VERSION} — CLI/headless")
    p.add_argument("--headless", action="store_true",
                   help="Запуск без GUI")
    p.add_argument("--sources-file", default=None,
                   help="JSON-файл источников")
    p.add_argument("--export", default=None,
                   help="Путь выходного M3U")
    p.add_argument("--export-stable", action="store_true",
                   help="Stable-экспорт с перепроверкой URL")
    p.add_argument("--check-workers", type=int,
                   default=STABLE_CHECK_MAX_WORKERS,
                   help="Число воркеров проверки")
    p.add_argument("--timeout", type=int,
                   default=STABLE_CHECK_TIMEOUT_SEC,
                   help="Таймаут проверки, сек")
    p.add_argument("--epg-url", default=None,
                   help="URL EPG (XMLTV/GZ) для применения метаданных. "
                        "Если не указан — EPG не используется.")
    p.add_argument("--version", action="version",
                   version=f"Ksenia {APP_VERSION}")
    return p.parse_args(argv)


def _install_translation_headless(app: QCoreApplication,
                                  lang: str) -> None:
    try:
        from paths import Paths
        qm = os.path.join(
            Paths.get_app_dir(), "locale", f"ksenia_{lang}.qm")
        if not os.path.isfile(qm):
            return
        tr_ = QTranslator()
        if not tr_.load(qm):
            return
        app._ksenia_translator = tr_
        app.installTranslator(tr_)
    except Exception:
        logger.exception("install translation (headless)")


def run_cli(args: argparse.Namespace) -> int:
    _check_sqlite_version()
    _qcore = (QCoreApplication.instance()
              or QCoreApplication(sys.argv))
    _qcore.setOrganizationName(APP_ORG)
    _qcore.setApplicationName(APP_NAME)
    if getattr(args, "lang", None):
        _install_translation_headless(_qcore, args.lang)

    from app_core import ApplicationCore
    core = ApplicationCore.instance()

    if not args.sources_file:
        print("Ошибка: --sources-file обязателен в headless",
              file=sys.stderr)
        return 2
    if not args.export:
        print("Ошибка: --export обязателен в headless",
              file=sys.stderr)
        return 2
    if not os.path.exists(args.sources_file):
        print(f"Ошибка: файл не найден: {args.sources_file}",
              file=sys.stderr)
        return 2

    try:
        with open(args.sources_file, 'r', encoding='utf-8') as f:
            sources_data = json.load(f)
    except Exception as e:
        print(f"Ошибка чтения источников: {e}", file=sys.stderr)
        return 2
    if not isinstance(sources_data, list):
        print("Ошибка: ожидался JSON-массив источников",
              file=sys.stderr)
        return 2

    sources: List[LinkSource] = []
    skipped = 0
    for d in sources_data:
        if isinstance(d, dict):
            src = LinkSource.from_dict(d)
            if src.name and src.enabled:
                sources.append(src)
            else:
                skipped += 1
    if skipped:
        print(f"[i] Пропущено источников: {skipped}",
              file=sys.stderr)
    if not sources:
        print("Ошибка: нет включённых источников",
              file=sys.stderr)
        return 2

    sources.sort(key=lambda s: s.priority)

    if args.epg_url:
        print(f"[+] Загрузка EPG: {args.epg_url}")
        try:
            cnt, errors = core.epg_db.load_from_urls(
                [args.epg_url], timeout=20)
            if errors:
                print(f"[i] EPG errors: {errors[:3]}",
                      file=sys.stderr)
            print(f"    Загружено программ: {cnt}")
        except Exception as e:
            print(f"[!] EPG load failed: {e}", file=sys.stderr)

    settings = core.get_replacement_settings()

    all_channels: List[ChannelData] = []
    for src in sources:
        print(f"[+] Загрузка: {src.name} ({src.path})")
        chs = core.link_source_manager.load_links_from_source(
            src, use_cache=False, config=core.config,
            update_meta=False,
            blacklist_manager=core.blacklist_manager,
            domain_blacklist_manager=
                core.domain_blacklist_manager,
            domain_user_agent_manager=
                core.domain_user_agent_manager,
            settings=settings)
        print(f"    Получено: {len(chs)} каналов")
        for ch in chs:
            ch.link.link_source = src.name
        all_channels.extend(chs)

    if core.blacklist_manager.get_all():
        before = len(all_channels)
        all_channels, removed = \
            core.apply_blacklist_to_channels(all_channels)
        if removed:
            print(f"[+] ЧС каналов: удалено {removed} "
                  f"(было {before})")

    if core.domain_blacklist_manager.get_all():
        before = len(all_channels)
        all_channels, cleaned = \
            core.apply_domain_blacklist_clean(all_channels)
        if cleaned:
            print(f"[+] ЧС домен/IP: очищено ссылок {cleaned} "
                  f"(осталось {len(all_channels)})")

    if core.domain_user_agent_manager.get_all_rules():
        modified = core.apply_domain_user_agent(all_channels)
        if modified:
            print(f"[+] UA-правила применены к {modified}")

    if args.epg_url and core.epg_db.has_channel_info:
        applied = 0
        overwrite = bool(core.config.get(
            'epg_overwrite_metadata', True))
        rename = bool(core.config.get(
            'epg_overwrite_channel_name', False))
        for ch in all_channels:
            try:
                if core.epg_db.apply_metadata_to_channel(
                        ch, settings,
                        overwrite_metadata=overwrite,
                        rename_channel=rename):
                    applied += 1
            except Exception:
                logger.exception("apply_metadata_to_channel")
        if applied:
            print(f"[+] EPG-метаданные применены к {applied}")

    if args.export_stable:
        ssm = core.stable_state_manager
        written = 0
        skipped_n = 0
        total = len(all_channels)
        print(f"[+] Stable-экспорт: {total} каналов, "
              f"timeout={args.timeout}s, "
              f"workers={args.check_workers}")

        def find_url(ch: ChannelData) -> Optional[str]:
            cands: List[str] = []
            ref = ssm.get(ch.stable_key(settings))
            if ref:
                cands.append(ref)
            if ch.link.url and ch.link.url not in cands:
                cands.append(ch.link.url)
            for u in ch.link.alternative_urls:
                if u and u not in cands:
                    cands.append(u)
            for u in cands:
                if not u or not u.strip():
                    continue
                if (settings.is_blacklisted(u)
                        or settings.is_filtered_domain(u)):
                    continue
                ok, rt, _, _ = URLUtils.check_url(
                    u, args.timeout, verify_ssl=False)
                if ok is True and (rt is None
                                   or rt * 1000
                                   <= STABLE_LATENCY_THRESHOLD_MS):
                    return u
            return None

        with open(args.export, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n\n")
            for i, ch in enumerate(all_channels):
                if (i + 1) % 25 == 0 or i == total - 1:
                    print(f"    Проверка: {i+1}/{total}")
                url = find_url(ch)
                if not url:
                    skipped_n += 1
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
        print(f"[✓] Stable: записано {written}, "
              f"пропущено {skipped_n}")
    else:
        with open(args.export, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n\n")
            for ch in all_channels:
                f.write(ch.link.extinf + '\n')
                for line in ch.link.extvlcopt_lines:
                    f.write(line + '\n')
                f.write((ch.link.url or '') + '\n')
        print(f"[✓] Экспорт: {len(all_channels)} каналов → "
              f"{args.export}")

    return 0


def _install_signal_handlers(app) -> None:
    def handler(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(ValueError, OSError):
            signal.signal(sig, handler)