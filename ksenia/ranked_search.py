# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""RankedSearchIndex — инвертированный индекс с ранжированием."""
from __future__ import annotations

import threading
from collections import OrderedDict, defaultdict
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set, Tuple

from logging_setup import logger
from models import ChannelData
from name_normalizer import normalize_with_settings


FIELD_WEIGHTS: Dict[str, int] = {
    'name': 100,
    'tvg_name': 60,
    'group': 50,
    'tvg_id': 40,
    'url': 20,
    'user_agent': 10,
}

_SEARCH_CACHE_MAX = 128


def parse_special_prefix(query: str
                          ) -> Tuple[Optional[str], str]:
    s = (query or "").strip()
    if s.startswith("is:"):
        rest = s[3:].strip()
        first, _, tail = rest.partition(" ")
        first = first.lower()
        if first in ("orphan", "broken", "working"):
            return first, tail.strip()
    return None, s


class RankedSearchIndex:
    def __init__(self):
        self._lock = threading.RLock()
        self._token_index: Dict[str, Set[int]] = defaultdict(set)
        self._uid_to_channel: Dict[int, ChannelData] = {}
        self._uid_tokens: Dict[int, Set[str]] = {}
        self._search_cache: "OrderedDict[str, List[int]]" = \
            OrderedDict()
        self._revision = 0
        self._settings = None

    def rebuild(self, channels: List[ChannelData],
                settings=None) -> None:
        with self._lock:
            self._token_index.clear()
            self._uid_to_channel.clear()
            self._uid_tokens.clear()
            self._search_cache.clear()
            self._revision += 1
            self._settings = settings
            for ch in channels:
                self._uid_to_channel[ch.uid] = ch
                tokens = self._channel_tokens(ch, settings)
                self._uid_tokens[ch.uid] = tokens
                for t in tokens:
                    self._token_index[t].add(ch.uid)

    def invalidate(self) -> None:
        with self._lock:
            self._search_cache.clear()
            self._revision += 1

    def is_empty(self) -> bool:
        with self._lock:
            return not self._uid_to_channel

    def search(self, query: str, limit: int = 200
               ) -> List[ChannelData]:
        q = (query or "").strip()
        if not q:
            with self._lock:
                return list(self._uid_to_channel.values())
        with self._lock:
            cache_key = f"{q}|{limit}|{self._revision}"
            cached = self._search_cache.get(cache_key)
            if cached is not None:
                self._search_cache.move_to_end(cache_key)
                return [self._uid_to_channel[u] for u in cached
                        if u in self._uid_to_channel]

        query_tokens = self._tokenize(q, self._settings)
        if not query_tokens:
            return []

        with self._lock:
            candidates: Set[int] = set()
            for t in query_tokens:
                candidates |= self._token_index.get(t, set())
            if not candidates:
                candidates = set(self._uid_to_channel.keys())
            query_norm = normalize_with_settings(
                q, self._settings)
            scored: List[Tuple[float, int]] = []
            for uid in candidates:
                ch = self._uid_to_channel.get(uid)
                if ch is None:
                    continue
                score = self._score(ch, query_norm, query_tokens)
                if score > 0:
                    scored.append((score, uid))
            scored.sort(key=lambda x: (-x[0], x[1]))
            top = [uid for _, uid in scored[:limit]]
            self._search_cache[cache_key] = top
            self._search_cache.move_to_end(cache_key)
            while len(self._search_cache) > _SEARCH_CACHE_MAX:
                self._search_cache.popitem(last=False)
            return [self._uid_to_channel[u] for u in top
                    if u in self._uid_to_channel]

    def _channel_tokens(self, ch: ChannelData,
                        settings) -> Set[str]:
        parts: List[str] = []
        for field in FIELD_WEIGHTS:
            v = self._field_value(ch, field)
            if v:
                parts.append(v)
        return self._tokenize(" ".join(parts), settings)

    @staticmethod
    def _field_value(ch: ChannelData, field: str) -> str:
        mapping = {
            'name': ch.meta.name,
            'tvg_name': ch.meta.tvg_name,
            'group': ch.meta.group,
            'tvg_id': ch.meta.tvg_id,
            'url': ch.link.url,
            'user_agent': ch.link.user_agent,
        }
        return mapping.get(field, "") or ""

    @staticmethod
    def _tokenize(text: str, settings) -> Set[str]:
        if not text:
            return set()
        norm = normalize_with_settings(text, settings)
        return {t for t in norm.split() if t}

    def _score(self, ch: ChannelData, query_norm: str,
               query_tokens: Set[str]) -> float:
        score = 0.0
        name_norm = normalize_with_settings(
            ch.meta.name or "", self._settings)
        if name_norm and query_norm:
            if name_norm == query_norm:
                score += 200.0
            elif name_norm.startswith(query_norm):
                score += 150.0
            else:
                ratio = SequenceMatcher(
                    None, query_norm, name_norm).ratio()
                score += ratio * 100.0
        for field, weight in FIELD_WEIGHTS.items():
            if field == 'name':
                continue
            value = self._field_value(ch, field)
            if not value:
                continue
            v_norm = normalize_with_settings(
                value, self._settings)
            if not v_norm:
                continue
            if query_norm and query_norm == v_norm:
                score += weight * 1.5
            elif query_norm and query_norm in v_norm:
                score += weight
        ch_tokens = self._uid_tokens.get(ch.uid, set())
        if query_tokens and ch_tokens:
            if query_tokens.issubset(ch_tokens):
                score += 50.0
            else:
                overlap = len(query_tokens & ch_tokens)
                if overlap:
                    score += (overlap / len(query_tokens)) * 30.0
        return score