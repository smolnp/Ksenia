# -*- coding: utf-8 -*-
"""EPGDatabase — база XMLTV с кэшем и нечётким поиском."""
from __future__ import annotations

import gzip
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from defusedxml.ElementTree import fromstring as safe_fromstring

from constants import (EPG_MAX_BYTES, EPG_SOURCE_TIMEOUT_SEC,
    EPG_CACHE_TTL_HOURS, EPG_FUZZY_CACHE_LIMIT,
    EPG_FUZZY_MIN_LENGTH_DEFAULT, EPG_FUZZY_MIN_GAP_DEFAULT,
    EPG_ALLOWED_META_FIELDS, EPG_ALTERNATIVE_NAMES_MAX)
from epg_naming import (pick_all_alternative_names,
                         pick_canonical_display_name)
from logging_setup import logger
from models import ChannelData, EPGEntry, EPGChannelInfo
from name_normalizer import (NormMode, is_stub_name,
    is_stub_tvg_id, normalize_with_settings, similarity)
from utils import _StopToken, cancelled


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36'),
        'Accept': '*/*',
    })
    return s


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _prefer_tvg_id_from_settings(settings: Any,
                                  default: bool = True) -> bool:
    if settings is None:
        return default
    try:
        v = getattr(settings, 'epg_prefer_tvg_id_over_name', None)
        if v is not None:
            return bool(v)
    except Exception:
        pass
    try:
        cfg = getattr(settings, '_cfg', None)
        if cfg is not None:
            return bool(cfg.get('epg_prefer_tvg_id_over_name',
                                 default))
    except Exception:
        pass
    return default


def _epg_fuzzy_params_from_settings(settings: Any
                                     ) -> Tuple[float, int, float]:
    """FIX: читает параметры fuzzy-поиска EPG из настроек."""
    if settings is None:
        return (1.0, EPG_FUZZY_MIN_LENGTH_DEFAULT,
                EPG_FUZZY_MIN_GAP_DEFAULT)
    try:
        enabled = bool(getattr(settings,
                                'epg_fuzzy_match_enabled', True))
        if not enabled:
            return 1.0, EPG_FUZZY_MIN_LENGTH_DEFAULT, \
                EPG_FUZZY_MIN_GAP_DEFAULT
        threshold = float(getattr(settings,
                                    'epg_fuzzy_threshold', 0.85))
        min_len = int(getattr(settings,
                               'epg_fuzzy_min_length',
                               EPG_FUZZY_MIN_LENGTH_DEFAULT))
        min_gap = float(getattr(settings,
                                 'epg_fuzzy_min_gap',
                                 EPG_FUZZY_MIN_GAP_DEFAULT))
        return threshold, min_len, min_gap
    except Exception:
        return (1.0, EPG_FUZZY_MIN_LENGTH_DEFAULT,
                EPG_FUZZY_MIN_GAP_DEFAULT)


