# -*- coding: utf-8 -*-
"""Config, LinkReplacementSettings."""

from __future__ import annotations
import os
import json
import copy
import threading
from contextlib import suppress
from typing import Any, Dict, Optional
from constants import (URL_CHECK_MAX_WORKERS, FALLBACK_DAYS_DEFAULT,
    EPG_CACHE_TTL_HOURS, CHECK_RESULT_CACHE_TTL_HOURS,
    EPG_FUZZY_ENABLED_DEFAULT, EPG_FUZZY_THRESHOLD_DEFAULT,
    EPG_FUZZY_MIN_LENGTH_DEFAULT, EPG_FUZZY_MIN_GAP_DEFAULT,
    VLC_PLAYER_DEFAULT_VOLUME, REPLACEMENT_MAX_WORKERS_DEFAULT,
    SOURCE_CHECK_TRUST_SEC_DEFAULT, SOURCE_CHECK_WORKERS_DEFAULT,
    SOURCE_CHECK_TIMEOUT_DEFAULT, SOURCE_CHECK_BATCH_SIZE_DEFAULT,
    DEFAULT_TIMEOUT)
from paths import Paths, logger


class Config:
    DEFAULT = {
        'max_workers': URL_CHECK_MAX_WORKERS,
        'check_timeout': 3,
        'max_retries': 0,
        'retry_delay': 0.0,
        'use_link_cache': True,
        'link_cache_hours': 6,
        'days_to_check': FALLBACK_DAYS_DEFAULT,
        'use_cache_manager': True,
        'max_channels_to_check': 2000,
        'limit_check_enabled': False,
        'save_extvlcopt': True,
        'auto_update_sources': True,
        'ignore_special_chars_in_names': True,
        'remove_parentheses_in_names': True,
        'remove_brackets_in_names': True,
        'remove_emojis_in_names': True,
        'match_threshold_percent': 80.0,
        'min_name_similarity': 0.1,
        'search_type': 'exact',
        'use_fuzzy_matching': True,
        'auto_replace_broken': True,
        'auto_replace_missing': True,
        'keep_backup_links': True,
        'max_alternative_urls': 5,
        'use_ip_filtering': True,
        'verify_ssl': False,
        'cell_font_size': 10,
        'cache_cleanup_days': 30,
        'keep_duplicates': False,
        'show_tvg_id': True,
        'show_tvg_logo': True,
        'show_catchup': False,
        'show_status_bar': True,
        'enable_icons': True,
        'preserve_original_order': True,
        'temporary_domains': ["tmp.", "temp.", "short."],
        'unsafe_domains': ["malware.", "phishing.", "spam."],
        'dedup_by_name_use_tvg': False,
        'link_cache_max_files': 10000,
        'link_cache_max_mb': 64,
        'epg_cache_ttl_hours': EPG_CACHE_TTL_HOURS,
        'check_result_cache_ttl_hours': CHECK_RESULT_CACHE_TTL_HOURS,
        'use_check_result_cache': True,
        'epg_overwrite_metadata': True,
        'epg_fuzzy_match_enabled': EPG_FUZZY_ENABLED_DEFAULT,
        'epg_fuzzy_threshold': EPG_FUZZY_THRESHOLD_DEFAULT,
        'epg_fuzzy_min_length': EPG_FUZZY_MIN_LENGTH_DEFAULT,
        'epg_fuzzy_min_gap': EPG_FUZZY_MIN_GAP_DEFAULT,
        'auto_backup_before_save': True,
        'vlc_volume': VLC_PLAYER_DEFAULT_VOLUME,
        'vlc_start_fullscreen': False,
        'replacement_max_workers': REPLACEMENT_MAX_WORKERS_DEFAULT,
        'fast_replacement_mode': True,
        'replace_check_timeout': 2,
        'replace_max_retries': 0,
        'replace_retry_delay': 0.0,
        'max_urls_to_check_per_channel': 3,
        'check_cache_trust_seconds': 86400,
        'source_check_workers': SOURCE_CHECK_WORKERS_DEFAULT,
        'source_check_timeout': SOURCE_CHECK_TIMEOUT_DEFAULT,
        'source_check_trust_sec': SOURCE_CHECK_TRUST_SEC_DEFAULT,
        'source_check_batch_size': SOURCE_CHECK_BATCH_SIZE_DEFAULT,
        'apply_filters_on_file_open': True,
    }

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_dir = Paths.get_config_dir()
            os.makedirs(config_dir, exist_ok=True)
            config_path = os.path.join(config_dir, "editor_config.json")
        self.config_path = config_path
        self.config: Dict[str, Any] = copy.deepcopy(self.DEFAULT)
        self._lock = threading.RLock()
        self._types: Dict[str, type] = _build_config_types(self.DEFAULT)
        self._load()

    def _coerce(self, key: str, value: Any) -> Any:
        expected = self._types.get(key)
        if expected is None:
            return value
        if expected is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ('1', 'true', 'yes', 'on', 'да')
            return bool(value)
        if expected is int:
            try:
                return int(value)
            except (ValueError, TypeError):
                return self.DEFAULT.get(key, value)
        if expected is float:
            try:
                return float(value)
            except (ValueError, TypeError):
                return self.DEFAULT.get(key, value)
        if expected is list:
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                return [v.strip() for v in value.split(',') if v.strip()]
            return list(self.DEFAULT.get(key, []) or [])
        if expected is str:
            if isinstance(value, str):
                return value
            return str(value)
        return value

    def _load(self):
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in ('whitelisted_ips', 'whitelisted_domains',
                             'prioritize_whitelisted',
                             'blacklisted_domains', 'blacklisted_ips'):
                        continue
                    self.config[k] = self._coerce(k, v)
                logger.info(f"Config: загружено {len(data)} ключей")
        except json.JSONDecodeError:
            logger.exception("Config load (JSON)")
        except Exception:
            logger.exception("Config load")

    def save(self) -> bool:
        with self._lock:
            try:
                tmp = self.config_path + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=2, ensure_ascii=False)
                    f.flush()
                    with suppress(OSError):
                        os.fsync(f.fileno())
                os.replace(tmp, self.config_path)
                return True
            except Exception:
                logger.exception("Config save")
                return False

    def get(self, key: str, default=None):
        with self._lock:
            return self.config.get(key, default)

    def set(self, key: str, value):
        with self._lock:
            self.config[key] = value

    def update(self, values: Dict[str, Any]):
        with self._lock:
            self.config.update(values)


