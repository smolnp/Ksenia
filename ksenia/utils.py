# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""URLUtils, _StopToken, валидация, decode."""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from typing import Any, Iterable, Optional, Tuple

import requests
import urllib3
from urllib.parse import urlparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from constants import MAX_URL_LENGTH, MAX_M3U_LINES, StatusText
from logging_setup import logger

from name_normalizer import (  # noqa: F401
    ChannelNameNormalizer,
    NormMode,
    canonical_key,
    is_stub_tvg_id,
    normalize,
    normalize_with_settings,
    similarity,
    apply_case_mode,
    dominant_script,
)


class _StopToken:
    __slots__ = ('_event',)

    def __init__(self):
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


def cancelled(stop_token: Optional['_StopToken']) -> bool:
    return stop_token is not None and stop_token.is_set()


# Порядок важен: utf-8-sig должен идти ДО utf-8, иначе BOM
# попадёт в первую строку и сломает распознавание #EXTM3U.
_M3U_ENCODINGS = ('utf-8-sig', 'utf-8', 'cp1251')


def decode_m3u_bytes(raw: bytes) -> Tuple[str, str]:
    for enc in _M3U_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace'), 'utf-8'


class InputValidationError(ValueError):
    pass


def validate_json_size(path: str,
                       max_bytes: int = 256 * 1024 * 1024
                       ) -> None:
    import os
    if not os.path.exists(path):
        raise InputValidationError(f"File not found: {path}")
    size = os.path.getsize(path)
    if size > max_bytes:
        raise InputValidationError(
            f"JSON too large: {size} bytes > {max_bytes}")


def validate_m3u_lines(content: str,
                       max_lines: int = MAX_M3U_LINES) -> None:
    n = content.count('\n') + 1
    if n > max_lines:
        raise InputValidationError(
            f"M3U too large: {n} lines > {max_lines}")


def sanitize_user_agent(ua: str, max_length: int = 1024) -> str:
    if not ua:
        return ""
    ua = re.sub(r'[\r\n\t\0]+', ' ', ua).strip()
    if len(ua) > max_length:
        ua = ua[:max_length]
    return ua


