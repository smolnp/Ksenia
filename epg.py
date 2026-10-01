# -*- coding: utf-8 -*-
"""EPGDatabase."""

from __future__ import annotations
import re
import gzip
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict
from difflib import SequenceMatcher
from xml.etree import ElementTree as ET
from constants import (EPG_MAX_BYTES, EPG_SOURCE_TIMEOUT_SEC,
    EPG_CACHE_TTL_HOURS, EPG_FUZZY_CACHE_LIMIT,
    EPG_FUZZY_MIN_LENGTH_DEFAULT, EPG_FUZZY_MIN_GAP_DEFAULT,
    EPG_ALLOWED_META_FIELDS)
from models import ChannelData, EPGEntry, EPGChannelInfo
from paths import logger
from utils import ChannelNameNormalizer, _StopToken, cancelled
from sources import HttpSessionFactory

class EPGDatabase:
    def __init__(self, cache_manager: Optional['CacheManager'] = None):
        self._entries: Dict[str, List[EPGEntry]] = defaultdict(list)
        self._lock = threading.RLock()
        self._sources: List[str] = []
        self.cache_manager = cache_manager

        self._channel_info: Dict[str, EPGChannelInfo] = {}
        self._channel_info_norm: Dict[str, str] = {}
        self._channel_info_lock = threading.RLock()
        self._fuzzy_cache: Dict[str, Optional[str]] = {}
        self._fuzzy_cache_lock = threading.RLock()

        self._channel_info_by_token: Dict[str, Set[str]] = defaultdict(set)
        self._token_cache: Dict[str, Set[str]] = {}
        self._token_cache_lock = threading.RLock()

        self._source_priority: Dict[str, int] = {}
        self._source_priority_lock = threading.RLock()
        self._channel_info_source: Dict[str, str] = {}
        self._channel_info_priority: Dict[str, int] = {}

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._sources = []
        with self._channel_info_lock:
            self._channel_info.clear()
            self._channel_info_norm.clear()
            self._channel_info_source.clear()
            self._channel_info_priority.clear()
        with self._fuzzy_cache_lock:
            self._fuzzy_cache.clear()
        with self._token_cache_lock:
            self._channel_info_by_token.clear()
            self._token_cache.clear()

    def set_source_priority(self, url: str, priority: int):
        with self._source_priority_lock:
            self._source_priority[url] = int(priority)

    def _get_source_priority(self, url: str) -> int:
        with self._source_priority_lock:
            return self._source_priority.get(url, 0)

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return bool(self._entries) or bool(self._channel_info)

    @property
    def has_channel_info(self) -> bool:
        with self._channel_info_lock:
            return bool(self._channel_info)

    def _tokens_for(self, text: str) -> Set[str]:
        if not text:
            return set()
        with self._token_cache_lock:
            cached = self._token_cache.get(text)
            if cached is not None:
                return cached
        tokens = ChannelNameNormalizer.token_set(text)
        with self._token_cache_lock:
            if len(self._token_cache) >= EPG_FUZZY_CACHE_LIMIT:
                drop = max(1, len(self._token_cache) // 8)
                for _ in range(drop):
                    self._token_cache.pop(
                        next(iter(self._token_cache)), None)
            self._token_cache[text] = tokens
        return tokens

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
                    offset = timedelta(hours=hh, minutes=mm) * sign
                    dt = dt - offset
                return dt
            except ValueError:
                return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    @staticmethod
    def _format_time(dt: Optional[datetime]) -> str:
        if dt is None:
            return ""
        return dt.strftime("%Y%m%d%H%M%S")

    def load_from_xmltv(self, xml_text: str,
                        stop_token: Optional['_StopToken'] = None,
                        source: str = "") -> int:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error(f"XMLTV parse error: {e}")
            return 0

        source_prio = self._get_source_priority(source)
        info_to_cache: List[Dict[str, Any]] = []
        with self._channel_info_lock:
            for ch_el in root.findall('channel'):
                if cancelled(stop_token):
                    break
                cid = ch_el.get('id', '')
                if not cid:
                    continue
                existing_prio = self._channel_info_priority.get(cid, -1)
                if cid in self._channel_info and existing_prio > source_prio:
                    continue
                info = EPGChannelInfo()
                info.channel_id = cid
                dn = ch_el.find('display-name')
                if dn is not None and dn.text:
                    info.display_name = dn.text.strip()
                icon = ch_el.find('icon')
                if icon is not None:
                    info.icon = icon.get('src', '') or ''
                lcn = ch_el.find('lcn')
                if lcn is not None and lcn.text:
                    info.lcn = lcn.text.strip()
                self._channel_info[cid] = info
                self._channel_info_source[cid] = source
                self._channel_info_priority[cid] = source_prio
                norm_id = ChannelNameNormalizer.normalize(cid)
                if norm_id and norm_id not in self._channel_info_norm:
                    self._channel_info_norm[norm_id] = cid
                norm_dn = ""
                if info.display_name:
                    norm_dn = ChannelNameNormalizer.normalize(info.display_name)
                    if norm_dn and norm_dn not in self._channel_info_norm:
                        self._channel_info_norm[norm_dn] = cid
                for token in self._tokens_for(norm_id) | self._tokens_for(norm_dn):
                    self._channel_info_by_token[token].add(cid)
                info_to_cache.append({
                    'channel_id': cid,
                    'display_name': info.display_name,
                    'icon': info.icon,
                    'lcn': info.lcn,
                })

        count = 0
        to_cache: List[Dict[str, Any]] = []
        with self._lock:
            for prog in root.findall('programme'):
                if cancelled(stop_token):
                    break
                e = EPGEntry()
                e.channel_id = prog.get('channel', '')
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
                if e.channel_id:
                    self._entries[e.channel_id].append(e)
                    count += 1
                    to_cache.append({
                        'channel_id': e.channel_id,
                        'start': self._format_time(e.start),
                        'stop': self._format_time(e.stop),
                        'title': e.title,
                        'desc': e.desc,
                        'category': e.category,
                    })
        if self.cache_manager:
            if to_cache:
                self.cache_manager.save_epg_entries(to_cache, source)
            if info_to_cache:
                self.cache_manager.save_epg_channels(info_to_cache, source)
        return count

    def load_from_urls(self, urls: List[str], timeout: int = EPG_SOURCE_TIMEOUT_SEC,
                       stop_token: Optional['_StopToken'] = None
                       ) -> Tuple[int, List[str]]:
        total = 0
        errors: List[str] = []
        session = HttpSessionFactory.get(verify_ssl=False)
        for url in urls:
            if cancelled(stop_token):
                break
            if not url:
                continue
            try:
                with session.get(url, timeout=timeout, verify=False,
                                 stream=True) as r:
                    if r.status_code != 200:
                        errors.append(f"{url}: HTTP {r.status_code}")
                        continue
                    buf = bytearray()
                    truncated = False
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if cancelled(stop_token):
                            truncated = True
                            break
                        buf.extend(chunk)
                        if len(buf) > EPG_MAX_BYTES:
                            errors.append(f"{url}: превышен лимит")
                            truncated = True
                            break
                    if truncated:
                        continue
                    content = bytes(buf)
                    if url.endswith('.gz') or content[:2] == b'\x1f\x8b':
                        try:
                            content = gzip.decompress(content)
                        except Exception as e:
                            errors.append(f"{url}: gzip {e}")
                            continue
                    text = content.decode('utf-8', errors='replace')
                    cnt = self.load_from_xmltv(text, stop_token, source=url)
                    total += cnt
                    if cnt:
                        with self._lock:
                            if url not in self._sources:
                                self._sources.append(url)
            except Exception as e:
                errors.append(f"{url}: {str(e)[:100]}")
        return total, errors

    def load_from_cache(self, max_age_hours: int = EPG_CACHE_TTL_HOURS) -> int:
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
        if ch_rows:
            with self._channel_info_lock:
                self._channel_info.clear()
                self._channel_info_norm.clear()
                self._channel_info_source.clear()
                self._channel_info_priority.clear()
                self._channel_info_by_token.clear()
                for r in ch_rows:
                    cid = r.get('channel_id', '')
                    if not cid:
                        continue
                    info = EPGChannelInfo()
                    info.channel_id = cid
                    info.display_name = r.get('display_name', '') or ''
                    info.icon = r.get('icon', '') or ''
                    info.lcn = r.get('lcn', '') or ''
                    src = r.get('source', '') or ''
                    self._channel_info[cid] = info
                    self._channel_info_source[cid] = src
                    self._channel_info_priority[cid] = self._get_source_priority(src)
                    norm_id = ChannelNameNormalizer.normalize(cid)
                    if norm_id and norm_id not in self._channel_info_norm:
                        self._channel_info_norm[norm_id] = cid
                    norm_dn = ""
                    if info.display_name:
                        norm_dn = ChannelNameNormalizer.normalize(info.display_name)
                        if norm_dn and norm_dn not in self._channel_info_norm:
                            self._channel_info_norm[norm_dn] = cid
                    for token in (self._tokens_for(norm_id) |
                                  self._tokens_for(norm_dn)):
                        self._channel_info_by_token[token].add(cid)
        return sum(len(v) for v in tmp.values())

    def get_current(self, tvg_id: str) -> Optional[EPGEntry]:
        if not tvg_id:
            return None
        now = datetime.now()
        with self._lock:
            entries = list(self._entries.get(tvg_id, []))
        best: Optional[EPGEntry] = None
        for e in entries:
            if e.start and e.stop and e.start <= now <= e.stop:
                if best is None or e.start > best.start:
                    best = e
        return best

    def find_channel_info(self, channel: ChannelData,
                          fuzzy_threshold: float = 1.0,
                          fuzzy_min_length: int = EPG_FUZZY_MIN_LENGTH_DEFAULT,
                          fuzzy_min_gap: float = EPG_FUZZY_MIN_GAP_DEFAULT
                          ) -> Optional[EPGChannelInfo]:
        with self._channel_info_lock:
            if not self._channel_info:
                return None

            if channel.meta.tvg_id:
                info = self._channel_info.get(channel.meta.tvg_id)
                if info:
                    return info

            if channel.meta.tvg_name:
                norm = ChannelNameNormalizer.normalize(channel.meta.tvg_name)
                if norm:
                    cid = self._channel_info_norm.get(norm)
                    if cid:
                        info = self._channel_info.get(cid)
                        if info:
                            return info

            if channel.meta.name:
                norm = ChannelNameNormalizer.normalize(channel.meta.name)
                if norm:
                    cid = self._channel_info_norm.get(norm)
                    if cid:
                        info = self._channel_info.get(cid)
                        if info:
                            return info

            if (fuzzy_threshold >= 1.0 or not channel.meta.name
                    or not channel.meta.name.strip()):
                return None

            target = ChannelNameNormalizer.normalize(channel.meta.name)
            if not target or len(target) < fuzzy_min_length:
                return None

            cache_key = (target, float(fuzzy_threshold),
                         int(fuzzy_min_length), float(fuzzy_min_gap))
            with self._fuzzy_cache_lock:
                if cache_key in self._fuzzy_cache:
                    cached_cid = self._fuzzy_cache[cache_key]
                    return self._channel_info.get(cached_cid) if cached_cid else None

            target_tokens = self._tokens_for(target)
            if target_tokens:
                cid_set: Set[str] = set()
                with self._channel_info_lock:
                    for t in target_tokens:
                        cid_set |= self._channel_info_by_token.get(t, set())
                    snapshot = [(c, (self._channel_info[c].display_name or c))
                                for c in cid_set if c in self._channel_info]
            else:
                with self._channel_info_lock:
                    snapshot = [(cid, norm)
                                for norm, cid in self._channel_info_norm.items()]

            candidates: List[Tuple[float, str]] = []
            for cid, norm_key in snapshot:
                if not norm_key or len(norm_key) < fuzzy_min_length:
                    continue
                score = SequenceMatcher(None, target, norm_key).ratio()
                if score >= fuzzy_threshold:
                    candidates.append((score, cid))

            best_cid: Optional[str] = None
            if candidates:
                candidates.sort(reverse=True)
                if len(candidates) == 1 or \
                        (candidates[0][0] - candidates[1][0]) >= fuzzy_min_gap:
                    best_cid = candidates[0][1]
                    logger.info(
                        f"EPG fuzzy match: '{channel.meta.name}' -> "
                        f"cid={best_cid} score={candidates[0][0]:.3f}")

            with self._fuzzy_cache_lock:
                if len(self._fuzzy_cache) >= EPG_FUZZY_CACHE_LIMIT:
                    drop = max(1, len(self._fuzzy_cache) // 8)
                    for _ in range(drop):
                        self._fuzzy_cache.pop(
                            next(iter(self._fuzzy_cache)), None)
                self._fuzzy_cache[cache_key] = best_cid

            return self._channel_info.get(best_cid) if best_cid else None

    def get_channel_source(self, cid: str) -> str:
        with self._channel_info_lock:
            return self._channel_info_source.get(cid, "")

    def build_epg_metadata_updates(self, channel: ChannelData,
                                   fuzzy_threshold: float = 1.0,
                                   fuzzy_min_length: int = EPG_FUZZY_MIN_LENGTH_DEFAULT,
                                   fuzzy_min_gap: float = EPG_FUZZY_MIN_GAP_DEFAULT
                                   ) -> Tuple[List[Tuple[str, str]], str]:
        info = self.find_channel_info(
            channel, fuzzy_threshold, fuzzy_min_length, fuzzy_min_gap)
        if info is None:
            return [], ""
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
        return [u for u in updates if u[0] in EPG_ALLOWED_META_FIELDS], source
