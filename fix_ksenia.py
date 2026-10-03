#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_ksenia.py v23 — атомарный фиксер Ksenia БЕЗ БЭКАПОВ.

ЧТО ДЕЛАЕТ (v23):
  1. Чистит дубли (fix_ksenia.py запускался несколько раз):
     • def _new_session() — оставляет один, остальные удаляет
     • import requests — один
     • import urllib3 + disable_warnings — один
     • from contextlib import suppress — один
  2. Чинит ksenia_window.py:
     • Убирает URLCheckerWorker из импорта (любой формат)
     • check_urls_async → SourceUrlCheckWorker
  3. Чинит workers.py:
     • Убирает URLCheckerWorker (класс)
     • НЕ удаляет _run_in_pool (нужен SourcesRefreshWorker)
  4. Заменяет check_url на дословную копию генератора
  5. Добавляет urllib3.disable_warnings
  6. Настраивает константы (4 воркера, 3 сек таймаут)
  7. paths.py — QueueHandler
  8. epg.py — убирает HttpSessionFactory + добавляет _new_session

ПРИНЦИПЫ:
  • ИДЕМПОТЕНТНОСТЬ — каждый фикс проверяет «уже применён»
  • АТОМАРНОСТЬ — если хоть один файл сломан, откатывает все;
    запись через .tmp + os.replace
  • БЕЗ БЭКАПОВ — .bak файлы НЕ создаются.
    Для откатов используйте git: git checkout -- *.py

Запуск:
    py fix_ksenia.py --dry-run    # показать без записи
    py fix_ksenia.py              # применить
    py fix_ksenia.py --verify     # проверить результат
    py fix_ksenia.py --list       # список правок
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple


# ===========================================================================
# ХЕЛПЕРЫ
# ===========================================================================

def _patch(text: str, old: str, new: str, count: int = 1) -> Tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, count), True


def _patch_all(text: str, old: str, new: str) -> Tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new), True


def is_syntax_ok(text: str) -> Tuple[bool, str]:
    try:
        ast.parse(text)
        return True, ""
    except SyntaxError as e:
        return False, f"{e.lineno}: {e.msg}"


def apply_one_fix(text: str, fn: Callable) -> Tuple[str, bool, str]:
    try:
        new_text, ok = fn(text)
    except Exception as e:
        return text, False, f"исключение: {e}"
    if not ok:
        return text, False, ""
    ok_syntax, err = is_syntax_ok(new_text)
    if not ok_syntax:
        return text, False, f"синтаксис: {err}"
    return new_text, True, ""


FIXES: List[Tuple[str, str, str, Callable[[str], Tuple[str, bool]]]] = []


def register(fname: str, desc: str, risk: str = "safe"):
    def deco(fn):
        FIXES.append((fname, desc, risk, fn))
        return fn
    return deco


# ===========================================================================
# УНИВЕРСАЛЬНЫЕ ЧИСТКИ
# ===========================================================================

def _dedupe_block(text: str, pattern: str,
                  keep_first: bool = True) -> Tuple[str, bool]:
    """Удаляет все вхождения, кроме первого."""
    matches = list(re.finditer(pattern, text, re.DOTALL))
    if len(matches) <= 1:
        return text, False
    if keep_first:
        to_remove = matches[1:]
    else:
        to_remove = matches[:-1]
    for m in reversed(to_remove):
        text = text[:m.start()] + text[m.end():]
    return text, True


def _dedupe_simple(text: str, block: str) -> Tuple[str, bool]:
    """Удаляет повторяющиеся вхождения простой строки."""
    count = text.count(block)
    if count <= 1:
        return text, False
    first = text.find(block)
    if first < 0:
        return text, False
    head = text[:first + len(block)]
    tail = text[first + len(block):].replace(block, '', count - 1)
    return head + tail, True


# ===========================================================================
# ЧИСТКА ДУБЛЕЙ
# ===========================================================================

@register("sources.py", "удалить дубли _new_session()", "safe")
def fx_sources_dedupe_new_session(text: str) -> Tuple[str, bool]:
    pattern = (
        r'def _new_session\(\) -> requests\.Session:.*?'
        r'return s\s*\n'
    )
    return _dedupe_block(text, pattern, keep_first=True)


@register("epg.py", "удалить дубли _new_session()", "safe")
def fx_epg_dedupe_new_session(text: str) -> Tuple[str, bool]:
    pattern = (
        r'def _new_session\(\) -> requests\.Session:.*?'
        r'return s\s*\n'
    )
    return _dedupe_block(text, pattern, keep_first=True)


@register("utils.py", "удалить дубли import urllib3", "safe")
def fx_utils_dedupe_urllib3(text: str) -> Tuple[str, bool]:
    block = (
        "import urllib3\n"
        "urllib3.disable_warnings("
        "urllib3.exceptions.InsecureRequestWarning)\n"
    )
    return _dedupe_simple(text, block)


@register("utils.py", "удалить дубли import requests", "safe")
def fx_utils_dedupe_requests(text: str) -> Tuple[str, bool]:
    return _dedupe_simple(text, "import requests\n")


