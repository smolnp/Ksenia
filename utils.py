# -*- coding: utf-8 -*-
"""URLUtils, ChannelNameNormalizer, _StopToken."""

from __future__ import annotations
import re
import time
import socket
import threading
import ipaddress
from contextlib import suppress
from difflib import SequenceMatcher
from typing import Optional, Tuple, Set
from urllib.parse import urlparse
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from constants import MAX_URL_LENGTH, StatusText
from paths import logger


class ChannelNameNormalizer:
    QUALITY_PATTERNS = [
        r'\(\s*(?:720|1080|1280|480|360|2160|4K|8K|UHD|HD|SD|FHD|FullHD)[pPi]?\s*\)',
        r'\(\s*(?:720|1080|1280|480|360|2160)[pP]\d*\s*\)',
        r'\(\s*(?:4K|8K|UHD|HD|SD|FHD|FullHD)\s*\)',
        r'\(\s*(?:60|50|30|25)[fFpPsS]?\s*\)',
        r'\(\s*(?:High|Medium|Low)\s+Quality\s*\)',
        r'\(\s*\[?[0-9]+[pPi]\s*\]?\s*\)',
        r'\(\s*HQ\s*\)', r'\(\s*LQ\s*\)', r'\(\s*SQ\s*\)',
        r'\[\s*(?:720|1080|1280|480|360|2160|4K|8K|UHD|HD|SD|FHD|FullHD)[pPi]?\s*\]',
        r'\[\s*(?:4K|8K|UHD|HD|SD|FHD|FullHD)\s*\]',
        r'\[\s*(?:60|50|30|25)[fFpPsS]?\s*\]',
        r'\(\s*(?:SD|HD)\s*/\s*(?:HD|FHD)\s*\)',
    ]
    BRACKETS_TO_REMOVE = [
        r'\[Geo-blocked\]', r'\[Not\s+24/7\]', r'\[Geo[-\s]?blocked\]',
        r'\[Not\s*24\s*/\s*7\]', r'\[Blocked\]', r'\[Restricted\]',
        r'\[GeoRestricted\]',
    ]
    MOJIBAKE_TRIGGERS = (
        'Ð°', 'Ð±', 'Ð²', 'Ð³', 'Ð´', 'Ðµ', 'Ð¶', 'Ð·',
        'Ð¸', 'Ð¹', 'Ðº', 'Ð»', 'Ð¼', 'Ð½', 'Ð¾', 'Ð¿',
        'Ñ€', 'Ñ', 'Ñ‚', 'Ñƒ', 'Ñ„', 'Ñ…', 'Ñ†', 'Ñ‡',
        'ÐŸ', 'Ð', 'Ð¡', 'Ð¢', 'Ð£', 'Ð¤', 'Ð¥', 'Ð¦',
        'Ñ‰', 'ÑŠ', 'Ñ‹', 'ÑŒ', 'Ñ', 'ÑŽ', 'Ñ',
        '–°', '–∞', '–µ', '–∏', '—Å', '—Ä', '√©', '√®',
    )
    EMOJI_PATTERN = re.compile(
        '[' '\U0001F600-\U0001F64F' '\U0001F300-\U0001F5FF'
        '\U0001F680-\U0001F6FF' '\U0001F1E0-\U0001F1FF'
        '\U00002702-\U000027B0' '\U000024C2-\U0001F251' ']+',
        flags=re.UNICODE)
    _SPECIAL_CHARS_RE = re.compile(r'[^\w\s\-\+\\/\.\,:()\[\]]')
    _WS_RE = re.compile(r'\s+')

    @staticmethod
    def fix_encoding(text: str) -> str:
        if not text:
            return text
        if not any(t in text for t in ChannelNameNormalizer.MOJIBAKE_TRIGGERS):
            return text
        try:
            fixed = text.encode('cp1251').decode('utf-8', errors='strict')
            if fixed and all(ord(c) < 0x10000 for c in fixed):
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        try:
            return text.encode('latin1').decode('utf-8', errors='strict')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text

    @staticmethod
    def normalize(name: str, remove_parentheses: bool = True,
                  remove_brackets: bool = True, remove_emojis: bool = True,
                  remove_special_chars: bool = True) -> str:
        if not name:
            return ""
        normalized = ChannelNameNormalizer.fix_encoding(name)
        if remove_parentheses:
            for pattern in ChannelNameNormalizer.QUALITY_PATTERNS:
                normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)
            normalized = re.sub(r'\(\s*\)', '', normalized)
        if remove_brackets:
            for pattern in ChannelNameNormalizer.BRACKETS_TO_REMOVE:
                normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)
            normalized = re.sub(r'\[\s*\]', '', normalized)
        normalized = re.sub(r'\{[^}]*\}', '', normalized)
        if remove_emojis:
            normalized = ChannelNameNormalizer.EMOJI_PATTERN.sub('', normalized)
        if remove_special_chars:
            normalized = ChannelNameNormalizer._SPECIAL_CHARS_RE.sub(' ', normalized)
        normalized = normalized.lower().strip()
        normalized = ChannelNameNormalizer._WS_RE.sub(' ', normalized)
        return normalized

    @staticmethod
    def token_set(name: str) -> Set[str]:
        return set(re.findall(r'\w+', name.lower()))

    @staticmethod
    def jaccard(a: str, b: str) -> float:
        sa = ChannelNameNormalizer.token_set(a)
        sb = ChannelNameNormalizer.token_set(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    @staticmethod
    def similarity(name1: str, name2: str) -> float:
        return SequenceMatcher(None, name1.lower(), name2.lower()).ratio()


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


class URLUtils:
    @staticmethod
    def extract_host(url: str) -> str:
        if not url or not url.strip():
            return ""
        try:
            host = urlparse(url).hostname
            return (host or "").lower()
        except Exception:
            return ""

    @staticmethod
    def normalize_host(raw: str) -> str:
        if not raw:
            return ""
        s = raw.strip().lower()
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
        else:
            if ':' in s and s.count(':') == 1:
                s = s.split(':', 1)[0]
        s = s.strip().strip('.')
        return s

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
            return ipaddress.ip_address(host) == ipaddress.ip_address(target_ip)
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
        if include_subdomains and host.endswith("." + target_host):
            return True
        return False

    @staticmethod
    def classify_status(url: str, status_code: Optional[int] = None,
                        error: str = "") -> Tuple[str, Optional[int]]:
        if not url or not url.strip():
            return StatusText.NO_URL, None
        if error:
            e = error.lower()
            if "таймаут" in e or "timeout" in e:
                return StatusText.TIMEOUT, status_code
            if "dns" in e:
                return StatusText.DNS_FAIL, status_code
            if "соединени" in e or "connection" in e:
                return StatusText.CONN_ERROR, status_code
            return f"🆘 {error[:40]}", status_code
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
    def _url_error(url: str) -> Optional[str]:
        """None = URL валиден. Строка = текст ошибки."""
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
            if p.port and p.port not in (80, 443, 8080, 8000, 8888):
                return "URL без пути на нестандартном порту"
        return None

    _validate_url = _url_error

    @staticmethod
    def _check_url_single(url, timeout, verify_ssl, user_agent="",
                          referrer="", extra_headers=None):
        """Дословная копия NetworkValidator.test_network_connectivity."""
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if hostname:
                try:
                    socket.gethostbyname(hostname)
                except socket.gaierror:
                    return False, 0, "DNS резолвинг не удался", None

            start_time = time.time()
            session = requests.Session()
            headers = {
                'User-Agent': user_agent or
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36',
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
                with session.get(
                    url, timeout=timeout, allow_redirects=True,
                    verify=False, stream=True,
                ) as response:
                    response_time = time.time() - start_time
                    code = response.status_code
                    if code in (200, 206, 301, 302, 304, 307, 308):
                        try:
                            next(response.iter_content(chunk_size=1024), None)
                        except Exception:
                            pass
                        return True, response_time, f"HTTP {code}", code
                    return False, response_time, f"HTTP {code}", code
            except requests.Timeout:
                return False, timeout, "Превышен таймаут", None
            except requests.ConnectionError:
                return False, 0, "Ошибка соединения", None
            except Exception as e:
                return False, 0, f"Ошибка: {str(e)[:60]}", None
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        except Exception as e:
            return False, 0, f"Критическая ошибка: {str(e)[:60]}", None

    @staticmethod
    def check_url(url: str, timeout: int = 3,
                  verify_ssl: bool = False,
                  max_retries: int = 0,
                  retry_delay: float = 0.5,
                  stop_token=None,
                  pool_size: int = 4,
                  user_agent: str = "",
                  referrer: str = "",
                  extra_headers=None
                  ) -> Tuple[Optional[bool], Optional[float], str, Optional[int]]:
        """ЕДИНСТВЕННЫЙ механизм проверки ссылок в Ksenia."""
        if not url or not url.strip():
            return False, None, "Пустой URL", None
        if stop_token is not None and stop_token.is_set():
            return False, None, "Отменено", None
        return URLUtils._check_url_single(
            url, timeout, verify_ssl,
            user_agent=user_agent, referrer=referrer,
            extra_headers=extra_headers)