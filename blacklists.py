# -*- coding: utf-8 -*-
"""BlacklistManager, DomainBlacklistManager, DomainUserAgentManager."""

from __future__ import annotations
import os
import re
import threading
import ipaddress
from urllib.parse import urlparse
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from collections import OrderedDict, defaultdict
from constants import DOMAIN_BL_CACHE_MAX, DOMAIN_UA_CACHE_MAX, StatusText
from models import ChannelData
from paths import logger, parse_datetime
from storage import BaseJsonStore
from utils import URLUtils

class BlacklistManager:
    def __init__(self, config_dir: str):
        self._store = BaseJsonStore(
            os.path.join(config_dir, "blacklist.json"), [])
        self._lock = threading.RLock()

    def add_channel(self, name: str, tvg_id: str = "") -> bool:
        if not name and not tvg_id:
            return False
        with self._lock:
            for it in self._store._data:
                if (it.get('name', '').lower() == name.lower() and
                        it.get('tvg_id', '').lower() == tvg_id.lower()):
                    return False
            self._store._data.append({
                'name': name, 'tvg_id': tvg_id,
                'added_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return self._store.save()

    def remove_channel(self, name: str, tvg_id: str = "") -> bool:
        with self._lock:
            for i, it in enumerate(self._store._data):
                if (it.get('name', '').lower() == name.lower() and
                        it.get('tvg_id', '').lower() == tvg_id.lower()):
                    del self._store._data[i]
                    return self._store.save()
        return False

    def get_all(self) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._store._data)

    def clear(self):
        with self._lock:
            self._store._data.clear()
            self._store.save()

    def filter_channels(self, channels: List[ChannelData]
                        ) -> Tuple[List[ChannelData], int]:
        with self._lock:
            bl = list(self._store._data)
        if not bl:
            return list(channels), 0
        bl_names: Set[str] = set()
        bl_tvgs: Set[str] = set()
        for bi in bl:
            n = bi.get('name', '').strip().lower()
            t = bi.get('tvg_id', '').strip().lower()
            if n:
                bl_names.add(n)
            if t:
                bl_tvgs.add(t)
        if not bl_names and not bl_tvgs:
            return list(channels), 0
        filtered, removed = [], 0
        for ch in channels:
            name_low = (ch.meta.name or '').strip().lower()
            tvg_low = (ch.meta.tvg_id or '').strip().lower()
            if (tvg_low and tvg_low in bl_tvgs) or \
               (name_low and name_low in bl_names):
                removed += 1
            else:
                filtered.append(ch)
        return filtered, removed

class DomainBlacklistRule:
    __slots__ = ('value', 'is_ip', 'include_subdomains', 'note', 'added_date')

    def __init__(self, value: str = "", include_subdomains: bool = True,
                 note: str = ""):
        self.value: str = URLUtils.normalize_host(value)
        self.is_ip: bool = URLUtils.is_ip_address(self.value)
        self.include_subdomains: bool = bool(include_subdomains)
        self.note: str = (note or "").strip()
        self.added_date: datetime = datetime.now()

    def matches(self, url: str) -> bool:
        if not url or not self.value:
            return False
        if self.is_ip:
            return URLUtils.ip_matches(url, self.value)
        return URLUtils.host_matches(
            url, self.value, include_subdomains=self.include_subdomains)

    def matches_host(self, host: str) -> bool:
        if not host or not self.value:
            return False
        if self.is_ip:
            try:
                return ipaddress.ip_address(host) == ipaddress.ip_address(self.value)
            except ValueError:
                return False
        if host == self.value:
            return True
        if self.include_subdomains and host.endswith('.' + self.value):
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'value': self.value,
            'is_ip': self.is_ip,
            'include_subdomains': self.include_subdomains,
            'note': self.note,
            'added_date': self.added_date.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DomainBlacklistRule':
        r = cls()
        raw = data.get('value', '') or ''
        r.value = URLUtils.normalize_host(raw) or raw.strip().lower()
        r.is_ip = bool(data.get('is_ip', URLUtils.is_ip_address(r.value)))
        r.include_subdomains = bool(data.get('include_subdomains', True))
        r.note = (data.get('note', '') or '').strip()
        dt = parse_datetime(data.get('added_date'))
        if dt is not None:
            r.added_date = dt
        return r

class DomainBlacklistManager:
    """
    ЧС домен/IP.
      - filter_channels()  — УДАЛЯЕТ каналы (legacy, для CLI).
      - clean_channels()   — ОЧИЩАЕТ ссылки, сохраняет каналы (UI).
    """

    def __init__(self, config_dir: str):
        self._store = BaseJsonStore(
            os.path.join(config_dir, "domain_blacklist.json"), [])
        self._lock = threading.RLock()
        self._rules: List[DomainBlacklistRule] = [
            DomainBlacklistRule.from_dict(x) for x in self._store._data
            if isinstance(x, dict)
        ]
        self._cache: "OrderedDict[str, bool]" = OrderedDict()

    def _persist_locked(self) -> bool:
        snapshot = [r.to_dict() for r in self._rules]
        self._store._data = snapshot
        ok = self._store.save()
        if not ok:
            # Пытаемся восстановить из уже загруженного _store._data
            # (там лежит последняя успешно сохранённая версия).
            logger.error("DomainBlacklist persist failed")
        return ok

    def _invalidate_cache_locked(self):
        self._cache.clear()

    def _cache_get_locked(self, host: str) -> Optional[bool]:
        if host in self._cache:
            self._cache.move_to_end(host)
            return self._cache[host]
        return None

    def _cache_put_locked(self, host: str, blocked: bool):
        self._cache[host] = blocked
        self._cache.move_to_end(host)
        while len(self._cache) > DOMAIN_BL_CACHE_MAX:
            self._cache.popitem(last=False)

    def add_domain(self, value: str, include_subdomains: bool = True,
                   note: str = "") -> bool:
        normalized = URLUtils.normalize_host(value)
        if not normalized:
            return False
        with self._lock:
            for r in self._rules:
                if r.value == normalized:
                    changed = False
                    if r.include_subdomains != include_subdomains:
                        r.include_subdomains = bool(include_subdomains)
                        changed = True
                    if note and r.note != note:
                        r.note = note.strip()
                        changed = True
                    if not changed:
                        return False
                    self._invalidate_cache_locked()
                    return self._persist_locked()
            self._rules.append(DomainBlacklistRule(
                normalized, include_subdomains, note))
            self._invalidate_cache_locked()
            return self._persist_locked()

    def remove_domain(self, value: str) -> bool:
        normalized = URLUtils.normalize_host(value)
        if not normalized:
            return False
        with self._lock:
            for i, r in enumerate(self._rules):
                if r.value == normalized:
                    del self._rules[i]
                    self._invalidate_cache_locked()
                    return self._persist_locked()
        return False

    def set_include_subdomains(self, value: str, include: bool) -> bool:
        normalized = URLUtils.normalize_host(value)
        if not normalized:
            return False
        with self._lock:
            for r in self._rules:
                if r.value == normalized:
                    if r.include_subdomains == include:
                        return False
                    r.include_subdomains = bool(include)
                    self._invalidate_cache_locked()
                    return self._persist_locked()
        return False

    def get_all(self) -> List[DomainBlacklistRule]:
        with self._lock:
            return list(self._rules)

    def get_all_dicts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._rules]

    def clear(self):
        with self._lock:
            self._rules.clear()
            self._invalidate_cache_locked()
            self._store._data = []
            self._store.save()

    def get_rule(self, value: str) -> Optional[DomainBlacklistRule]:
        normalized = URLUtils.normalize_host(value)
        if not normalized:
            return None
        with self._lock:
            for r in self._rules:
                if r.value == normalized:
                    return r
        return None

    def find_rule_for_url(self, url: str) -> Optional[DomainBlacklistRule]:
        if not url:
            return None
        with self._lock:
            rules = list(self._rules)
        for r in rules:
            if r.matches(url):
                return r
        return None

    def _matches_host(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            cached = self._cache_get_locked(host)
            if cached is not None:
                return cached
            # Кэш-промах — считаем под тем же lock (правила неизменны)
            blocked = False
            for r in self._rules:
                if r.matches_host(host):
                    blocked = True
                    break
            self._cache_put_locked(host, blocked)
            return blocked

    def matches_url(self, url: str) -> bool:
        if not url:
            return False
        host = URLUtils.extract_host(url)
        if not host:
            return False
        return self._matches_host(host)

    def filter_channels(self, channels: List[ChannelData]
                        ) -> Tuple[List[ChannelData], int]:
        """LEGACY: удаляет каналы."""
        with self._lock:
            if not self._rules:
                return list(channels), 0
            hosts: Set[str] = set()
            for ch in channels:
                if ch.link.url:
                    h = URLUtils.extract_host(ch.link.url)
                    if h:
                        hosts.add(h)
            for h in hosts:
                self._matches_host(h)

        filtered: List[ChannelData] = []
        removed = 0
        for ch in channels:
            if ch.link.url and self.matches_url(ch.link.url):
                removed += 1
            else:
                filtered.append(ch)
        return filtered, removed

    def clean_channels(self, channels: List[ChannelData]
                       ) -> Tuple[List[ChannelData], int]:
        """
        ОЧИЩАЕТ ссылку, сохраняет канал.
        v0.9.4: помечает статус «🚫 Заблокирован ЧС домен/IP».
        """
        with self._lock:
            if not self._rules:
                return list(channels), 0
            rules_snapshot = list(self._rules)

        cleaned = 0
        for ch in channels:
            if not ch.link.url:
                continue
            host = URLUtils.extract_host(ch.link.url)
            if not host:
                continue
            blocked = False
            for r in rules_snapshot:
                if r.matches_host(host):
                    blocked = True
                    break
            if blocked:
                ch.link.alternative_urls = [
                    u for u in ch.link.alternative_urls
                    if u and not self.matches_url(u)
                ]
                ch.clear_url()
                
                ch.status.status_text = StatusText.BLOCKED_BY_DOMAIN
                ch.update_extinf()
                object.__setattr__(ch, 'modified_date', datetime.now())
                cleaned += 1
        return list(channels), cleaned

    def import_dicts(self, items: List[Dict[str, Any]]) -> int:
        if not isinstance(items, list):
            return 0
        processed = 0
        with self._lock:
            for it in items:
                if not isinstance(it, dict):
                    continue
                raw = it.get('value', '') or it.get('domain', '') or ''
                if not raw:
                    continue
                normalized = URLUtils.normalize_host(raw)
                if not normalized:
                    continue
                inc = bool(it.get('include_subdomains', True))
                note = (it.get('note', '') or '').strip()
                found = False
                for r in self._rules:
                    if r.value == normalized:
                        r.include_subdomains = inc
                        if note:
                            r.note = note
                        found = True
                        processed += 1
                        break
                if not found:
                    self._rules.append(
                        DomainBlacklistRule(normalized, inc, note))
                    processed += 1
            self._invalidate_cache_locked()
            self._persist_locked()
        return processed

class DomainUserAgentRule:
    __slots__ = ('domain', 'user_agent', 'enabled', 'created_date')

    def __init__(self, domain: str = "", user_agent: str = ""):
        self.domain: str = domain.strip().lower()
        self.user_agent: str = self._sanitize_ua(user_agent)
        self.enabled: bool = True
        self.created_date: datetime = datetime.now()

    @staticmethod
    def _sanitize_ua(ua: str) -> str:
        if not ua:
            return ""
        return re.sub(r'[\r\n\t\0]+', ' ', ua).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'domain': self.domain, 'user_agent': self.user_agent,
            'enabled': self.enabled,
            'created_date': self.created_date.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DomainUserAgentRule':
        r = cls()
        r.domain = (data.get('domain', '') or '').lower()
        r.user_agent = cls._sanitize_ua(data.get('user_agent', '') or '')
        r.enabled = bool(data.get('enabled', True))
        dt = parse_datetime(data.get('created_date'))
        if dt is not None:
            r.created_date = dt
        return r

class DomainUserAgentManager:
    def __init__(self, config_dir: str):
        self._store = BaseJsonStore(
            os.path.join(config_dir, "domain_user_agent_rules.json"), [])
        self._lock = threading.RLock()
        self._rules: List[DomainUserAgentRule] = [
            DomainUserAgentRule.from_dict(x) for x in self._store._data
            if isinstance(x, dict)
        ]
        self._cache: "OrderedDict[str, Tuple[bool, Optional[str]]]" = OrderedDict()

    def _persist_locked(self) -> bool:
        self._store._data = [r.to_dict() for r in self._rules]
        return self._store.save()

    def _invalidate_cache_locked(self):
        self._cache.clear()

    def _cache_get_locked(self, host: str):
        if host in self._cache:
            self._cache.move_to_end(host)
            return True, self._cache[host]
        return False, None

    def _cache_put_locked(self, host: str, matched: bool, ua):
        self._cache[host] = (bool(matched), ua)
        self._cache.move_to_end(host)
        while len(self._cache) > DOMAIN_UA_CACHE_MAX:
            self._cache.popitem(last=False)

    def add_rule(self, domain: str, user_agent: str) -> bool:
        if not domain:
            return False
        domain = domain.lower()
        ua = DomainUserAgentRule._sanitize_ua(user_agent)
        with self._lock:
            for r in self._rules:
                if r.domain == domain:
                    r.user_agent = ua
                    break
            else:
                self._rules.append(DomainUserAgentRule(domain, ua))
            self._invalidate_cache_locked()
            return self._persist_locked()

    def remove_rule(self, domain: str) -> bool:
        domain = domain.lower()
        with self._lock:
            for i, r in enumerate(self._rules):
                if r.domain == domain:
                    del self._rules[i]
                    self._invalidate_cache_locked()
                    return self._persist_locked()
        return False

    def set_enabled(self, domain: str, enabled: bool) -> bool:
        domain = domain.lower()
        with self._lock:
            for r in self._rules:
                if r.domain == domain:
                    if r.enabled == enabled:
                        return False
                    r.enabled = enabled
                    self._invalidate_cache_locked()
                    return self._persist_locked()
        return False

    def get_rule(self, domain: str) -> Optional[DomainUserAgentRule]:
        domain = domain.lower()
        with self._lock:
            for r in self._rules:
                if r.domain == domain:
                    return r
        return None

    def get_all_rules(self) -> List[DomainUserAgentRule]:
        with self._lock:
            return list(self._rules)

    def _find_ua_for_host(self, host: str) -> Tuple[bool, Optional[str]]:
        if not host:
            return False, None
        host = host.lower()
        with self._lock:
            found, cached = self._cache_get_locked(host)
            if found:
                return cached
            for r in self._rules:
                if r.enabled and (host == r.domain or host.endswith('.' + r.domain)):
                    ua = r.user_agent or None
                    self._cache_put_locked(host, True, ua)
                    return True, ua
            self._cache_put_locked(host, False, None)
            return False, None

    def get_user_agent_for_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            host = urlparse(url).hostname
        except Exception:
            return None
        matched, ua = self._find_ua_for_host(host)
        if not matched:
            return None
        return ua

    def should_remove_user_agent(self, url: str) -> bool:
        if not url:
            return False
        try:
            host = urlparse(url).hostname
        except Exception:
            return False
        matched, ua = self._find_ua_for_host(host)
        if not matched:
            return False
        return ua is None

    def apply_rules_to_channel(self, channel: ChannelData) -> bool:
        if not channel.link.url:
            return False
        if self.should_remove_user_agent(channel.link.url):
            if channel.link.user_agent:
                channel.link.user_agent = ""
                for k in list(channel.link.extra_headers.keys()):
                    if k.lower() == 'user-agent':
                        del channel.link.extra_headers[k]
                channel.update_extvlcopt_from_headers()
                return True
            return False
        new_ua = self.get_user_agent_for_url(channel.link.url)
        if new_ua is not None and new_ua != channel.link.user_agent:
            channel.link.user_agent = new_ua
            channel.link.extra_headers['User-Agent'] = new_ua
            channel.update_extvlcopt_from_headers()
            object.__setattr__(channel, 'modified_date', datetime.now())
            return True
        return False

    def apply_rules_to_channels(self, channels: Iterable[ChannelData]) -> int:
        return sum(1 for ch in channels if self.apply_rules_to_channel(ch))