@register("utils.py", "удалить дубли from contextlib import suppress", "safe")
def fx_utils_dedupe_suppress(text: str) -> Tuple[str, bool]:
    return _dedupe_simple(text, "from contextlib import suppress\n")


@register("epg.py", "удалить дубли import requests", "safe")
def fx_epg_dedupe_requests(text: str) -> Tuple[str, bool]:
    return _dedupe_simple(text, "import requests\n")


# ===========================================================================
# ksenia_window.py — URLCheckerWorker
# ===========================================================================

@register("ksenia_window.py", "починить from workers import (...)", "medium")
def fx_kw_fix_broken_import(text: str) -> Tuple[str, bool]:
    pattern = re.compile(
        r'from workers import\s*\([^)]*\)',
        re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return text, False
    block = m.group(0)
    if block.count('(') != block.count(')'):
        return text, False
    inside = block[len('from workers import'):].strip()
    fixed_inside = inside
    fixed_inside = re.sub(r',\s*,', ',', fixed_inside)
    fixed_inside = re.sub(r'\(\s*,', '(', fixed_inside)
    fixed_inside = re.sub(r',\s*\)', ')', fixed_inside)
    fixed_inside = re.sub(r'\bURLCheckerWorker\s*,\s*', '', fixed_inside)
    fixed_inside = re.sub(r',\s*URLCheckerWorker\b', '', fixed_inside)
    if fixed_inside == inside:
        return text, False
    fixed_block = f"from workers import {fixed_inside}"
    return _patch(text, block, fixed_block)


@register("ksenia_window.py",
          "удалить URLCheckerWorker из импорта и кода", "medium")
def fx_kw_remove_url_checker(text: str) -> Tuple[str, bool]:
    """Работает с многострочным и однострочным форматом."""
    if "URLCheckerWorker" not in text:
        return text, False
    changed = False

    def _clean_parens(m):
        nonlocal changed
        body = m.group(1)
        original = body
        body = re.sub(r'\bURLCheckerWorker\s*,\s*', '', body)
        body = re.sub(r',\s*URLCheckerWorker\b', '', body)
        body = re.sub(r'\bURLCheckerWorker\b', '', body)
        body = re.sub(r'\(\s*,', '(', body)
        body = re.sub(r',\s*,', ',', body)
        body = re.sub(r',\s*\)', ')', body)
        if body != original:
            changed = True
        return 'from workers import (' + body + ')'

    text2, n = re.subn(
        r'from workers import \(([^)]*?)\)',
        _clean_parens,
        text,
        count=0,
        flags=re.DOTALL,
    )
    if n:
        text = text2

    text2, n = re.subn(
        r'from workers import ([^\n]*?), URLCheckerWorker\b',
        r'from workers import \1',
        text,
    )
    if n:
        text = text2
        changed = True
    text2, n = re.subn(
        r'from workers import URLCheckerWorker, ([^\n]*?)\n',
        r'from workers import \1\n',
        text,
    )
    if n:
        text = text2
        changed = True

    # Удалить строки с использованием URLCheckerWorker целиком
    lines = text.splitlines(keepends=True)
    out = []
    removed = False
    for line in lines:
        if 'URLCheckerWorker' in line:
            removed = True
            continue
        out.append(line)
    if removed:
        text = ''.join(out)
        changed = True

    return text, changed


@register("workers.py", "удалить URLCheckerWorker (класс)", "medium")
def fx_workers_remove_url_checker(text: str) -> Tuple[str, bool]:
    pattern = re.compile(
        r'class URLCheckerWorker\(BaseWorker\):.*?\n'
        r'(?=\nclass \w+|\n@|\Z)',
        re.DOTALL,
    )
    new_text, n = pattern.subn('', text, count=1)
    if n == 0:
        return text, False
    return new_text, True


# ===========================================================================
# utils.py
# ===========================================================================

@register("utils.py", "импорты: socket", "safe")
def fx_utils_socket(text: str) -> Tuple[str, bool]:
    if "import socket\n" in text:
        return text, False
    return _patch(text, "import time\n",
                  "import time\nimport socket\n")


@register("utils.py", "import requests + urllib3", "safe")
def fx_utils_imports(text: str) -> Tuple[str, bool]:
    changed = False
    if "import requests\n" not in text:
        text, ok = _patch(text, "import socket\n",
                          "import socket\nimport requests\n")
        if ok:
            changed = True
    if "urllib3.disable_warnings" not in text:
        text, ok = _patch(
            text,
            "import requests\n",
            "import requests\n"
            "import urllib3\n"
            "urllib3.disable_warnings("
            "urllib3.exceptions.InsecureRequestWarning)\n",
        )
        if ok:
            changed = True
    if "from contextlib import suppress" not in text:
        text, ok = _patch(text,
                          "from __future__ import annotations\n",
                          "from __future__ import annotations\n"
                          "from contextlib import suppress\n")
        if ok:
            changed = True
    return text, changed


@register("utils.py", "удалить _vlc_get_request", "safe")
def fx_utils_remove_vlc_get(text: str) -> Tuple[str, bool]:
    pattern = re.compile(
        r'    @staticmethod\n'
        r'    def _vlc_get_request\(.*?\n'
        r'(?=\n    @staticmethod\n|\n    @classmethod\n|'
        r'\n    def \w+\(|\Z)',
        re.DOTALL,
    )
    new_text, n = pattern.subn('', text, count=1)
    if n == 0:
        return text, False
    return new_text, True


@register("utils.py",
          "удалить _try_head_request, _read_first_chunk, _is_stream_url",
          "safe")
def fx_utils_remove_head_chunk(text: str) -> Tuple[str, bool]:
    changed = False
    for method_name in ('_read_first_chunk', '_try_head_request',
                        '_is_stream_url'):
        pattern = re.compile(
            rf'    @staticmethod\n'
            rf'    def {method_name}\(.*?\n'
            rf'(?=\n    @staticmethod\n|\n    @classmethod\n|'
            rf'\n    def \w+\(|\Z)',
            re.DOTALL,
        )
        new_text, n = pattern.subn('', text, count=1)
        if n > 0:
            text = new_text
            changed = True
    return text, changed


@register("utils.py", "_check_url_single — копия генератора", "safe")
def fx_utils_add_check_single(text: str) -> Tuple[str, bool]:
    if "_check_url_single" in text:
        return text, False

    new_method = '''    @staticmethod
    def _check_url_single(url, timeout, verify_ssl, user_agent="",
                          referrer="", extra_headers=None):
        """ДОСЛОВНАЯ КОПИЯ NetworkValidator.test_network_connectivity
        из iptv_generator.py.

          • НЕТ HEAD-запроса
          • НЕТ Connection: close
          • НЕТ pool_block
          • НЕТ _hard_deadline
          • timeout=timeout (число)
          • 301/302/304/307/308 → живой
          • 200/206 → живой, читаем 1 чанк
          • .mpd (DASH) — чанк НЕ читаем
          • НИКОГДА не возвращает None
        """
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
                        low_url = url.lower().split('?', 1)[0]
                        if not low_url.endswith('.mpd'):
                            try:
                                next(response.iter_content(chunk_size=1024),
                                     None)
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

'''

    marker = "    @staticmethod\n    def check_url("
    if marker in text:
        return _patch(text, marker, new_method + marker)
    marker2 = "class URLUtils:"
    return _patch(text, marker2, marker2 + "\n" + new_method)


@register("utils.py", "check_url — тонкая обёртка", "safe")
def fx_utils_check_url_wrapper(text: str) -> Tuple[str, bool]:
    if "ЕДИНСТВЕННЫЙ механизм проверки" in text:
        return text, False

    pattern = re.compile(
        r'    @staticmethod\n'
        r'    def check_url\(url.*?\n'
        r'(?=\n    @staticmethod\n|\n    @classmethod\n|'
        r'\n    def \w+\(|\n\nclass |\Z)',
        re.DOTALL,
    )

    new_method = '''    @staticmethod
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
        """ЕДИНСТВЕННЫЙ механизм проверки ссылок в Ksenia.

        Используется И источниками, И плейлистом.
        """
        if not url or not url.strip():
            return False, None, "Пустой URL", None
        if stop_token is not None and stop_token.is_set():
            return False, None, "Отменено", None
        return URLUtils._check_url_single(
            url, timeout, verify_ssl,
            user_agent=user_agent, referrer=referrer,
            extra_headers=extra_headers)
'''

    new_text, n = pattern.subn(new_method, text, count=1)
    if n == 0:
        return text, False
    return new_text, True


@register("utils.py", "убрать Connection: close", "safe")
def fx_utils_no_connection_close(text: str) -> Tuple[str, bool]:
    changed = False
    for old in (
        "                headers={'Connection': 'close'},\n",
        "            headers={'Connection': 'close'},\n",
        "        headers={'Connection': 'close'},\n",
        "                headers={'Connection': 'close'}\n",
        "            headers={'Connection': 'close'}\n",
        "        headers={'Connection': 'close'}\n",
    ):
        text, ok = _patch_all(text, old, "")
        if ok:
            changed = True
    return text, changed


# ===========================================================================
# sources.py
# ===========================================================================

@register("sources.py", "удалить HttpSessionFactory", "safe")
def fx_sources_remove_factory(text: str) -> Tuple[str, bool]:
    if "class HttpSessionFactory" not in text:
        return text, False
    pattern = re.compile(
        r'class HttpSessionFactory:.*?\n'
        r'(?=\nclass \w+|\n@|\Z)',
        re.DOTALL,
    )
    new_text, n = pattern.subn('', text, count=1)
    if n == 0:
        return text, False
    return new_text, True


@register("sources.py", "добавить _new_session()", "safe")
def fx_sources_add_helper(text: str) -> Tuple[str, bool]:
    if "def _new_session():" in text:
        return text, False
    marker = "from paths import logger, parse_datetime\n"
    if marker not in text:
        marker = "from paths import logger\n"
    if marker not in text:
        return text, False
    helper = '''

def _new_session() -> requests.Session:
    """Простая сессия, как в генераторе."""
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36',
        'Accept': '*/*',
    })
    return s

'''
    return _patch(text, marker, marker + helper)


@register("sources.py", "убрать вызовы HttpSessionFactory", "safe")
def fx_sources_remove_factory_usage(text: str) -> Tuple[str, bool]:
    changed = False
    for old in (
        "HttpSessionFactory.get(verify_ssl=False)",
        "HttpSessionFactory.get(verify_ssl=verify_ssl, pool_size=pool_size)",
        "HttpSessionFactory.get(verify_ssl=False, pool_size=pool_size)",
        "HttpSessionFactory.get()",
    ):
        text, ok = _patch_all(text, old, "_new_session()")
        if ok:
            changed = True
    return text, changed


@register("sources.py", "убрать pool_block=True", "safe")
def fx_sources_no_pool_block(text: str) -> Tuple[str, bool]:
    old = """            pool_connections=pool_size,
            pool_maxsize=pool_size,
            pool_block=True,
        )
"""
    new = """            pool_connections=pool_size,
            pool_maxsize=pool_size,
        )
"""
    return _patch(text, old, new)


# ===========================================================================
# epg.py
# ===========================================================================

@register("epg.py", "убрать импорт HttpSessionFactory", "safe")
def fx_epg_remove_factory_import(text: str) -> Tuple[str, bool]:
    if "HttpSessionFactory" not in text:
        return text, False
    changed = False
    for old in (
        "from sources import HttpSessionFactory\n",
        "from sources import HttpSessionFactory, _StopToken, cancelled\n",
        "from sources import HttpSessionFactory, _StopToken\n",
    ):
        text2, ok = _patch(text, old, "")
        if ok:
            text = text2
            changed = True
    for pattern, repl in (
        (r'from sources import \([^)]*?HttpSessionFactory,?\s*([^)]*?)\)',
         r'from sources import \1'),
        (r'from sources import ([^\n]*?), HttpSessionFactory\n',
         r'from sources import \1\n'),
        (r'from sources import HttpSessionFactory, ([^\n]*?)\n',
         r'from sources import \1\n'),
    ):
        text2, n = re.subn(pattern, repl, text)
        if n:
            text = text2
            changed = True
    return text, changed


@register("epg.py", "добавить _new_session()", "safe")
def fx_epg_add_helper(text: str) -> Tuple[str, bool]:
    if "def _new_session()" in text:
        return text, False
    marker = "from paths import logger\n"
    if marker not in text:
        return text, False
    helper = '''

def _new_session() -> requests.Session:
    """Простая сессия, как в генераторе."""
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36',
        'Accept': '*/*',
    })
    return s
'''
    return _patch(text, marker, marker + helper)


@register("epg.py", "заменить HttpSessionFactory.get()", "safe")
def fx_epg_replace_factory_usage(text: str) -> Tuple[str, bool]:
    if "HttpSessionFactory.get" not in text:
        return text, False
    changed = False
    for old in (
        "HttpSessionFactory.get(verify_ssl=False)",
        "HttpSessionFactory.get(verify_ssl=verify_ssl)",
        "HttpSessionFactory.get()",
    ):
        text, ok = _patch_all(text, old, "_new_session()")
        if ok:
            changed = True
    return text, changed


# ===========================================================================
# constants.py
# ===========================================================================

@register("constants.py", "VLC_DEFAULT_CHECK_TIMEOUT = 3", "safe")
def fx_const_vlc_timeout(text: str) -> Tuple[str, bool]:
    for old in ("VLC_DEFAULT_CHECK_TIMEOUT = 8",
                "VLC_DEFAULT_CHECK_TIMEOUT = 5",
                "VLC_DEFAULT_CHECK_TIMEOUT = 2"):
        text, ok = _patch(text, old, "VLC_DEFAULT_CHECK_TIMEOUT = 3")
        if ok:
            return text, True
    return text, False


@register("constants.py", "SOURCE_CHECK_TIMEOUT_DEFAULT = 3", "safe")
def fx_const_src_timeout(text: str) -> Tuple[str, bool]:
    for old in ("SOURCE_CHECK_TIMEOUT_DEFAULT = 8",
                "SOURCE_CHECK_TIMEOUT_DEFAULT = 5",
                "SOURCE_CHECK_TIMEOUT_DEFAULT = 2"):
        text, ok = _patch(text, old, "SOURCE_CHECK_TIMEOUT_DEFAULT = 3")
        if ok:
            return text, True
    return text, False


@register("constants.py", "SOURCE_CHECK_WORKERS_DEFAULT = 4", "safe")
def fx_const_src_workers(text: str) -> Tuple[str, bool]:
    for old in ("SOURCE_CHECK_WORKERS_DEFAULT = 32",
                "SOURCE_CHECK_WORKERS_DEFAULT = 24",
                "SOURCE_CHECK_WORKERS_DEFAULT = 20",
                "SOURCE_CHECK_WORKERS_DEFAULT = 16",
                "SOURCE_CHECK_WORKERS_DEFAULT = 12",
                "SOURCE_CHECK_WORKERS_DEFAULT = 8"):
        text, ok = _patch(text, old, "SOURCE_CHECK_WORKERS_DEFAULT = 4")
        if ok:
            return text, True
    return text, False


@register("constants.py", "URL_CHECK_MAX_WORKERS = 4", "safe")
def fx_const_url_workers(text: str) -> Tuple[str, bool]:
    for old in ("URL_CHECK_MAX_WORKERS = 20",
                "URL_CHECK_MAX_WORKERS = 16",
                "URL_CHECK_MAX_WORKERS = 12",
                "URL_CHECK_MAX_WORKERS = 8",
                "URL_CHECK_MAX_WORKERS = 6"):
        text, ok = _patch(text, old, "URL_CHECK_MAX_WORKERS = 4")
        if ok:
            return text, True
    return text, False


# ===========================================================================
# workers.py
# ===========================================================================

@register("workers.py", "убрать [SLOW] из лога", "safe")
def fx_workers_no_slow_log(text: str) -> Tuple[str, bool]:
    old = '''                _dt = time.time() - _t0
                if _dt > float(self.timeout) + 2.0:
                    logger.warning(
                        f"[SLOW] [{self.source_name}] "
                        f"{url[:80]} took {_dt:.1f}s "
                        f"(timeout={self.timeout}, ok={ok})")
'''
    new = '''                # [SLOW] убран
'''
    return _patch(text, old, new)


@register("workers.py", "убрать logger.exception на каждый URL", "safe")
def fx_workers_no_exception_log(text: str) -> Tuple[str, bool]:
    old = '''                except Exception:
                    logger.exception(
                        f"SourceUrlCheckWorker check {url[:80]}")
                    ok, rt, msg, code = False, 0.0, "exception", None
'''
    new = '''                except Exception as e:
                    ok, rt, msg, code = False, 0.0, \\
                                          f"exception: {str(e)[:40]}", None
'''
    return _patch(text, old, new)


@register("workers.py", "убрать channel_checked эмит", "safe")
def fx_workers_no_channel_emit(text: str) -> Tuple[str, bool]:
    old = '''                if _should_emit and ok is not None:
                    with suppress(Exception):
                        self.channel_checked.emit(
                            ch.meta.name, bool(ok),
                            (msg or "")[:80])
'''
    new = '''                # channel_checked убран
                pass
'''
    return _patch(text, old, new)


@register("workers.py", "прогресс в лог каждые 500 URL", "safe")
def fx_workers_src_log_progress(text: str) -> Tuple[str, bool]:
    if "[SourceUrlCheckWorker" in text and "checked=" in text:
        return text, False
    old = '''                    cur = checked
                    if cur - last_emit_count >= 5 or cur == len(to_check):
                        last_emit_count = cur
                        self.source_check_progress.emit(
                            self.source_name, cur, total)
'''
    new = '''                    cur = checked
                    if cur - last_emit_count >= 5 or cur == len(to_check):
                        last_emit_count = cur
                        self.source_check_progress.emit(
                            self.source_name, cur, total)
                    if cur % 500 == 0 and cur > 0:
                        logger.info(
                            f"[SourceUrlCheckWorker {self.source_name}] "
                            f"checked={cur}/{len(to_check)}, "
                            f"working={working}")
'''
    return _patch(text, old, new)


@register("workers.py", "короткий timeout для .mpd", "safe")
def fx_workers_src_mpd_timeout(text: str) -> Tuple[str, bool]:
    old = '''                _t0 = time.time()
                try:
                    ok, rt, msg, code = URLUtils.check_url(
                        url, self.timeout, verify_ssl=False,
                        max_retries=0, retry_delay=0.0,
                        stop_token=self._stop_token, pool_size=4)
'''
    new = '''                _t0 = time.time()
                _timeout = self.timeout
                if url.lower().split('?', 1)[0].endswith('.mpd'):
                    _timeout = max(3, self.timeout // 2)
                try:
                    ok, rt, msg, code = URLUtils.check_url(
                        url, _timeout, verify_ssl=False,
                        max_retries=0, retry_delay=0.0,
                        stop_token=self._stop_token, pool_size=4)
'''
    return _patch(text, old, new)


@register("workers.py", "убрать processEvents", "safe")
def fx_workers_no_process_events(text: str) -> Tuple[str, bool]:
    old = '''                    if cur - last_yield_count >= YIELD_EVERY:
                        last_yield_count = cur
                        try:
                            from PyQt6.QtCore import QCoreApplication
                            QCoreApplication.processEvents()
                        except Exception:
                            pass
                        time.sleep(0)
'''
    new = '''                    time.sleep(0)
'''
    return _patch(text, old, new)


# ===========================================================================
# ksenia_window.py
# ===========================================================================

@register("ksenia_window.py",
          "check_urls_async → SourceUrlCheckWorker", "medium")
def fx_kw_check_via_source_worker(text: str) -> Tuple[str, bool]:
    if "ЕДИНЫЙ механизм проверки — как у источников" in text:
        return text, False

    pattern = re.compile(
        r'    def check_urls_async\(self, channels.*?\n'
        r'(?=\n    def |\n    @|\nclass |\Z)',
        re.DOTALL,
    )

    new_method = '''    def check_urls_async(self, channels: List[ChannelData],
                         force: bool = True):
        """ЕДИНЫЙ механизм проверки — как у источников.

        Использует SourceUrlCheckWorker (тот же, что «Обновить всё»
        в менеджере источников).
        """
        if not _is_qobject_valid(self):
            return
        if getattr(self, '_src_check_worker', None) is not None \\
                and self._src_check_worker.isRunning():
            info_box(self, "Проверка уже запущена")
            return

        to_check: List[ChannelData] = list(channels)
        if not to_check:
            return

        limit_enabled = self.core.config.get('limit_check_enabled', False)
        limit = int(self.core.config.get('max_channels_to_check', 2000))
        if limit_enabled and len(to_check) > limit:
            to_check = to_check[:limit]

        settings = self.core.get_replacement_settings()
        w = self.parent_window
        if w is not None:
            w.set_action(
                f"⏳ Проверка ссылок: {len(to_check)} каналов")

        from workers import SourceUrlCheckWorker
        timeout = max(int(settings.check_timeout or 3), 3)
        max_workers = int(self.core.config.get(
            'source_check_workers', 4))
        trust_sec = 0
        batch_size = int(self.core.config.get(
            'source_check_batch_size', 100))

        worker = SourceUrlCheckWorker(
            source_name="__playlist__",
            channels=to_check,
            cache_manager=self.core.cache_manager,
            max_workers=max_workers,
            timeout=timeout,
            trust_sec=trust_sec,
            batch_size=batch_size,
            stop_token=None,
        )
        self._src_check_worker = worker

        def on_progress(name, cur, tot):
            w2 = self.parent_window
            if w2 is not None:
                w2.show_progress(cur, max(1, tot),
                                 f"Проверено: {cur}/{tot}")

        def on_done(name, working, total):
            if getattr(self, '_src_check_worker', None) is worker:
                self._src_check_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if not _is_qobject_valid(self):
                return
            with self._suppress_save():
                self.undo_manager.save_state(
                    self.all_channels, "Проверка ссылок")
            self.sync_to_core()
            with self._suppress_save():
                self.model.refresh_all()
            self.update_info()
            with suppress(Exception):
                self.core.link_source_manager.rebuild_alive_index()
            if _is_qobject_valid(self):
                info_box(
                    self,
                    f"Проверка завершена.\\n"
                    f"Рабочих: {working} из {total}",
                    "Проверка ссылок")

        def on_error(msg):
            if getattr(self, '_src_check_worker', None) is worker:
                self._src_check_worker = None
            w2 = self.parent_window
            if w2 is not None:
                w2.hide_progress()
            if _is_qobject_valid(self):
                error_box(self, msg, "Ошибка проверки")

        worker.source_check_progress.connect(on_progress)
        worker.source_check_done.connect(on_done)
        worker.error.connect(on_error)
        worker.start()
'''

    new_text, n = pattern.subn(new_method, text, count=1)
    if n == 0:
        return text, False
    return new_text, True


@register("ksenia_window.py", "check_all_urls: force=True", "safe")
def fx_kw_check_all_force(text: str) -> Tuple[str, bool]:
    old = '''        if not confirm(self, f"Проверить {len(with_urls)} ссылок?"):
            return
        self.check_urls_async(with_urls)
'''
    new = '''        if not confirm(self, f"Проверить {len(with_urls)} ссылок?"):
            return
        self.check_urls_async(with_urls, force=True)
'''
    return _patch(text, old, new)


@register("ksenia_window.py", "_check_selected_urls: force=True", "safe")
def fx_kw_check_selected_force(text: str) -> Tuple[str, bool]:
    old = '''        if not with_urls:
            info_box(self, "У выбранных каналов нет URL")
            return
        self.check_urls_async(with_urls)
'''
    new = '''        if not with_urls:
            info_box(self, "У выбранных каналов нет URL")
            return
        self.check_urls_async(with_urls, force=True)
'''
    return _patch(text, old, new)


@register("ksenia_window.py", "_check_single_url: force=True", "safe")
def fx_kw_check_single_force(text: str) -> Tuple[str, bool]:
    old = '''    def _check_single_url(self, row: int):
        ch = self.channel_for_row(row)
        if ch and ch.link.url:
            self.check_urls_async([ch])
'''
    new = '''    def _check_single_url(self, row: int):
        ch = self.channel_for_row(row)
        if ch and ch.link.url:
            self.check_urls_async([ch], force=True)
'''
    return _patch(text, old, new)


@register("ksenia_window.py",
          "заменить _url_checker_worker на _src_check_worker", "safe")
def fx_kw_rename_url_checker(text: str) -> Tuple[str, bool]:
    changed = False
    if "self._url_checker_worker" in text:
        text = text.replace("self._url_checker_worker",
                            "self._src_check_worker")
        changed = True
    return text, changed


@register("ksenia_window.py", "убрать вызов _install_exit_watchdog()", "safe")
def fx_kw_remove_watchdog(text: str) -> Tuple[str, bool]:
    if "_install_exit_watchdog()" not in text:
        return text, False
    text = text.replace("    _install_exit_watchdog()\n", "")
    return text, True


# ===========================================================================
# paths.py
# ===========================================================================

@register("paths.py", "FileHandler → QueueHandler", "safe")
def fx_paths_queue_handler(text: str) -> Tuple[str, bool]:
    if "QueueHandler" in text:
        return text, False
    old = '''        try:
            config_dir = Paths.get_config_dir()
            os.makedirs(config_dir, exist_ok=True)
            log_path = Paths.get_log_path()
            fh = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding='utf-8')
            fh.setFormatter(fmt)
            logger_.addHandler(fh)
        except Exception:
            pass
'''
    new = '''        try:
            import queue as _queue
            from logging.handlers import QueueHandler, QueueListener
            import atexit as _atexit
            config_dir = Paths.get_config_dir()
            os.makedirs(config_dir, exist_ok=True)
            log_path = Paths.get_log_path()
            fh = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding='utf-8')
            fh.setFormatter(fmt)
            log_queue = _queue.Queue(-1)
            qh = QueueHandler(log_queue)
            qh.setFormatter(fmt)
            logger_.addHandler(qh)
            _listener = QueueListener(log_queue, fh,
                                      respect_handler_level=True)
            _listener.start()
            _atexit.register(_listener.stop)
        except Exception:
            pass
'''
    return _patch(text, old, new)


# ===========================================================================
# ПОИСК КОРНЯ ПРОЕКТА
# ===========================================================================

def find_root(base: Path) -> Optional[Path]:
    if base.is_file():
        base = base.parent
    if (base / "ksenia.py").exists() and \
            (base / "ksenia_window.py").exists():
        return base
    for p in sorted(base.iterdir()):
        if p.is_dir() and (p / "ksenia_window.py").exists() \
                and (p / "ksenia.py").exists():
            return p
    for p in sorted(base.iterdir()):
        if not p.is_dir() or p.name.startswith('.'):
            continue
        for q in sorted(p.iterdir()):
            if q.is_dir() and (q / "ksenia_window.py").exists() \
                    and (q / "ksenia.py").exists():
                return q
    return None


# ===========================================================================
# АТОМАРНАЯ ЗАПИСЬ (без бэкапов)
# ===========================================================================

def atomic_write(fp: Path, text: str) -> bool:
    """Пишет через .tmp + os.replace. Бэкапы НЕ создаются."""
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, fp)
        return True
    except Exception as e:
        print(f"  [!] Не удалось записать {fp.name}: {e}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


# ===========================================================================
# MAIN
# ===========================================================================

def print_fixes_list():
    print("=" * 78)
    print("  Список правок fix_ksenia.py v23 (без бэкапов)")
    print("=" * 78)
    by_file: dict = {}
    for fname, desc, risk, _fn in FIXES:
        by_file.setdefault(fname, []).append((risk, desc))
    for fname in sorted(by_file):
        print(f"\n{fname}:")
        for risk, desc in by_file[fname]:
            mark = "[РИСК]" if risk == "medium" else "[safe]"
            print(f"  {mark:8} {desc}")
    print(f"\nВсего: {len(FIXES)} правок")


def verify_markers(root: Path) -> bool:
    print("\n[*] Проверка маркеров:")
    all_ok = True

    checks = [
        ("utils.py", "def _check_url_single(", "_check_url_single"),
        ("utils.py", "ЕДИНСТВЕННЫЙ механизм проверки", "check_url"),
        ("utils.py", "urllib3.disable_warnings", "disable_warnings"),
        ("sources.py", "def _new_session()", "_new_session sources"),
        ("epg.py", "def _new_session()", "_new_session epg"),
        ("constants.py", "SOURCE_CHECK_WORKERS_DEFAULT = 4", "workers=4"),
        ("constants.py", "URL_CHECK_MAX_WORKERS = 4", "url_workers=4"),
        ("ksenia_window.py", "ЕДИНЫЙ механизм проверки",
         "check_urls_async через SourceUrlCheckWorker"),
        ("paths.py", "QueueHandler", "QueueHandler"),
    ]
    for fname, marker, consequence in checks:
        fp = root / fname
        if not fp.exists():
            print(f"  [!] {fname}: файла нет")
            all_ok = False
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"  [!] {fname}: не UTF-8")
            all_ok = False
            continue
        if marker in text:
            print(f"  [✓] {fname}: {marker[:60]}")
        else:
            print(f"  [!] {fname}: нет '{marker[:60]}' — {consequence}")
            all_ok = False

    print("\n[*] Проверка дублей:")
    dup_checks = [
        ("sources.py", "def _new_session()", 1),
        ("epg.py", "def _new_session()", 1),
        ("utils.py", "urllib3.disable_warnings", 1),
    ]
    for fname, marker, expected in dup_checks:
        fp = root / fname
        if not fp.exists():
            continue
        text = fp.read_text(encoding="utf-8")
        count = text.count(marker)
        if count == expected:
            print(f"  [✓] {fname}: {marker[:40]} — {count} шт.")
        else:
            print(f"  [!] {fname}: {marker[:40]} — {count} шт. "
                  f"(ожидалось {expected})")
            all_ok = False

    print("\n[*] Проверка, что удалено:")
    for f in sorted(root.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for forbidden in ("HttpSessionFactory", "_vlc_get_request"):
            if forbidden in text:
                print(f"  [!] {f.name}: ОСТАЛОСЬ '{forbidden}'")
                all_ok = False
        if f.name == "workers.py" and "class URLCheckerWorker" in text:
            print(f"  [!] {f.name}: class URLCheckerWorker ОСТАЛСЯ")
            all_ok = False
        if f.name == "ksenia_window.py" and "URLCheckerWorker" in text:
            print(f"  [!] {f.name}: URLCheckerWorker ОСТАЛСЯ")
            all_ok = False
    if all_ok:
        print("  [✓] Лишнее удалено везде")

    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fix_ksenia",
        description="Ksenia v23 — атомарный фиксер БЕЗ БЭКАПОВ")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.list:
        print_fixes_list()
        return 0

    base = Path(__file__).parent.resolve()
    root = find_root(base)
    if root is None:
        print(f"[!] Не нашёл проект в {base}")
        return 2

    print("=" * 78)
    print("  Ksenia — fix_ksenia.py v23")
    print("  Атомарный фиксер (БЕЗ БЭКАПОВ)")
    if args.dry_run:
        print("  [DRY-RUN] Файлы не будут изменены")
    if args.verify:
        print("  [VERIFY] Только проверки")
    print("=" * 78)

    print(f"[*] Проект: {root}\n")

    if args.verify:
        ok = verify_markers(root)
        print()
        print("=" * 78)
        print("[✓] Проверки пройдены." if ok
              else "[!] Есть предупреждения.")
        return 0 if ok else 1

    stats = {'applied': 0, 'failed': [], 'files_changed': 0}
    file_contents = {}

    print("[*] Применение код-фиксов...")
    by_file: dict = {}
    for fname, desc, risk, fn in FIXES:
        by_file.setdefault(fname, []).append((desc, risk, fn))

    for fname, items in by_file.items():
        fp = root / fname
        if not fp.exists():
            print(f"  [!] {fname}: файла нет")
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"  [!] {fname}: не UTF-8")
            continue
        new_text = text
        file_changed = False
        for desc, risk, fn in items:
            new_text, ok, err = apply_one_fix(new_text, fn)
            if ok:
                mark = "[РИСК]" if risk == "medium" else "[+]"
                print(f"  {mark} {fname}: {desc}")
                stats['applied'] += 1
                file_changed = True
            elif err:
                print(f"  [!] {fname}: {desc} — {err}")
                stats['failed'].append((fname, desc, err))
        if file_changed:
            file_contents[fname] = new_text

    print()
    print("[*] Проверка синтаксиса всех изменённых файлов...")
    syntax_errors = []
    for fname, new_text in file_contents.items():
        ok, err = is_syntax_ok(new_text)
        if not ok:
            print(f"  [!] {fname}: {err}")
            syntax_errors.append((fname, err))

    if syntax_errors:
        print()
        print("=" * 78)
        print("[!] ОТМЕНА: синтаксис сломан в файлах:")
        for fname, err in syntax_errors:
            print(f"    - {fname}: {err}")
        print("    Ничего не записано.")
        return 3

    if args.dry_run:
        print()
        print("[DRY-RUN] Файлы не изменены.")
        return 0

    print()
    print("[*] Атомарная запись (без .bak)...")
    for fname, new_text in file_contents.items():
        fp = root / fname
        if atomic_write(fp, new_text):
            print(f"  [+] {fname}: записан")
            stats['files_changed'] += 1

    print()
    print("=" * 78)
    print(f"[✓] Файлов изменено: {stats['files_changed']}")
    print(f"[✓] Код-фиксов применено: {stats['applied']}")
    if stats['failed']:
        print(f"[!] Ошибок: {len(stats['failed'])}")
        for fname, desc, err in stats['failed'][:10]:
            print(f"    - {fname}: {desc} — {err}")

    print()
    print("[*] Проверка синтаксиса...")
    bad = []
    for f in sorted(root.glob("*.py")):
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append((f.name, e.lineno, e.msg))
    if bad:
        for name, ln, msg in bad:
            print(f"  [!] {name}:{ln}: {msg}")
    else:
        print("  [✓] Все файлы корректны")

    all_ok = verify_markers(root)

    print()
    print("=" * 78)
    if all_ok and not bad:
        print("[✓] Готово.")
        print()
        print("    Проверка ссылок теперь ОДНА:")
        print("      • Проверка источников = проверка плейлиста")
        print("      • Оба через SourceUrlCheckWorker")
        print("      • URLUtils.check_url → _check_url_single")
        print()
        print("    Запустите:  py ksenia.py")
    else:
        print("[!] Готово, но есть предупреждения.")
    print()
    print("    БЭКАПЫ НЕ СОЗДАЮТСЯ (v23).")
    print("    Для отката используйте git: git checkout -- *.py")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
