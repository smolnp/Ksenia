# -*- coding: utf-8 -*-
"""URLUtils, ChannelNameNormalizer, link_score, _StopToken."""

from __future__ import annotations
import re
import time
import threading
import ipaddress
from difflib import SequenceMatcher
from typing import Optional, Tuple, Set, Dict, Any
from urllib.parse import urlparse
import requests
from constants import (MAX_URL_LENGTH, STREAMING_PROTOCOLS, StatusText,
    ENABLE_HEAD_FOR_STREAMS, VLC_STREAM_CONTENT_TYPES,
    URL_CHECK_MAX_WORKERS, DEFAULT_TIMEOUT, VLC_DEFAULT_CHECK_TIMEOUT)
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
        # cp1251 → latin1 (кириллица)
        'Ð°', 'Ð±', 'Ð²', 'Ð³', 'Ð´', 'Ðµ', 'Ð¶', 'Ð·',
        'Ð¸', 'Ð¹', 'Ðº', 'Ð»', 'Ð¼', 'Ð½', 'Ð¾', 'Ð¿',
        'Ñ€', 'Ñ', 'Ñ‚', 'Ñƒ', 'Ñ„', 'Ñ…', 'Ñ†', 'Ñ‡',
        'ÐŸ', 'Ð', 'Ð¡', 'Ð¢', 'Ð£', 'Ð¤', 'Ð¥', 'Ð¦',
        'Ñ‰', 'ÑŠ', 'Ñ‹', 'ÑŒ', 'Ñ', 'ÑŽ', 'Ñ',
        # cp1251 → utf-8 двойное перекодирование
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
        return None

    _validate_url = _url_error

    @staticmethod
    def _is_stream_url(url: str) -> bool:
        if not url:
            return False
        low = url.lower().split('?', 1)[0]
        return low.endswith(('.mpd', '.m3u8', '.m3u'))

    @staticmethod
    def _read_first_chunk(response, chunk_size: int = 4096) -> bool:
        try:
            raw = response.raw
            if raw is None:
                return False
            try:
                if hasattr(raw, 'read1'):
                    chunk = raw.read1(chunk_size)
                else:
                    chunk = raw.read(chunk_size)
            except Exception:
                return False
            return bool(chunk)
        except Exception:
            return False

    @staticmethod
    def _try_head_request(session, url, timeout, verify, stop_token=None):
        """v6.1: быстрый HEAD. Возвращает (ok, rt, msg, code) или None.

        None = HEAD не подходит, надо идти в GET.
        """
        if cancelled(stop_token):
            return False, None, "Отменено", None
        start = time.time()
        try:
            with session.head(
                url,
                timeout=(timeout, timeout),
                verify=verify,
                allow_redirects=True,
            ) as r:
                rt = time.time() - start
                code = r.status_code
                if 200 <= code < 300:
                    return True, rt, f"HTTP {code} (HEAD)", code
                if code in (301, 302, 303, 307, 308):
                    return True, rt, f"HTTP {code} (HEAD)", code
                if code in (403, 404):
                    return False, rt, f"HTTP {code} (HEAD)", code
                # 405, 501, 400, 5xx — идём в GET
                return None
        except requests.exceptions.Timeout:
            return None
        except requests.exceptions.SSLError:
            return None
        except requests.exceptions.ConnectionError:
            return None
        except Exception:
            return None

    @staticmethod
    def _vlc_get_request(session, url, timeout, verify, stop_token=None):
        if cancelled(stop_token):
            return False, None, "Отменено", None
        if not ENABLE_HEAD_FOR_STREAMS and URLUtils._is_stream_url(url):
            head = None
        else:
            head = URLUtils._try_head_request(
                session, url, timeout, verify, stop_token=stop_token)
        if head is not None:
            return head
        if cancelled(stop_token):
            return False, None, "Отменено", None
        start = time.time()
        try:
            with session.get(
                url,
                timeout=(timeout, timeout),
                verify=verify,
                allow_redirects=True,
                stream=True,
            ) as response:
                rt = time.time() - start
                code = response.status_code

                if code >= 400:
                    return False, rt, f"HTTP {code}", code

                ctype = (response.headers.get('Content-Type') or '').lower()
                is_stream = URLUtils._is_stream_url(url)
                ctype_is_stream = any(
                    ct in ctype for ct in VLC_STREAM_CONTENT_TYPES)

                if is_stream and ctype_is_stream:
                    return True, rt, f"HTTP {code} ({ctype.split(';')[0]})", code

                got = URLUtils._read_first_chunk(
                    response, chunk_size=4096)
                rt = time.time() - start
                if got:
                    return True, rt, f"HTTP {code}", code

                if 200 <= code < 400:
                    return None, rt, (
                        f"HTTP {code}, но данные не пришли "
                        f"за {rt:.1f} с"), code

                return False, rt, f"HTTP {code}", code
        except requests.exceptions.Timeout:
            rt = time.time() - start
            return None, rt, "Таймаут (сервер не ответил)", None
        except requests.exceptions.SSLError:
            rt = time.time() - start
            return None, rt, "SSL ошибка (VLC игнорирует)", None
        except requests.exceptions.ConnectionError as e:
            rt = time.time() - start
            return None, rt, f"Ошибка соединения: {str(e)[:60]}", None
        except requests.exceptions.RequestException as e:
            rt = time.time() - start
            return None, rt, f"Ошибка: {str(e)[:60]}", None
        except Exception as e:
            rt = time.time() - start
            return None, rt, f"Ошибка: {str(e)[:60]}", None

    @staticmethod
    def check_url(url: str, timeout: int = VLC_DEFAULT_CHECK_TIMEOUT,
                  verify_ssl: bool = False,
                  max_retries: int = 0,
                  retry_delay: float = 0.5,
                  stop_token: Optional['_StopToken'] = None,
                  pool_size: int = URL_CHECK_MAX_WORKERS
                  ) -> Tuple[Optional[bool], Optional[float], str, Optional[int]]:
        if not url or not url.strip():
            return False, None, "Пустой URL", None
        err = URLUtils._validate_url(url)
        if err:
            return False, None, err, None

        from sources import HttpSessionFactory
        session = HttpSessionFactory.get(verify_ssl=verify_ssl,
                                          pool_size=pool_size)

        if cancelled(stop_token):
            return False, None, "Отменено", None

        ok, rt, msg, code = URLUtils._vlc_get_request(
            session, url, timeout, verify=verify_ssl, stop_token=stop_token)

        if ok is None and max_retries > 0:
            if stop_token:
                stop_token.wait(retry_delay)
            else:
                time.sleep(retry_delay)
            if cancelled(stop_token):
                return False, rt, "Отменено", code
            ok2, rt2, msg2, code2 = URLUtils._vlc_get_request(
                session, url, timeout, verify=verify_ssl, stop_token=stop_token)
            if ok2 is True:
                return True, rt2, msg2, code2
            if ok2 is False:
                return False, rt2, msg2, code2
            return None, rt, msg, code

        return ok, rt, msg, code

def link_score(channel: ChannelData, url: str,
               cached: Optional[Dict[str, Any]] = None) -> float:
    if not url or not url.strip():
        return -1000.0
    score = 0.0
    if url == channel.link.url and channel.status.url_status is True:
        score += 100.0
    elif url in channel.link.alternative_urls:
        score += 50.0
    if cached:
        if cached.get('alive'):
            score += 100.0
        ms = float(cached.get('response_ms') or 0)
        if ms > 0:
            score += max(0.0, 40.0 - ms / 100.0)
        score += min(int(cached.get('successes') or 0) * 2, 20)
        score -= min(int(cached.get('failures') or 0) * 5, 30)
    return score