def _build_config_types(default: Dict[str, Any]) -> Dict[str, type]:
    types: Dict[str, type] = {}
    for k, v in default.items():
        if isinstance(v, bool):
            types[k] = bool
        elif isinstance(v, int):
            types[k] = int
        elif isinstance(v, float):
            types[k] = float
        elif isinstance(v, str):
            types[k] = str
        elif isinstance(v, list):
            types[k] = list
        else:
            types[k] = type(v)
    return types


class LinkReplacementSettings:
    __slots__ = ('_config',)

    def __init__(self, config: Config):
        object.__setattr__(self, '_config', config)

    def __getattr__(self, name: str):
        cfg = object.__getattribute__(self, '_config')
        if name in Config.DEFAULT:
            return cfg.get(name, Config.DEFAULT[name])
        raise AttributeError(name)

    def __setattr__(self, name: str, value):
        if name == '_config':
            object.__setattr__(self, name, value)
            return
        self._config.set(name, value)

    def is_blacklisted(self, url: str) -> bool:
        if not self.use_ip_filtering or not url:
            return False
        try:
            from ksenia_window import ApplicationCore
            core = ApplicationCore.instance()
            return core.domain_blacklist_manager.matches_url(url)
        except Exception:
            return False

    def is_filtered_domain(self, url: str) -> bool:
        if not url:
            return False
        try:
            from utils import URLUtils
            host = URLUtils.extract_host(url) or ''
        except Exception:
            host = ''
        if host:
            for d in list(self.temporary_domains) + list(self.unsafe_domains):
                dn = (d or '').strip().lower().strip('.')
                if not dn:
                    continue
                if host == dn or host.endswith('.' + dn):
                    return True
            return False
        u = url.lower()
        return any((d or '').lower() in u
                   for d in self.temporary_domains) or \
               any((d or '').lower() in u
                   for d in self.unsafe_domains)

    def get_replace_timeout(self) -> int:
        if self._config.get('fast_replacement_mode', True):
            return int(self._config.get('replace_check_timeout', 2))
        return int(self._config.get('check_timeout', DEFAULT_TIMEOUT))

    def get_replace_retries(self) -> int:
        if self._config.get('fast_replacement_mode', True):
            return int(self._config.get('replace_max_retries', 0))
        return int(self._config.get('max_retries', 2))

    def get_replace_retry_delay(self) -> float:
        if self._config.get('fast_replacement_mode', True):
            return float(self._config.get('replace_retry_delay', 0.0))
        return float(self._config.get('retry_delay', 0.5))

    def get_max_urls_per_channel(self) -> int:
        return max(1, int(self._config.get(
            'max_urls_to_check_per_channel', 3)))