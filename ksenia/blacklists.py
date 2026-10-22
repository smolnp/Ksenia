# -*- coding: utf-8 -*-
"""Чёрные списки и правила UA."""
from __future__ import annotations

import ipaddress
import os
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from constants import DOMAIN_BL_CACHE_MAX, DOMAIN_UA_CACHE_MAX, StatusText
from logging_setup import logger
from models import ChannelData
from paths import parse_datetime
from storage import BaseJsonStore
from utils import URLUtils, sanitize_user_agent


class BlacklistIndex(NamedTuple):
    names: Set[str]
    tvg_ids: Set[str]

    def matches(self, name: str, tvg_id: str) -> bool:
        n = (name or '').strip().lower()
        t = (tvg_id or '').strip().lower()
        return (n and n in self.names) or (t and t in self.tvg_ids)


class BlacklistManager:
    def __init__(self, config_dir: str):
        self._store = BaseJsonStore(
            os.path.join(config_dir, "blacklist.json"), [])
        self._lock = threading.RLock()
        self._index: Optional[BlacklistIndex] = None

    def build_index(self) -> BlacklistIndex:
        with self._lock:
            if self._index is not None:
                return self._index
            names: Set[str] = set()
            tvg_ids: Set[str] = set()
            for it in self._store._data:
                n = (it.get('name') or '').strip().lower()
                t = (it.get('tvg_id') or '').strip().lower()
                if n:
                    names.add(n)
                if t:
                    tvg_ids.add(t)
            self._index = BlacklistIndex(names, tvg_ids)
            return self._index

    def _invalidate_index(self):
        self._index = None

    def add_channel(self, name: str, tvg_id: str = "") -> bool:
        if not name and not tvg_id:
            return False
        with self._lock:
            for it in self._store._data:
                if (it.get('name', '').lower() == name.lower()
                        and it.get('tvg_id', '').lower()
                        == tvg_id.lower()):
                    return False
            self._store._data.append({
                'name': name, 'tvg_id': tvg_id,
                'added_date': datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"),
            })
            self._invalidate_index()
            return self._store.save()

    def remove_channel(self, name: str, tvg_id: str = "") -> bool:
        with self._lock:
            for i, it in enumerate(self._store._data):
                if ((it.get('name') or '').lower()
                        == (name or '').lower()
                        and (it.get('tvg_id') or '').lower()
                        == (tvg_id or '').lower()):
                    del self._store._data[i]
                    self._invalidate_index()
                    return self._store.save()
        return False

    def get_all(self) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._store._data)

    def clear(self):
        with self._lock:
            self._store._data.clear()
            self._invalidate_index()
            self._store.save()

    def matches(self, name: str, tvg_id: str = "") -> bool:
        return self.build_index().matches(name, tvg_id)

    def filter_channels(self, channels: List[ChannelData]
                        ) -> Tuple[List[ChannelData], int]:
        idx = self.build_index()
        if not idx.names and not idx.tvg_ids:
            return list(channels), 0
        filtered, removed = [], 0
        for ch in channels:
            if idx.matches(ch.meta.name, ch.meta.tvg_id):
                removed += 1
            else:
                filtered.append(ch)
        return filtered, removed


class DomainBlacklistRule:
    __slots__ = ('value', 'is_ip', 'include_subdomains', 'note',
                 'added_date')

    def __init__(self, value: str = "",
                 include_subdomains: bool = True,
                 note: str = ""):
        self.value: str = URLUtils.normalize_host(value)
        self.is_ip: bool = URLUtils.is_ip_address(self.value)
        self.include_subdomains: bool = bool(include_subdomains)
        self.note: str = (note or "").strip()
        self.added_date: datetime = datetime.now()

    @staticmethod
    def _ip_eq(a: str, b: str) -> bool:
        try:
            ia, ib = (ipaddress.ip_address(a),
                      ipaddress.ip_address(b))
            if ia.version != ib.version:
                if ia.version == 6 and ia.ipv4_mapped:
                    ia = ia.ipv4_mapped
                if ib.version == 6 and ib.ipv4_mapped:
                    ib = ib.ipv4_mapped
            return ia == ib
        except ValueError:
            return False

    def matches_host(self, host: str) -> bool:
        if not host or not self.value:
            return False
        if self.is_ip:
            return self._ip_eq(host, self.value)
        if host == self.value:
            return True
        if (self.include_subdomains
                and host.endswith('.' + self.value)):
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'value': self.value, 'is_ip': self.is_ip,
            'include_subdomains': self.include_subdomains,
            'note': self.note,
            'added_date': self.added_date.strftime(
                "%Y-%m-%d %H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]
                  ) -> 'DomainBlacklistRule':
        r = cls()
        raw = data.get('value', '') or ''
        r.value = URLUtils.normalize_host(raw) or raw.strip().lower()
        r.is_ip = bool(data.get(
            'is_ip', URLUtils.is_ip_address(r.value)))
        r.include_subdomains = bool(
            data.get('include_subdomains', True))
        r.note = (data.get('note', '') or '').strip()
        dt = parse_datetime(data.get('added_date'))
        if dt is not None:
            r.added_date = dt
        return r


