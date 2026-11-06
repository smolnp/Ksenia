# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Единый HTTP-клиент с пулом, ретраями и per-thread сессиями.

Заменяет _new_session() в epg.py, sources.py, utils.py.
verify_ssl — из настроек (по умолчанию True).
"""
from __future__ import annotations

import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from logging_setup import logger


_DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36')


def _default_verify_ssl() -> bool:
    try:
        from app_core import ApplicationCore
        core = ApplicationCore._instance
        if core is not None:
            return bool(core.config.get('verify_ssl', True))
    except Exception:
        pass
    return True


class HttpClient:
    """Единый клиент. Сессия — per-thread (requests не потокобезопасен)."""
    _local = threading.local()
    _lock = threading.Lock()

    @classmethod
    def _make_session(cls) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': _DEFAULT_UA,
            'Accept': '*/*',
        })
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(['GET', 'HEAD']),
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=20,
            pool_maxsize=50,
        )
        s.mount('http://', adapter)
        s.mount('https://', adapter)
        return s

    @classmethod
    def session(cls) -> requests.Session:
        s = getattr(cls._local, 'session', None)
        if s is None:
            s = cls._make_session()
            cls._local.session = s
        return s

    @classmethod
    def get(cls, url: str, *, timeout: int = 10,
            verify: Optional[bool] = None,
            stream: bool = False,
            headers: Optional[dict] = None,
            allow_redirects: bool = True,
            **kwargs):
        if verify is None:
            verify = _default_verify_ssl()
        s = cls.session()
        return s.get(
            url, timeout=timeout, verify=verify,
            stream=stream, headers=headers,
            allow_redirects=allow_redirects, **kwargs)

    @classmethod
    def post(cls, url: str, *, timeout: int = 10,
             verify: Optional[bool] = None,
             headers: Optional[dict] = None,
             **kwargs):
        if verify is None:
            verify = _default_verify_ssl()
        s = cls.session()
        return s.post(
            url, timeout=timeout, verify=verify,
            headers=headers, **kwargs)

    @classmethod
    def close_thread_session(cls):
        s = getattr(cls._local, 'session', None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
            cls._local.session = None