class URLUtils:
    @staticmethod
    def extract_host(url: str) -> str:
        if not url or not url.strip():
            return ""
        try:
            p = urlparse(url)
            host = (p.hostname or "").lower()
        except Exception:
            return ""
        if '\\' in host:
            return ""
        return host

    @staticmethod
    def normalize_host(raw: str) -> str:
        if not raw:
            return ""
        s = raw.strip().lower()
        if '\\' in s:
            s = s.split('\\', 1)[0]
        if '://' in s:
            s = s.split('://', 1)[1]
        for sep in ('/', '?', '#'):
            if sep in s:
                s = s.split(sep, 1)[0]
        if '@' in s:
            s = s.rsplit('@', 1)[1]
        if s.startswith('['):
            end = s.find(']')
            if end > 0:
                s = s[1:end]
        elif ':' in s and s.count(':') == 1:
            s = s.split(':', 1)[0]
        return s.strip().strip('.')

    @staticmethod
    def is_ip_address(s: str) -> bool:
        if not s:
            return False
        try:
            ipaddress.ip_address(s)
            return True
        except ValueError:
            return False

    @staticmethod
    def ip_matches(url: str, target_ip: str) -> bool:
        if not url or not target_ip:
            return False
        host = URLUtils.extract_host(url)
        if not host:
            return False
        try:
            return (ipaddress.ip_address(host)
                    == ipaddress.ip_address(target_ip))
        except ValueError:
            return False

    @staticmethod
    def host_matches(url: str, target_host: str,
                     include_subdomains: bool = True) -> bool:
        if not target_host:
            return False
        target_host = target_host.lower()
        if URLUtils.is_ip_address(target_host):
            return URLUtils.ip_matches(url, target_host)
        host = URLUtils.extract_host(url)
        if not host:
            return False
        if host == target_host:
            return True
        if (include_subdomains
                and host.endswith("." + target_host)):
            return True
        return False

    @staticmethod
    def host_in_domains(host: str, domains: Iterable[str]) -> bool:
        if not host:
            return False
        host = host.lower()
        for d in domains:
            dn = (d or '').strip().lower().strip('.')
            if not dn:
                continue
            if host == dn or host.endswith('.' + dn):
                return True
        return False

    @staticmethod
    def classify_status(url: str,
                        status_code: Optional[int] = None,
                        error: str = ""
                        ) -> Tuple[str, Optional[int]]:
        if not url or not url.strip():
            return StatusText.NO_URL, None
        if error:
            e = error.lower()
            if "empty response" in e or "пустой ответ" in e:
                return StatusText.EMPTY_RESPONSE, status_code
            if "timeout" in e or "таймаут" in e:
                return StatusText.TIMEOUT, status_code
            if "dns" in e:
                return StatusText.DNS_FAIL, status_code
            if "connection" in e or "соединени" in e:
                return StatusText.CONN_ERROR, status_code
            return "🆘 " + error[:40], status_code
        if status_code is None:
            return StatusText.UNCHECKED, None
        if 200 <= status_code < 300:
            return StatusText.WORKING, status_code
        if status_code == 403:
            return StatusText.GEOBLOCK, status_code
        if status_code == 404:
            return StatusText.NOT_FOUND, status_code
        if status_code in (301, 302, 307, 308):
            return f"↪ Редирект ({status_code})", status_code
        return f"⚠️ HTTP {status_code}", status_code

    @staticmethod
    def validate_url(url: str) -> Optional[str]:
        try:
            p = urlparse(url)
        except Exception as e:
            return f"Ошибка: {str(e)[:50]}"
        if not p.scheme or not p.netloc:
            return "Некорректный URL"
        if len(url) > MAX_URL_LENGTH:
            return "URL слишком длинный"
        if p.scheme not in ('http', 'https'):
            return f"Неподдерживаемый протокол: {p.scheme}"
        try:
            port = p.port
        except ValueError:
            return "Некорректный порт в URL"
        low_path = (p.path or '').lower()
        if low_path.startswith('/udp/') or '/udp/' in low_path:
            return "UDP через HTTP-прокси (не поддерживается)"
        if low_path.startswith('/tcp/') or '/tcp/' in low_path:
            return "TCP через HTTP-прокси (не поддерживается)"
        if low_path.endswith('.php'):
            q = (p.query or '').lower()
            if 'id=' in q and len(q) > 32:
                return "Прокси-скрипт (не поток)"
        if not low_path or low_path == '/':
            if port and port not in (80, 443, 8080, 8000, 8888):
                return "URL без пути на нестандартном порту"
        return None

    @staticmethod
    def _resolve_dns(hostname: Optional[str]) -> bool:
        if not hostname:
            return True
        try:
            socket.getaddrinfo(hostname, None)
            return True
        except socket.gaierror:
            return False

    @staticmethod
    def _check_url_once(url: str, timeout: int,
                        verify_ssl: bool,
                        user_agent: str = "",
                        referrer: str = "",
                        extra_headers=None,
                        stop_token: Optional['_StopToken'] = None
                        ) -> Tuple[Optional[bool], Optional[float],
                                    str, Optional[int]]:
        if cancelled(stop_token):
            return None, None, "Отменено", None
        parsed = urlparse(url)
        if not URLUtils._resolve_dns(parsed.hostname):
            return False, 0.0, "DNS резолвинг не удался", None
        if cancelled(stop_token):
            return None, None, "Отменено", None

        start = time.time()
        session = requests.Session()
        headers = {
            'User-Agent': user_agent or
                ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                 'AppleWebKit/537.36'),
            'Accept': '*/*',
        }
        if referrer:
            headers['Referer'] = referrer
        if extra_headers:
            for k, v in extra_headers.items():
                if k.lower() not in ('user-agent', 'referer'):
                    headers[k] = v
        session.headers.update(headers)
        try:
            with session.get(url, timeout=timeout,
                             allow_redirects=True,
                             verify=verify_ssl,
                             stream=True) as r:
                rt = time.time() - start
                code = r.status_code
                if code in (200, 206, 301, 302, 304, 307, 308):
                    try:
                        chunk = next(
                            r.iter_content(chunk_size=1024),
                            None)
                    except Exception:
                        chunk = None
                    if not chunk:
                        return False, rt, "Пустой ответ", code
                    return True, rt, f"HTTP {code}", code
                return False, rt, f"HTTP {code}", code
        except requests.Timeout:
            return False, timeout, "Превышен таймаут", None
        except requests.ConnectionError:
            return False, 0.0, "Ошибка соединения", None
        except Exception as e:
            return False, 0.0, f"Ошибка: {str(e)[:60]}", None
        finally:
            try:
                session.close()
            except Exception:
                pass

    @staticmethod
    def check_url(url: str, timeout: int = 3,
                  verify_ssl: bool = False,
                  stop_token: Optional['_StopToken'] = None,
                  user_agent: str = "", referrer: str = "",
                  extra_headers=None,
                  retries: int = 0,
                  retry_delay: float = 0.0
                  ) -> Tuple[Optional[bool], Optional[float],
                              str, Optional[int]]:
        if not url or not url.strip():
            return False, None, "Пустой URL", None
        if cancelled(stop_token):
            return None, None, "Отменено", None
        retries = max(0, int(retries))
        retry_delay = max(0.0, float(retry_delay))
        last = URLUtils._check_url_once(
            url, timeout, verify_ssl, user_agent=user_agent,
            referrer=referrer, extra_headers=extra_headers,
            stop_token=stop_token)
        ok, _rt, msg, code = last
        if ok is True or code in (403, 404) or retries == 0:
            return last
        if msg and 'DNS' in msg:
            return last
        for _ in range(retries):
            if cancelled(stop_token):
                return None, None, "Отменено", None
            if retry_delay > 0:
                time.sleep(retry_delay)
            result = URLUtils._check_url_once(
                url, timeout, verify_ssl,
                user_agent=user_agent, referrer=referrer,
                extra_headers=extra_headers,
                stop_token=stop_token)
            ok, _rt, msg, code = result
            if ok is True or code in (403, 404):
                return result
            if msg and 'DNS' in msg:
                return result
            last = result
        return last