class DomainBlacklistManager:
    def __init__(self, config_dir: str):
        self._store = BaseJsonStore(
            os.path.join(config_dir, "domain_blacklist.json"), [])
        self._lock = threading.RLock()
        self._rules: List[DomainBlacklistRule] = [
            DomainBlacklistRule.from_dict(x)
            for x in self._store._data if isinstance(x, dict)
        ]
        self._cache: "OrderedDict[str, bool]" = OrderedDict()

    def _persist_locked(self) -> bool:
        self._store._data = [r.to_dict() for r in self._rules]
        return self._store.save()

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

    def add_domain(self, value: str,
                   include_subdomains: bool = True,
                   note: str = "") -> bool:
        normalized = URLUtils.normalize_host(value)
        if not normalized:
            return False
        with self._lock:
            for r in self._rules:
                if r.value == normalized:
                    changed = False
                    if r.include_subdomains != include_subdomains:
                        r.include_subdomains = bool(
                            include_subdomains)
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

    def get_rule(self, value: str
                 ) -> Optional[DomainBlacklistRule]:
        normalized = URLUtils.normalize_host(value)
        if not normalized:
            return None
        with self._lock:
            for r in self._rules:
                if r.value == normalized:
                    return r
        return None

    def _matches_host(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            cached = self._cache_get_locked(host)
            if cached is not None:
                return cached
            blocked = any(r.matches_host(host) for r in self._rules)
            self._cache_put_locked(host, blocked)
            return blocked

    def matches_url(self, url: str) -> bool:
        if not url:
            return False
        host = URLUtils.extract_host(url)
        return bool(host) and self._matches_host(host)

    def filter_channels(self, channels: List[ChannelData]
                        ) -> Tuple[List[ChannelData], int]:
        with self._lock:
            if not self._rules:
                return list(channels), 0
        filtered, removed = [], 0
        for ch in channels:
            if ch.link.url and self.matches_url(ch.link.url):
                removed += 1
            else:
                filtered.append(ch)
        return filtered, removed

    def clean_channels(self, channels: List[ChannelData]
                       ) -> Tuple[List[ChannelData], int]:
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
            if any(r.matches_host(host) for r in rules_snapshot):
                ch.link.alternative_urls = [
                    u for u in ch.link.alternative_urls
                    if u and not self.matches_url(u)]
                ch.clear_url()
                ch.status.status_text = StatusText.BLOCKED_BY_DOMAIN
                ch.update_extinf()
                ch.touch()
                cleaned += 1
        return list(channels), cleaned


class DomainUserAgentRule:
    __slots__ = ('domain', 'user_agent', 'enabled', 'created_date')

    def __init__(self, domain: str = "", user_agent: str = ""):
        self.domain: str = (URLUtils.normalize_host(domain)
                            if domain else "")
        self.user_agent: str = sanitize_user_agent(user_agent)
        self.enabled: bool = True
        self.created_date: datetime = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'domain': self.domain, 'user_agent': self.user_agent,
            'enabled': self.enabled,
            'created_date': self.created_date.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]
                  ) -> 'DomainUserAgentRule':
        r = cls()
        raw = data.get('domain', '') or ''
        r.domain = URLUtils.normalize_host(raw) or raw.strip().lower()
        r.user_agent = sanitize_user_agent(
            data.get('user_agent', '') or '')
        r.enabled = bool(data.get('enabled', True))
        dt = parse_datetime(data.get('created_date'))
        if dt is not None:
            r.created_date = dt
        return r


