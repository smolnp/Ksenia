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

from constants import (
    MAX_URL_LENGTH, MAX_M3U_LINES, StatusText,
    AUTO_REPAIR_MAX_ATTEMPTS, AUTO_REPAIR_SCHEME_TIMEOUT,
)
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
            # Content-Type audit из _check_url_once
            if "html" in e and "instead" in e:
                return StatusText.HTML_INSTEAD_OF_STREAM, status_code
            if "json" in e and "instead" in e:
                return StatusText.JSON_INSTEAD_OF_STREAM, status_code
            if "non-video" in e or "не видео" in e:
                return StatusText.NON_VIDEO_CONTENT, status_code
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

    # ── Content-Type audit ──

    @staticmethod
    def _classify_content_type(ct: str) -> Optional[str]:
        """Возвращает статус ошибки, если Content-Type не похож
        на видео/аудио-поток. Иначе None.

        Валидные для IPTV типы:
          - video/*, audio/*
          - application/vnd.apple.mpegurl (HLS)
          - application/x-mpegurl (HLS)
          - application/dash+xml (DASH)
          - application/octet-stream (часто .ts/.mp4 без MIME)
        """
        if not ct:
            return None
        low = ct.lower().split(';', 1)[0].strip()
        if not low:
            return None
        if low.startswith(('video/', 'audio/')):
            return None
        if low in (
                'application/vnd.apple.mpegurl',
                'application/x-mpegurl',
                'application/dash+xml',
                'application/octet-stream',
                'application/mpegurl',
                'application/x-mpegURL'.lower(),
        ):
            return None
        if 'text/html' in low or 'application/xhtml' in low:
            return StatusText.HTML_INSTEAD_OF_STREAM
        if 'application/json' in low:
            return StatusText.JSON_INSTEAD_OF_STREAM
        # image/*, text/plain, application/xml и т.п.
        return StatusText.NON_VIDEO_CONTENT

    # ── Core single check ──

    @staticmethod
    def _check_url_once(
            url: str, timeout: int, verify_ssl: bool,
            user_agent: str = "",
            referrer: str = "",
            extra_headers=None,
            stop_token: Optional['_StopToken'] = None,
    ) -> Tuple[Optional[bool], Optional[float], str,
               Optional[int], Optional[float], Optional[str]]:
        """Одна попытка проверки URL.

        Возвращает кортеж:
          (ok, response_time_sec, message, status_code,
           ttfb_ms, repaired_url)

        repaired_url всегда None здесь — Auto-Repair реализован
        отдельным методом try_repair() на уровне check_url().
        """
        if cancelled(stop_token):
            return None, None, "Отменено", None, None, None
        parsed = urlparse(url)
        if not URLUtils._resolve_dns(parsed.hostname):
            return (False, 0.0, "DNS резолвинг не удался",
                    None, None, None)
        if cancelled(stop_token):
            return None, None, "Отменено", None, None, None

        start = time.time()
        ttfb: Optional[float] = None
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
                # TTFB — время до получения заголовков
                # (для стримов это то же, что до первого байта
                # заголовков ответа; для HEAD — приблизительно).
                ttfb = (time.time() - start) * 1000.0
                rt = time.time() - start
                code = r.status_code
                if code in (200, 206, 301, 302, 304, 307, 308):
                    # Content-Type audit
                    ct = r.headers.get('Content-Type', '')
                    ct_err = URLUtils._classify_content_type(ct)
                    if ct_err:
                        return (False, rt,
                                f"HTTP {code} ({ct[:60]})",
                                code, ttfb, None)
                    try:
                        chunk = next(
                            r.iter_content(chunk_size=1024), None)
                    except Exception:
                        chunk = None
                    if not chunk:
                        return (False, rt, "Пустой ответ",
                                code, ttfb, None)
                    return True, rt, f"HTTP {code}", code, ttfb, None
                return False, rt, f"HTTP {code}", code, ttfb, None
        except requests.Timeout:
            return False, timeout, "Превышен таймаут", None, None, None
        except requests.ConnectionError:
            return (False, 0.0, "Ошибка соединения",
                    None, None, None)
        except Exception as e:
            return (False, 0.0, f"Ошибка: {str(e)[:60]}",
                    None, None, None)
        finally:
            try:
                session.close()
            except Exception:
                pass

    # ── Auto-Repair ──

    @staticmethod
    def _try_scheme_swap(url: str) -> Optional[str]:
        """Меняет http↔https и проверяет с коротким таймаутом."""
        if url.startswith("http://"):
            alt = "https://" + url[7:]
        elif url.startswith("https://"):
            alt = "http://" + url[8:]
        else:
            return None
        try:
            ok, _rt, _msg, _code, _ttfb, _ = \
                URLUtils._check_url_once(
                    alt, AUTO_REPAIR_SCHEME_TIMEOUT,
                    verify_ssl=False, stop_token=None)
            if ok is True:
                logger.info(
                    "Auto-Repair scheme: %s -> %s",
                    url[:80], alt[:80])
                return alt
        except Exception:
            logger.debug("Auto-Repair scheme swap failed",
                         exc_info=True)
        return None

    @staticmethod
    def _try_follow_redirects(url: str, timeout: int
                              ) -> Optional[str]:
        """GET с follow, но без чтения тела. Возвращает
        финальный URL, если он отличается от исходного."""
        try:
            with requests.Session() as s:
                with s.get(url, timeout=timeout, stream=True,
                           allow_redirects=True,
                           verify=False) as r:
                    if r.history and r.url != url:
                        logger.info(
                            "Auto-Repair redirect: %s -> %s",
                            url[:80], r.url[:80])
                        return r.url
        except Exception:
            logger.debug("Auto-Repair redirect probe failed",
                         exc_info=True)
        return None

    @staticmethod
    def try_repair(url: str, timeout: int = 3,
                   max_attempts: int = AUTO_REPAIR_MAX_ATTEMPTS
                   ) -> Optional[str]:
        """Пытается починить ссылку: схема http↔https +
        следование редиректам. Возвращает рабочий URL или None.

        Не пишет в лог шумно — debug для неудач, info для успеха.
        Не бросает исключений.
        """
        if not url or not url.strip():
            return None
        current = url.strip()
        seen = {current}
        for _ in range(max(1, int(max_attempts))):
            # 1. Смена схемы
            new_url = URLUtils._try_scheme_swap(current)
            if not new_url:
                # 2. Редиректы
                new_url = URLUtils._try_follow_redirects(
                    current, timeout)
            if not new_url or new_url in seen:
                return None
            # Финальная верификация: новый URL должен
            # действительно открываться.
            try:
                ok, _rt, _msg, _code, _ttfb, _ = \
                    URLUtils._check_url_once(
                        new_url, timeout,
                        verify_ssl=False, stop_token=None)
                if ok is True:
                    return new_url
            except Exception:
                logger.debug(
                    "Auto-Repair verify failed", exc_info=True)
            seen.add(new_url)
            current = new_url
        return None

    # ── Public check_url ──

    @staticmethod
    def check_url(url: str, timeout: int = 3,
                  verify_ssl: bool = False,
                  stop_token: Optional['_StopToken'] = None,
                  user_agent: str = "", referrer: str = "",
                  extra_headers=None,
                  retries: int = 0,
                  retry_delay: float = 0.0,
                  try_repair: bool = False,
                  ) -> Tuple[Optional[bool], Optional[float], str,
                              Optional[int], Optional[float],
                              Optional[str]]:
        """Проверяет URL. Возвращает 6-tuple:

          (ok, response_time_sec, message, status_code,
           ttfb_ms, repaired_url)

        Параметр try_repair=True включает Auto-Repair при первой
        неудаче. Auto-Repair — первый шаг, до обращения к кэшу
        живых URL (вызывающий код).
        """
        if not url or not url.strip():
            return False, None, "Пустой URL", None, None, None
        if cancelled(stop_token):
            return None, None, "Отменено", None, None, None
        retries = max(0, int(retries))
        retry_delay = max(0.0, float(retry_delay))
        last = URLUtils._check_url_once(
            url, timeout, verify_ssl, user_agent=user_agent,
            referrer=referrer, extra_headers=extra_headers,
            stop_token=stop_token)
        ok, _rt, msg, code, _ttfb, _ = last
        if ok is True or code in (403, 404) or retries == 0:
            return last
        if msg and 'DNS' in msg:
            return last
        for _ in range(retries):
            if cancelled(stop_token):
                return None, None, "Отменено", None, None, None
            if retry_delay > 0:
                time.sleep(retry_delay)
            result = URLUtils._check_url_once(
                url, timeout, verify_ssl,
                user_agent=user_agent, referrer=referrer,
                extra_headers=extra_headers,
                stop_token=stop_token)
            ok, _rt, msg, code, _ttfb, _ = result
            if ok is True or code in (403, 404):
                return result
            if msg and 'DNS' in msg:
                return result
            last = result
        return last