class EPGDatabase:
    def __init__(self, cache_manager: Optional['CacheManager'] = None):
        self._entries: Dict[str, List[EPGEntry]] = defaultdict(list)
        self._lock = threading.RLock()
        self.cache_manager = cache_manager

        self._channel_info: Dict[str, EPGChannelInfo] = {}
        self._channel_info_lock = threading.RLock()
        self._channel_info_settings_key: Optional[tuple] = None
        self._indexes_built: bool = False

        self._index_full: Dict[str, List[Tuple[str, int]]] = {}
        self._index_light: Dict[str, List[Tuple[str, int]]] = {}
        self._index_strict: Dict[str, List[Tuple[str, int]]] = {}

        self._source_priority: Dict[str, int] = {}
        self._source_priority_lock = threading.RLock()
        self._prefer_tvg_id: bool = True

        self._fuzzy_cache: Dict[Tuple[str, float, int, float],
                                  Optional[str]] = {}
        self._fuzzy_cache_lock = threading.RLock()

        self._token_cache: Dict[str, Set[str]] = {}
        self._token_cache_lock = threading.RLock()

    def clear(self):
        with self._lock:
            self._entries.clear()
        with self._channel_info_lock:
            self._channel_info.clear()
            self._index_full.clear()
            self._index_light.clear()
            self._index_strict.clear()
            self._channel_info_settings_key = None
            self._indexes_built = False
        with self._fuzzy_cache_lock:
            self._fuzzy_cache.clear()
        with self._token_cache_lock:
            self._token_cache.clear()

    def clear_fuzzy_cache(self):
        with self._fuzzy_cache_lock:
            self._fuzzy_cache.clear()

    def set_prefer_tvg_id(self, prefer: bool):
        self._prefer_tvg_id = bool(prefer)
        self.clear_fuzzy_cache()

    def _get_source_priority(self, url: str) -> int:
        with self._source_priority_lock:
            return self._source_priority.get(url, 0)

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            if self._entries:
                return True
        with self._channel_info_lock:
            return bool(self._channel_info)

    @property
    def has_channel_info(self) -> bool:
        with self._channel_info_lock:
            return bool(self._channel_info)

    @staticmethod
    def _parse_xmltv_time(s: str) -> Optional[datetime]:
        if not s:
            return None
        s = s.strip()
        m = re.match(r'^(\d{14})\s*([+-]\d{4})?$', s)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
                if m.group(2):
                    sign = 1 if m.group(2)[0] == '+' else -1
                    hh = int(m.group(2)[1:3])
                    mm = int(m.group(2)[3:5])
                    dt = dt - timedelta(hours=hh, minutes=mm) * sign
                return dt
            except ValueError:
                return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            return None

    @staticmethod
    def _format_time(dt: Optional[datetime]) -> str:
        if dt is None:
            return ""
        return dt.strftime("%Y%m%d%H%M%S") + " +0000"

    def load_from_xmltv(self, xml_text: str,
                        stop_token: Optional['_StopToken'] = None,
                        source: str = "") -> int:
        try:
            root = safe_fromstring(xml_text)
        except Exception as e:
            logger.error("XMLTV parse error (source=%s): %s",
                          source, e)
            return 0

        source_prio = self._get_source_priority(source)

        new_infos: List[EPGChannelInfo] = []
        for ch_el in root.findall('channel'):
            if cancelled(stop_token):
                break
            cid = (ch_el.get('id', '') or '').strip()
            if not cid or cid.lower().startswith('no_epg'):
                continue

            display_names: List[Tuple[str, str]] = []
            for dn in ch_el.findall('display-name'):
                txt = (dn.text or '').strip()
                if not txt:
                    continue
                lang = (dn.get('lang', '') or '').strip().lower()
                display_names.append((txt, lang))
            if not display_names:
                continue
            if all(is_stub_name(n) for n, _ in display_names):
                continue

            icon_el = ch_el.find('icon')
            icon = ((icon_el.get('src', '') or '')
                    if icon_el is not None else '')
            lcn_el = ch_el.find('lcn')
            lcn = ((lcn_el.text or '').strip()
                   if lcn_el is not None and lcn_el.text else '')

            info = EPGChannelInfo()
            info.channel_id = cid
            info.display_names = [n for n, _ in display_names]
            info.display_names_lang = dict(display_names)
            info.icon = icon
            info.lcn = lcn
            info.source = source
            info.all_sources = [source] if source else []
            info.priority = source_prio
            new_infos.append(info)

        with self._channel_info_lock:
            for info in new_infos:
                cid = info.channel_id
                existing = self._channel_info.get(cid)
                if existing is None:
                    self._channel_info[cid] = info
                    continue
                existing_names = set(existing.display_names)
                for n in info.display_names:
                    if n and n not in existing_names:
                        existing.display_names.append(n)
                        existing_names.add(n)
                        existing.display_names_lang[n] = \
                            info.display_names_lang.get(n, '')
                if source and source not in existing.all_sources:
                    existing.all_sources.append(source)
                if info.icon and not existing.icon:
                    existing.icon = info.icon
                if info.lcn and not existing.lcn:
                    existing.lcn = info.lcn
                if info.priority > existing.priority:
                    existing.priority = info.priority
                    existing.source = info.source

            for _cid, info in self._channel_info.items():
                if not info.display_names:
                    continue
                names_priority = {
                    n: info.priority for n in info.display_names}
                canon = pick_canonical_display_name(
                    info.display_names, info.display_names_lang,
                    source_lang='ru',
                    names_priority=names_priority)
                info.update_canonical(canon)
                info.alternative_names = pick_all_alternative_names(
                    info.display_names, canon,
                    max_alternatives=EPG_ALTERNATIVE_NAMES_MAX)

            self._channel_info_settings_key = None
            self._indexes_built = False

        count = 0
        to_cache: List[Dict[str, Any]] = []
        with self._lock:
            for prog in root.findall('programme'):
                if cancelled(stop_token):
                    break
                e = EPGEntry()
                e.channel_id = prog.get('channel', '')
                if not e.channel_id:
                    continue
                e.start = self._parse_xmltv_time(prog.get('start', ''))
                e.stop = self._parse_xmltv_time(prog.get('stop', ''))
                title_el = prog.find('title')
                if title_el is not None and title_el.text:
                    e.title = title_el.text.strip()
                desc_el = prog.find('desc')
                if desc_el is not None and desc_el.text:
                    e.desc = desc_el.text.strip()
                cat_el = prog.find('category')
                if cat_el is not None and cat_el.text:
                    e.category = cat_el.text.strip()
                self._entries[e.channel_id].append(e)
                count += 1
                to_cache.append({
                    'channel_id': e.channel_id,
                    'start': self._format_time(e.start),
                    'stop': self._format_time(e.stop),
                    'title': e.title, 'desc': e.desc,
                    'category': e.category})

        if self.cache_manager:
            if to_cache:
                self.cache_manager.save_epg_entries(to_cache, source)
            info_to_cache: List[Dict[str, Any]] = []
            with self._channel_info_lock:
                for info in self._channel_info.values():
                    info_to_cache.append({
                        'channel_id': info.channel_id,
                        'display_name': info.display_name,
                        'icon': info.icon,
                        'lcn': info.lcn,
                    })
            if info_to_cache:
                self.cache_manager.save_epg_channels(
                    info_to_cache, source)

        self.clear_fuzzy_cache()
        logger.info(
            "EPG: loaded %d programmes, %d channels (source=%s)",
            count, len(self._channel_info), source)
        return count

    def load_from_urls(self, urls: List[str],
                       timeout: int = EPG_SOURCE_TIMEOUT_SEC,
                       stop_token: Optional['_StopToken'] = None
                       ) -> Tuple[int, List[str]]:
        total = 0
        errors: List[str] = []
        session = _new_session()
        try:
            for idx, url in enumerate(urls):
                if cancelled(stop_token):
                    break
                if not url:
                    continue
                with self._source_priority_lock:
                    if url not in self._source_priority:
                        self._source_priority[url] = max(1, 10 - idx)
                try:
                    with session.get(url, timeout=timeout,
                                     verify=False,
                                     stream=True) as r:
                        if r.status_code != 200:
                            errors.append(
                                f"{url}: HTTP {r.status_code}")
                            continue
                        buf = bytearray()
                        truncated = False
                        for chunk in r.iter_content(
                                chunk_size=64 * 1024):
                            if cancelled(stop_token):
                                truncated = True
                                break
                            buf.extend(chunk)
                            if len(buf) > EPG_MAX_BYTES:
                                errors.append(
                                    f"{url}: size limit exceeded")
                                truncated = True
                                break
                        if truncated:
                            continue
                        content = bytes(buf)
                        if (url.endswith('.gz')
                                or content[:2] == b'\x1f\x8b'):
                            try:
                                content = gzip.decompress(content)
                            except Exception as e:
                                errors.append(f"{url}: gzip {e}")
                                continue
                        text = content.decode('utf-8',
                                              errors='replace')
                        cnt = self.load_from_xmltv(
                            text, stop_token, source=url)
                        total += cnt
                except Exception as e:
                    errors.append(f"{url}: {str(e)[:100]}")
        finally:
            try:
                session.close()
            except Exception:
                pass
        return total, errors

    def load_from_cache(self,
                        max_age_hours: int = EPG_CACHE_TTL_HOURS
                        ) -> int:
        if not self.cache_manager:
            return 0
        rows = self.cache_manager.load_epg_entries(max_age_hours)
        tmp: Dict[str, List[EPGEntry]] = defaultdict(list)
        for r in rows:
            e = EPGEntry()
            e.channel_id = r.get('channel_id', '')
            e.start = self._parse_xmltv_time(r.get('start', ''))
            e.stop = self._parse_xmltv_time(r.get('stop', ''))
            e.title = r.get('title', '')
            e.desc = r.get('desc', '')
            e.category = r.get('category', '')
            if e.channel_id:
                tmp[e.channel_id].append(e)
        with self._lock:
            self._entries.clear()
            self._entries.update(tmp)

        ch_rows = self.cache_manager.load_epg_channels(max_age_hours)
        with self._channel_info_lock:
            self._channel_info.clear()
            if ch_rows:
                for r in ch_rows:
                    cid = r.get('channel_id', '')
                    if not cid or cid.lower().startswith('no_epg'):
                        continue
                    info = EPGChannelInfo()
                    info.channel_id = cid
                    info.display_name = r.get('display_name', '') or ''
                    info.display_names = ([info.display_name]
                                           if info.display_name else [])
                    info.icon = r.get('icon', '') or ''
                    info.lcn = r.get('lcn', '') or ''
                    info.source = r.get('source', '') or ''
                    info.priority = self._get_source_priority(
                        info.source)
                    info.all_sources = ([info.source]
                                         if info.source else [])
                    self._channel_info[cid] = info
            self._channel_info_settings_key = None
            self._indexes_built = False

        self.clear_fuzzy_cache()
        return sum(len(v) for v in tmp.values())

    @staticmethod
    def _settings_key(settings) -> Optional[tuple]:
        """Отпечаток настроек, влияющих на нормализацию.

        Включает ВСЕ флаги, которые читает
        normalize_with_settings: remove_parentheses, remove_brackets,
        remove_emojis, ignore_special_chars, use_iptv_org_aliases.
        """
        if settings is None:
            return None
        try:
            return (
                bool(getattr(settings,
                             'remove_parentheses_in_names', True)),
                bool(getattr(settings,
                             'remove_brackets_in_names', True)),
                bool(getattr(settings, 'remove_emojis_in_names',
                             True)),
                bool(getattr(settings,
                             'ignore_special_chars_in_names', True)),
                bool(getattr(settings,
                             'use_iptv_org_aliases', True)),
            )
        except Exception:
            return None

    def _rebuild_indexes_locked(self, settings=None):
        self._index_full.clear()
        self._index_light.clear()
        self._index_strict.clear()
        for cid, info in self._channel_info.items():
            prio = info.priority
            names = list(info.display_names)
            if (info.display_name
                    and info.display_name not in names):
                names.append(info.display_name)
            for name in names:
                if not name:
                    continue
                for index, mode in (
                        (self._index_full, NormMode.FULL),
                        (self._index_light, NormMode.LIGHT),
                        (self._index_strict, NormMode.STRICT)):
                    norm = normalize_with_settings(
                        name, settings, mode)
                    if not norm:
                        continue
                    lst = index.get(norm)
                    if lst is None:
                        index[norm] = [(cid, prio)]
                    else:
                        lst.append((cid, prio))
        for index in (self._index_full, self._index_light,
                      self._index_strict):
            for key, lst in index.items():
                lst.sort(key=lambda x: -x[1])
        self._channel_info_settings_key = self._settings_key(
            settings)
        self._indexes_built = True

    def _ensure_indexes(self, settings) -> None:
        key = self._settings_key(settings)
        if (not self._indexes_built
                or key != self._channel_info_settings_key):
            self._rebuild_indexes_locked(settings)

    def find_channel_info(
            self, channel: ChannelData,
            fuzzy_threshold: float = 1.0,
            fuzzy_min_length: int = EPG_FUZZY_MIN_LENGTH_DEFAULT,
            fuzzy_min_gap: float = EPG_FUZZY_MIN_GAP_DEFAULT,
            settings: Any = None) -> Optional[EPGChannelInfo]:
        prefer_tvg_id = _prefer_tvg_id_from_settings(
            settings, self._prefer_tvg_id)

        with self._channel_info_lock:
            if not self._channel_info:
                return None
            self._ensure_indexes(settings)

            if prefer_tvg_id and channel.meta.tvg_id:
                tid = channel.meta.tvg_id.strip()
                if not is_stub_tvg_id(tid):
                    info = self._channel_info.get(tid)
                    if info:
                        return info

            field_candidates: List[str] = []
            if channel.meta.tvg_name:
                field_candidates.append(channel.meta.tvg_name)
            if channel.meta.name:
                field_candidates.append(channel.meta.name)
            # FIX: учитываем alternative_names из ранее
            # применённого EPG. Это позволяет находить канал
            # повторно, даже если его отображаемое имя уже
            # было переименовано в каноническое EPG-имя,
            # а tvg_id по какой-то причине пуст.
            for alt in (channel.meta.epg_alternative_names or []):
                if alt and alt not in field_candidates:
                    field_candidates.append(alt)

            for field in field_candidates:
                if not field:
                    continue
                for index, mode in (
                        (self._index_full, NormMode.FULL),
                        (self._index_light, NormMode.LIGHT),
                        (self._index_strict, NormMode.STRICT)):
                    norm = normalize_with_settings(
                        field, settings, mode)
                    if not norm:
                        continue
                    variants = {norm}
                    if ' ' in norm:
                        variants.add(norm.replace(' ', ''))
                    for v in variants:
                        lst = index.get(v)
                        if not lst:
                            continue
                        top_prio = lst[0][1]
                        top = [c for c, p in lst if p == top_prio]
                        if len(top) == 1:
                            return self._channel_info.get(top[0])

            if not prefer_tvg_id and channel.meta.tvg_id:
                tid = channel.meta.tvg_id.strip()
                if not is_stub_tvg_id(tid):
                    info = self._channel_info.get(tid)
                    if info:
                        return info

        if (fuzzy_threshold >= 1.0
                or not channel.meta.name
                or not channel.meta.name.strip()):
            return None
        return self._fuzzy_find(channel, fuzzy_threshold,
                                 fuzzy_min_length, fuzzy_min_gap,
                                 settings)

    def _fuzzy_find(
            self, channel: ChannelData,
            fuzzy_threshold: float, fuzzy_min_length: int,
            fuzzy_min_gap: float,
            settings: Any) -> Optional[EPGChannelInfo]:
        target_raw = channel.meta.name or ""
        target_norm = normalize_with_settings(
            target_raw, settings, NormMode.FULL)
        if not target_norm or len(target_norm) < fuzzy_min_length:
            return None

        cache_key = (target_norm, float(fuzzy_threshold),
                     int(fuzzy_min_length), float(fuzzy_min_gap))
        with self._fuzzy_cache_lock:
            if cache_key in self._fuzzy_cache:
                cid = self._fuzzy_cache[cache_key]
                if cid is None:
                    return None
                with self._channel_info_lock:
                    return self._channel_info.get(cid)

        target_tokens = self._tokens_for(target_norm)
        candidates: List[Tuple[str, str]] = []
        with self._channel_info_lock:
            if target_tokens:
                cand_cids: Set[str] = set()
                for name_key, lst in self._index_full.items():
                    if self._tokens_for(name_key) & target_tokens:
                        for cid, _ in lst:
                            cand_cids.add(cid)
                for cid in cand_cids:
                    info = self._channel_info.get(cid)
                    if info is None:
                        continue
                    for n in info.display_names:
                        norm = normalize_with_settings(
                            n, settings, NormMode.FULL)
                        if norm:
                            candidates.append((cid, norm))
            else:
                for cid, info in self._channel_info.items():
                    for n in info.display_names:
                        norm = normalize_with_settings(
                            n, settings, NormMode.FULL)
                        if norm:
                            candidates.append((cid, norm))

        scored: List[Tuple[float, str]] = []
        for cid, norm in candidates:
            if len(norm) < fuzzy_min_length:
                continue
            score = similarity(target_raw, norm, settings)
            if score >= fuzzy_threshold:
                scored.append((score, cid))

        best_cid: Optional[str] = None
        if scored:
            scored.sort(reverse=True)
            if (len(scored) == 1
                    or (scored[0][0] - scored[1][0])
                    >= fuzzy_min_gap):
                best_cid = scored[0][1]

        with self._fuzzy_cache_lock:
            if len(self._fuzzy_cache) >= EPG_FUZZY_CACHE_LIMIT:
                drop = max(1, len(self._fuzzy_cache) // 8)
                for _ in range(drop):
                    self._fuzzy_cache.pop(
                        next(iter(self._fuzzy_cache)), None)
            self._fuzzy_cache[cache_key] = best_cid

        if best_cid is None:
            return None
        with self._channel_info_lock:
            return self._channel_info.get(best_cid)

    def _tokens_for(self, text: str) -> Set[str]:
        if not text:
            return set()
        with self._token_cache_lock:
            cached = self._token_cache.get(text)
            if cached is not None:
                return cached
        tokens = set(re.findall(r'\w+', text.casefold()))
        with self._token_cache_lock:
            if len(self._token_cache) >= EPG_FUZZY_CACHE_LIMIT:
                drop = max(1, len(self._token_cache) // 8)
                for _ in range(drop):
                    self._token_cache.pop(
                        next(iter(self._token_cache)), None)
            self._token_cache[text] = tokens
        return tokens

    def get_current(self, tvg_id: str) -> Optional[EPGEntry]:
        if not tvg_id:
            return None
        now = _utcnow_naive()
        with self._lock:
            entries = list(self._entries.get(tvg_id, []))
        best: Optional[EPGEntry] = None
        for e in entries:
            if e.start and e.stop and e.start <= now <= e.stop:
                if best is None or e.start > best.start:
                    best = e
        return best

    def get_channel_source(self, cid: str) -> str:
        with self._channel_info_lock:
            info = self._channel_info.get(cid)
            return info.source if info else ""

    def build_epg_metadata_updates(
            self, channel: ChannelData,
            fuzzy_threshold: float = 1.0,
            fuzzy_min_length: int = EPG_FUZZY_MIN_LENGTH_DEFAULT,
            fuzzy_min_gap: float = EPG_FUZZY_MIN_GAP_DEFAULT,
            settings: Any = None) -> Tuple[List[Tuple[str, str]],
                                             str, List[str]]:
        info = self.find_channel_info(
            channel, fuzzy_threshold, fuzzy_min_length,
            fuzzy_min_gap, settings=settings)
        if info is None:
            return [], "", []
        updates: List[Tuple[str, str]] = []
        if info.channel_id:
            updates.append(('tvg_id', info.channel_id))
        if info.display_name:
            updates.append(('tvg_name', info.display_name))
        if info.icon:
            updates.append(('tvg_logo', info.icon))
        if info.lcn:
            updates.append(('tvg_chno', info.lcn))
        source = self.get_channel_source(info.channel_id)
        alternatives = list(info.alternative_names)
        filtered = [u for u in updates
                    if u[0] in EPG_ALLOWED_META_FIELDS]
        return filtered, source, alternatives

    # FIX: единая точка применения EPG к каналу.
    # Используется при создании плейлиста из источников и в
    # фоновом воркере — чтобы не дублировать логику.
    def apply_metadata_to_channel(
            self, channel: ChannelData,
            settings: Any = None,
            *,
            overwrite_metadata: bool = True,
            fuzzy_threshold: Optional[float] = None,
            fuzzy_min_length: int = EPG_FUZZY_MIN_LENGTH_DEFAULT,
            fuzzy_min_gap: float = EPG_FUZZY_MIN_GAP_DEFAULT,
            rename_channel: bool = True,
    ) -> bool:
        """Применяет EPG-метаданные к каналу.

        Возвращает True, если что-то изменилось.

        Параметр overwrite_metadata определяет, перезаписывать ли
        уже существующие поля (tvg_id, tvg_name, tvg_logo,
        tvg_chno). По умолчанию — да (соответствует
        epg_overwrite_metadata=True).
        """
        if fuzzy_threshold is None:
            fuzzy_threshold, _ml, _mg = \
                _epg_fuzzy_params_from_settings(settings)
            if fuzzy_min_length == EPG_FUZZY_MIN_LENGTH_DEFAULT:
                fuzzy_min_length = _ml
            if fuzzy_min_gap == EPG_FUZZY_MIN_GAP_DEFAULT:
                fuzzy_min_gap = _mg

        info = self.find_channel_info(
            channel, fuzzy_threshold, fuzzy_min_length,
            fuzzy_min_gap, settings=settings)
        if info is None:
            return False

        changed = False
        # tvg_id
        if info.channel_id:
            cur = (channel.meta.tvg_id or "").strip()
            if (not cur or is_stub_tvg_id(cur)
                    or overwrite_metadata):
                if cur != info.channel_id:
                    channel.meta.tvg_id = info.channel_id
                    changed = True
        # tvg_name
        if info.display_name:
            cur = channel.meta.tvg_name or ""
            if (not cur or overwrite_metadata):
                if cur != info.display_name:
                    channel.meta.tvg_name = info.display_name
                    changed = True
        # tvg_logo
        if info.icon:
            cur = channel.meta.tvg_logo or ""
            if not cur or overwrite_metadata:
                if cur != info.icon:
                    channel.meta.tvg_logo = info.icon
                    changed = True
        # tvg_chno
        if info.lcn:
            cur = channel.meta.tvg_chno or ""
            if not cur or overwrite_metadata:
                if cur != info.lcn:
                    channel.meta.tvg_chno = info.lcn
                    changed = True
        # epg_source
        src = self.get_channel_source(info.channel_id)
        if src and channel.meta.epg_source != src:
            channel.meta.epg_source = src
            changed = True
        # epg_alternative_names
        alts = list(info.alternative_names)
        if alts and channel.meta.epg_alternative_names != alts:
            channel.meta.epg_alternative_names = alts
            changed = True
        # Переименование канала в каноническое EPG-имя
        if rename_channel and info.display_name:
            if channel.meta.name != info.display_name:
                channel.meta.name = info.display_name
                changed = True
        if changed:
            try:
                channel.update_extinf()
            except Exception:
                logger.exception("update_extinf after EPG")
            channel.touch()
        return changed