class DomainUserAgentManager:
    """Правила UA по доменам. Более специфичные правила первыми."""

    def __init__(self, config_dir: str):
        self._store = BaseJsonStore(
            os.path.join(config_dir,
                         "domain_user_agent_rules.json"), [])
        self._lock = threading.RLock()
        self._rules: List[DomainUserAgentRule] = [
            DomainUserAgentRule.from_dict(x)
            for x in self._store._data if isinstance(x, dict)
        ]
        self._cache: "OrderedDict[str, Tuple[bool, Optional[str]]]" = \
            OrderedDict()
        self._sorted_rules: Optional[List[DomainUserAgentRule]] = None

    def _persist_locked(self) -> bool:
        self._store._data = [r.to_dict() for r in self._rules]
        self._sorted_rules = None
        return self._store.save()

    def _invalidate_cache_locked(self):
        self._cache.clear()
        self._sorted_rules = None

    def _get_sorted_rules_locked(self) -> List[DomainUserAgentRule]:
        if self._sorted_rules is None:
            self._sorted_rules = sorted(
                self._rules,
                key=lambda r: (-len(r.domain), r.domain))
        return self._sorted_rules

    def _cache_get_locked(
            self, host: str) -> Optional[Tuple[bool, Optional[str]]]:
        if host in self._cache:
            self._cache.move_to_end(host)
            return self._cache[host]
        return None

    def _cache_put_locked(self, host: str, matched: bool,
                          ua: Optional[str]):
        self._cache[host] = (bool(matched), ua)
        self._cache.move_to_end(host)
        while len(self._cache) > DOMAIN_UA_CACHE_MAX:
            self._cache.popitem(last=False)

    def add_rule(self, domain: str, user_agent: str) -> bool:
        normalized = URLUtils.normalize_host(domain)
        if not normalized:
            return False
        ua = sanitize_user_agent(user_agent)
        with self._lock:
            for r in self._rules:
                if r.domain == normalized:
                    r.user_agent = ua
                    break
            else:
                self._rules.append(
                    DomainUserAgentRule(normalized, ua))
            self._invalidate_cache_locked()
            return self._persist_locked()

    def remove_rule(self, domain: str) -> bool:
        normalized = URLUtils.normalize_host(domain)
        if not normalized:
            return False
        with self._lock:
            for i, r in enumerate(self._rules):
                if r.domain == normalized:
                    del self._rules[i]
                    self._invalidate_cache_locked()
                    return self._persist_locked()
        return False

    def set_enabled(self, domain: str, enabled: bool) -> bool:
        normalized = URLUtils.normalize_host(domain)
        if not normalized:
            return False
        with self._lock:
            for r in self._rules:
                if r.domain == normalized:
                    if r.enabled == enabled:
                        return False
                    r.enabled = enabled
                    self._invalidate_cache_locked()
                    return self._persist_locked()
        return False

    def get_rule(self, domain: str
                 ) -> Optional[DomainUserAgentRule]:
        normalized = URLUtils.normalize_host(domain)
        if not normalized:
            return None
        with self._lock:
            for r in self._rules:
                if r.domain == normalized:
                    return r
        return None

    def get_all_rules(self) -> List[DomainUserAgentRule]:
        with self._lock:
            return list(self._rules)

    def _find_ua_for_host(
            self, host: str) -> Tuple[bool, Optional[str]]:
        if not host:
            return False, None
        host = host.lower()
        with self._lock:
            cached = self._cache_get_locked(host)
            if cached is not None:
                return cached
            for r in self._get_sorted_rules_locked():
                if not r.enabled:
                    continue
                if host == r.domain or host.endswith('.' + r.domain):
                    ua = r.user_agent or None
                    self._cache_put_locked(host, True, ua)
                    return True, ua
            self._cache_put_locked(host, False, None)
            return False, None

    def get_user_agent_for_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        host = URLUtils.extract_host(url)
        if not host:
            return None
        matched, ua = self._find_ua_for_host(host)
        return ua if matched else None

    def should_remove_user_agent(self, url: str) -> bool:
        if not url:
            return False
        host = URLUtils.extract_host(url)
        if not host:
            return False
        matched, ua = self._find_ua_for_host(host)
        return matched and ua is None

    @staticmethod
    def _has_extvlcopt_ua(channel: ChannelData,
                          expected_ua: str) -> bool:
        if not expected_ua:
            return False
        prefix = '#EXTVLCOPT:http-user-agent='
        for line in channel.link.extvlcopt_lines:
            if not line.startswith(prefix):
                continue
            if line[len(prefix):].strip().strip('"') == expected_ua:
                return True
        return False

    def apply_rules_to_channel(self, channel: ChannelData) -> bool:
        if not channel.link.url:
            return False
        if self.should_remove_user_agent(channel.link.url):
            changed = False
            if channel.link.user_agent:
                channel.link.user_agent = ""
                changed = True
            for k in [k for k in channel.link.extra_headers
                      if k.lower() == 'user-agent']:
                del channel.link.extra_headers[k]
                changed = True
            if changed:
                channel.update_extvlcopt_from_headers()
                channel.touch()
            return changed

        new_ua = self.get_user_agent_for_url(channel.link.url)
        if new_ua is None:
            return False

        header_ua = None
        for k, v in channel.link.extra_headers.items():
            if k.lower() == 'user-agent':
                header_ua = v
                break

        if (new_ua == channel.link.user_agent
                and header_ua == new_ua
                and self._has_extvlcopt_ua(channel, new_ua)):
            return False

        channel.link.user_agent = new_ua
        channel.link.extra_headers['User-Agent'] = new_ua
        channel.update_extvlcopt_from_headers()
        channel.touch()
        return True

    def apply_rules_to_channels(
            self, channels: Iterable[ChannelData]) -> int:
        enabled_domains = [r.domain
                           for r in self.get_all_rules()
                           if r.enabled]
        total = matched = changed = 0
        for ch in channels:
            total += 1
            if not ch.link.url:
                continue
            host = URLUtils.extract_host(ch.link.url)
            if not host:
                continue
            if URLUtils.host_in_domains(host, enabled_domains):
                matched += 1
            if self.apply_rules_to_channel(ch):
                changed += 1
        logger.info("UA rules: total=%d, matched=%d, changed=%d",
                    total, matched, changed)
        return changed