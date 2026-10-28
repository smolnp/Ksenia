# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Настройки: общие, замена ссылок, кэш."""
from __future__ import annotations

from contextlib import suppress
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QProgressBar, QSpinBox, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget)

from config import Config
from constants import (OK_CANCEL_BB, URL_CHECK_MAX_WORKERS,
                        DEFAULT_EPG_URL,
                        CHECK_RESULT_CACHE_TTL_HOURS,
                        EPG_CACHE_TTL_HOURS)
from dialogs_base import BaseDialog, hint_label
from logging_setup import logger
from paths import confirm, error_box, info_box, warn_box


SETTINGS_SPEC: List[Tuple[str, str, str, Dict[str, Any],
                           str, Optional[str]]] = [
    ("search_type_combo", "search_type", "combo",
     {"items": [
         ("Точное совпадение", "exact"),
         ("Похожие (SequenceMatcher)", "similar"),
         ("Нечёткий (Jaccard)", "fuzzy"),
     ]},
     "search", "Тип поиска:"),
    ("match_threshold_spin", "match_threshold_percent", "dspin",
     {"range": (0.0, 100.0), "decimals": 1, "suffix": " %"},
     "search", "Порог совпадения:"),
    ("min_similarity_spin", "min_name_similarity", "dspin",
     {"range": (0.0, 1.0), "decimals": 2, "step": 0.05},
     "search", "Мин. схожесть имён:"),
    ("use_fuzzy_check", "use_fuzzy_matching", "check",
     {}, "search", "Использовать нечёткое сравнение"),
    ("ignore_special_check", "ignore_special_chars_in_names",
     "check", {}, "norm", "Игнорировать спецсимволы"),
    ("remove_parens_check", "remove_parentheses_in_names",
     "check", {}, "norm", "Удалять (...)"),
    ("remove_brackets_check", "remove_brackets_in_names",
     "check", {}, "norm", "Удалять [...]"),
    ("remove_emoji_check", "remove_emojis_in_names",
     "check", {}, "norm", "Удалять эмодзи"),
    ("use_iptv_org_check", "use_iptv_org_aliases",
     "check", {}, "norm", "Использовать алиасы iptv-org"),
    ("iptv_org_ttl_spin", "iptv_org_aliases_ttl_hours", "spin",
     {"range": (1, 720), "suffix": " ч"},
     "norm", "TTL кэша iptv-org:"),
    ("auto_broken_check", "auto_replace_broken",
     "check", {}, "auto", "Заменять битые ссылки"),
    ("auto_missing_check", "auto_replace_missing",
     "check", {}, "auto", "Заполнять отсутствующие ссылки"),
    ("keep_backup_check", "keep_backup_links",
     "check", {}, "auto", "Сохранять старые ссылки"),
    ("max_alt_spin", "max_alternative_urls", "spin",
     {"range": (1, 50)},
     "auto", "Макс. альтернативных ссылок:"),
    ("replacement_workers_spin", "replacement_max_workers", "spin",
     {"range": (1, 8)},
     "auto", "Потоков замены (каналов):"),
    ("fast_replacement_check", "fast_replacement_mode",
     "check", {}, "auto",
     "⚡ Быстрая замена (агрессивные таймауты)"),
    ("replace_timeout_spin", "replace_check_timeout", "spin",
     {"range": (1, 60), "suffix": " с"},
     "auto", "Таймаут замены:"),
    ("replace_retries_spin", "replace_max_retries", "spin",
     {"range": (0, 5)},
     "auto", "Повторов при замене:"),
    ("max_urls_per_channel_spin",
     "max_urls_to_check_per_channel", "spin",
     {"range": (1, 50)},
     "auto", "Макс. URL на канал:"),
    ("cache_trust_spin", "check_cache_trust_seconds", "spin",
     {"range": (60, 86400), "suffix": " с"},
     "auto", "Доверять кэшу проверок (сек):"),
    ("verify_candidates_check", "verify_candidates_if_multiple",
     "check", {}, "auto",
     "Проверять кандидатов, если их несколько"),
    ("verify_candidates_timeout_spin",
     "verify_candidates_timeout", "spin",
     {"range": (1, 30), "suffix": " с"},
     "auto", "Таймаут проверки кандидатов:"),
    ("verify_candidates_workers_spin",
     "verify_candidates_max_workers", "spin",
     {"range": (1, 8)},
     "auto", "Потоков проверки кандидатов:"),
    ("source_check_workers_spin", "source_check_workers", "spin",
     {"range": (1, 32)},
     "auto", "Потоков проверки источников:"),
    ("source_check_timeout_spin", "source_check_timeout", "spin",
     {"range": (1, 60), "suffix": " с"},
     "auto", "Таймаут проверки источников:"),
    ("source_check_trust_spin", "source_check_trust_sec", "spin",
     {"range": (60, 86400), "suffix": " с"},
     "auto", "Доверять кэшу (источники):"),
    ("source_check_batch_spin", "source_check_batch_size", "spin",
     {"range": (10, 1000)},
     "auto", "Размер батча записи:"),
    ("timeout_spin", "check_timeout", "spin",
     {"range": (1, 120), "suffix": " с"},
     "net", "Таймаут проверки (обычная):"),
    ("workers_spin", "max_workers", "spin",
     {"range": (1, URL_CHECK_MAX_WORKERS)},
     "net", "Потоков проверки:"),
    ("retries_spin", "max_retries", "spin",
     {"range": (0, 10)},
     "net", "Повторов (обычная):"),
    ("retry_delay_spin", "retry_delay", "dspin",
     {"range": (0.0, 10.0), "decimals": 1,
      "step": 0.1, "suffix": " с"},
     "net", "Задержка между повторами:"),
    ("verify_ssl_check", "verify_ssl", "check",
     {}, "net", "Проверять SSL сертификаты"),
    ("use_ip_filter_check", "use_ip_filtering", "check",
     {}, "filt", "Использовать IP/домен-фильтры"),
    ("temporary_domains_edit", "temporary_domains", "text",
     {"max_height": 50}, "filt", "Временные домены:"),
    ("unsafe_domains_edit", "unsafe_domains", "text",
     {"max_height": 50}, "filt", "Небезопасные домены:"),
    ("epg_overwrite_check", "epg_overwrite_metadata", "check",
     {}, "epg",
     "Перезаписывать существующие метаданные из EPG"),
    ("epg_fuzzy_enabled_check", "epg_fuzzy_match_enabled", "check",
     {}, "epg", "Нечёткий поиск EPG по имени канала"),
    ("epg_fuzzy_threshold_spin", "epg_fuzzy_threshold", "dspin",
     {"range": (0.5, 1.0), "decimals": 2, "step": 0.01},
     "epg", "Порог нечёткого поиска:"),
    ("epg_fuzzy_min_length_spin", "epg_fuzzy_min_length", "spin",
     {"range": (1, 50)},
     "epg", "Мин. длина имени:"),
    ("epg_fuzzy_min_gap_spin", "epg_fuzzy_min_gap", "dspin",
     {"range": (0.0, 0.5), "decimals": 2, "step": 0.01},
     "epg", "Мин. отрыв от 2-го места:"),
    ("epg_overwrite_name_check", "epg_overwrite_channel_name",
     "check", {}, "epg",
     "Перезаписывать имя канала из EPG"),
    ("epg_prefer_tvg_id_check", "epg_prefer_tvg_id_over_name",
     "check", {}, "epg",
     "Предпочитать tvg_id канала при матчинге EPG"),
    ("epg_avoid_conflicts_check", "epg_avoid_name_conflicts",
     "check", {}, "epg",
     "Избегать конфликтов имён из EPG"),
]


def _make_widget(kind: str, params: Dict[str, Any],
                 label: Optional[str],
                 parent: Optional[QWidget] = None) -> QWidget:
    if kind == "check":
        w = QCheckBox(parent)
        if label:
            w.setText(label)
        return w
    if kind == "spin":
        w = QSpinBox(parent)
        rng = params.get("range", (0, 100))
        w.setRange(*rng)
        if "suffix" in params:
            w.setSuffix(params["suffix"])
        return w
    if kind == "dspin":
        w = QDoubleSpinBox(parent)
        rng = params.get("range", (0.0, 1.0))
        w.setRange(*rng)
        if "decimals" in params:
            w.setDecimals(params["decimals"])
        if "step" in params:
            w.setSingleStep(params["step"])
        if "suffix" in params:
            w.setSuffix(params["suffix"])
        return w
    if kind == "combo":
        w = QComboBox(parent)
        for item_label, data in params.get("items", []):
            w.addItem(item_label, data)
        return w
    if kind == "text":
        w = QTextEdit(parent)
        if "max_height" in params:
            w.setMaximumHeight(params["max_height"])
        return w
    if kind == "line":
        w = QLineEdit(parent)
        if "placeholder" in params:
            w.setPlaceholderText(params["placeholder"])
        return w
    raise ValueError(f"Unknown widget kind: {kind}")


def _read_value(kind: str, w: QWidget) -> Any:
    if kind == "check":
        return w.isChecked()
    if kind == "spin":
        return w.value()
    if kind == "dspin":
        return w.value()
    if kind == "combo":
        return w.currentData()
    if kind == "text":
        return [ln.strip() for ln in
                w.toPlainText().replace(',', '\n').splitlines()
                if ln.strip()]
    if kind == "line":
        return w.text().strip()
    return None


def _write_value(kind: str, w: QWidget, value: Any) -> None:
    if kind == "check":
        w.setChecked(bool(value))
    elif kind == "spin":
        with suppress(ValueError, TypeError):
            w.setValue(int(value))
    elif kind == "dspin":
        with suppress(ValueError, TypeError):
            w.setValue(float(value))
    elif kind == "combo":
        idx = w.findData(value)
        if idx >= 0:
            w.setCurrentIndex(idx)
    elif kind == "text":
        if isinstance(value, list):
            w.setPlainText("\n".join(str(x) for x in value))
        else:
            w.setPlainText(str(value or ""))
    elif kind == "line":
        w.setText(str(value or ""))


class LinkReplacementSettingsDialog(BaseDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__("Настройки замены ссылок", parent,
                          size=(660, 860))
        self.config = config
        self._widgets: Dict[str, QWidget] = {}
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        layout = self.root
        tabs = QTabWidget()
        tabs_by_key: Dict[str, Tuple[QTabWidget, QWidget,
                                     QFormLayout]] = {}
        for tab_key, title in (
                ("search", "Поиск"),
                ("norm", "Нормализация"),
                ("auto", "Автозамена"),
                ("net", "Сеть"),
                ("filt", "Фильтрация"),
                ("epg", "EPG-метаданные")):
            w = QWidget()
            f = QFormLayout(w)
            tabs.addTab(w, title)
            tabs_by_key[tab_key] = (tabs, w, f)

        for attr, cfg_key, kind, params, tab_key, label in SETTINGS_SPEC:
            widget = _make_widget(kind, params, label, self)
            self._widgets[attr] = widget
            _, _, form = tabs_by_key[tab_key]
            if kind == "check":
                form.addRow(widget)
            else:
                form.addRow(label or "", widget)

        iptv_row = QHBoxLayout()
        self.iptv_org_status_label = QLabel("")
        self.iptv_org_status_label.setWordWrap(True)
        iptv_row.addWidget(self.iptv_org_status_label, 1)
        self.iptv_org_refresh_btn = QPushButton("Обновить")
        self.iptv_org_refresh_btn.clicked.connect(
            self._refresh_iptv_org)
        iptv_row.addWidget(self.iptv_org_refresh_btn)
        iptv_w = QWidget()
        iptv_w.setLayout(iptv_row)
        tabs_by_key["norm"][2].addRow(iptv_w)

        tabs_by_key["search"][2].addRow(hint_label(
            "Порог (%) и мин. схожесть (0–1) — две шкалы для "
            "одной задачи. В коде берётся максимум из двух: "
            "max(min_name_similarity, match_threshold_percent / "
            "100).\n"
            "Если не уверены — оставьте 80 % и 0.1."))

        fast_w = self._widgets.get("fast_replacement_check")
        if fast_w is not None:
            fast_w.setToolTip(
                "При включении игнорируются: таймаут, повторы, "
                "макс. URL на канал, доверие кэшу и проверка "
                "кандидатов. Используются агрессивные значения "
                "по умолчанию.")
            fast_w.toggled.connect(
                self._on_fast_replacement_toggled)

        st = self._widgets.get("search_type_combo")
        if st is not None:
            st.currentIndexChanged.connect(
                self._on_search_type_changed)

        epg_chk = self._widgets.get("epg_fuzzy_enabled_check")
        if epg_chk is not None:
            epg_chk.toggled.connect(self._on_epg_fuzzy_toggled)

        tabs_by_key["auto"][2].addRow(hint_label(
            "⚡ Быстрая замена эффективна для массовой "
            "замены сотен битых каналов."))
        tabs_by_key["filt"][2].addRow(hint_label(
            "ЧС домен/IP управляется отдельно. "
            "По умолчанию: ОЧИЩАЕТ ссылку, канал сохраняется."))
        tabs_by_key["epg"][2].addRow(hint_label(
            "Имена каналов (meta.name) не перезаписываются "
            "из EPG, если не включён пункт "
            "«Перезаписывать имя канала из EPG»."))

        layout.addWidget(tabs)
        self.add_ok_cancel()
        self.add_reset_button(self._reset_to_defaults)

    def _on_search_type_changed(self, _idx: int):
        combo = self._widgets.get("search_type_combo")
        if combo is None:
            return
        is_exact = (combo.currentData() == "exact")
        for attr in ("match_threshold_spin",
                     "min_similarity_spin",
                     "use_fuzzy_check"):
            w = self._widgets.get(attr)
            if w is not None:
                w.setEnabled(not is_exact)

    def _on_epg_fuzzy_toggled(self, checked: bool):
        for attr in ("epg_fuzzy_threshold_spin",
                     "epg_fuzzy_min_length_spin",
                     "epg_fuzzy_min_gap_spin"):
            w = self._widgets.get(attr)
            if w is not None:
                w.setEnabled(checked)

    def _on_fast_replacement_toggled(self, checked: bool):
        for attr in ("replace_timeout_spin",
                     "replace_retries_spin",
                     "max_urls_per_channel_spin",
                     "cache_trust_spin",
                     "verify_candidates_check",
                     "verify_candidates_timeout_spin",
                     "verify_candidates_workers_spin"):
            w = self._widgets.get(attr)
            if w is not None:
                w.setEnabled(not checked)

    def _load(self):
        c = self.config
        for attr, cfg_key, kind, _p, _t, _l in SETTINGS_SPEC:
            w = self._widgets.get(attr)
            if w is None:
                continue
            _write_value(kind, w, c.get(cfg_key))
        fast_w = self._widgets.get("fast_replacement_check")
        if fast_w is not None:
            self._on_fast_replacement_toggled(fast_w.isChecked())
        st = self._widgets.get("search_type_combo")
        if st is not None:
            self._on_search_type_changed(st.currentIndex())
        epg_chk = self._widgets.get("epg_fuzzy_enabled_check")
        if epg_chk is not None:
            self._on_epg_fuzzy_toggled(epg_chk.isChecked())
        self._update_iptv_org_status()

    def _apply(self):
        c = self.config
        for attr, cfg_key, kind, _p, _t, _l in SETTINGS_SPEC:
            w = self._widgets.get(attr)
            if w is None:
                continue
            c.set(cfg_key, _read_value(kind, w))
        c.set('replace_retry_delay', 0.0)

    def _reset_to_defaults(self):
        if not confirm(self,
                        "Сбросить все настройки замены ссылок к "
                        "значениям по умолчанию?"):
            return
        for attr, cfg_key, kind, _p, _t, _l in SETTINGS_SPEC:
            w = self._widgets.get(attr)
            if w is None:
                continue
            _write_value(kind, w, Config.DEFAULT.get(cfg_key))
        fast_w = self._widgets.get("fast_replacement_check")
        if fast_w is not None:
            self._on_fast_replacement_toggled(fast_w.isChecked())
        st = self._widgets.get("search_type_combo")
        if st is not None:
            self._on_search_type_changed(st.currentIndex())
        epg_chk = self._widgets.get("epg_fuzzy_enabled_check")
        if epg_chk is not None:
            self._on_epg_fuzzy_toggled(epg_chk.isChecked())

    def _update_iptv_org_status(self):
        try:
            from iptv_org_aliases import get_aliases_info
            info = get_aliases_info()
            count = info.get("count", 0)
            age = info.get("cache_age_sec")
            age_txt = ("нет кэша" if age is None
                       else f"{age / 3600.0:.1f} ч")
            self.iptv_org_status_label.setText(
                f"Алиасы: {count} • {age_txt}")
        except Exception:
            self.iptv_org_status_label.setText(
                "Модуль iptv-org недоступен")

    def _refresh_iptv_org(self):
        try:
            from iptv_org_aliases import get_aliases
            self.iptv_org_status_label.setText(
                "Загрузка алиасов iptv-org...")
            self.iptv_org_refresh_btn.setEnabled(False)
            aliases = get_aliases(force_refresh=True)
            self._update_iptv_org_status()
            info_box(
                self,
                f"Алиасы iptv-org: {len(aliases or {})}",
                "iptv-org")
        except Exception as e:
            logger.exception("refresh_iptv_org")
            error_box(self, str(e))
        finally:
            self.iptv_org_refresh_btn.setEnabled(True)

    def accept(self):
        try:
            self._apply()
        except Exception:
            logger.exception("apply failed")
            warn_box(self, "Не удалось сохранить настройки")
            return
        if not self.config.save():
            warn_box(self, "Не удалось сохранить настройки")
            return
        super().accept()


GENERAL_SPEC: List[Tuple[str, str, str, Dict[str, Any],
                          str, Optional[str]]] = [
    ("font_size_spin", "cell_font_size", "spin",
     {"range": (6, 24)}, "main", "Размер шрифта:"),
    ("save_extvlcopt_check", "save_extvlcopt", "check",
     {}, "main", "Сохранять EXTVLCOPT-строки"),
    ("keep_dup_check", "keep_duplicates", "check",
     {}, "main", "Сохранять дубликаты при дедупликации"),
    ("dedup_tvg_check", "dedup_by_name_use_tvg", "check",
     {}, "main", "Учитывать TVG-ID при дедупликации"),
    ("apply_filters_on_open_check", "apply_filters_on_file_open",
     "check", {},
     "main", "Применять ЧС каналов и ЧС домен/IP "
             "при открытии файла"),
    ("preserve_order_check", "preserve_original_order", "check",
     {}, "main", "Сохранять оригинальный порядок каналов"),
    ("show_tvg_id_check", "show_tvg_id", "check",
     {}, "main", "Показывать TVG-ID"),
    ("show_tvg_logo_check", "show_tvg_logo", "check",
     {}, "main", "Показывать логотип"),
    ("show_catchup_check", "show_catchup", "check",
     {}, "main", "Показывать catchup"),
    ("show_status_bar_check", "show_status_bar", "check",
     {}, "main", "Показывать статус-бар"),
    ("enable_icons_check", "enable_icons", "check",
     {}, "main", "Показывать иконки"),
    ("auto_update_sources_check", "auto_update_sources", "check",
     {}, "main", "Автоматически обновлять источники"),
    ("auto_backup_check", "auto_backup_before_save", "check",
     {}, "main", "Автобэкап перед сохранением (.bak)"),
    ("allow_dup_urls_check", "allow_duplicate_urls", "check",
     {}, "main",
     "Разрешать одинаковые URL у разных каналов"),
    ("days_to_check_spin", "days_to_check", "spin",
     {"range": (0, 30)}, "main", "Дней для fallback:"),
    ("default_epg_url_edit", "default_epg_url", "line",
     {"placeholder": "http://example.com/epg.xml — "
                     "укажите свой источник EPG"},
     "main", "EPG по умолчанию (URL):"),
    ("sf_auto_load_check", "sources_finder_auto_load_after_add",
     "check", {}, "sf",
     "Сразу загружать в кэш после авто-поиска"),
    ("sf_github_check", "sources_finder_include_github", "check",
     {}, "sf", "Включать GitHub в авто-поиск"),
    ("sf_m3uguide_check", "sources_finder_include_m3uguide",
     "check", {}, "sf",
     "Включать поиск в репозитории m3u.guide "
     "через GitHub API"),
    ("sf_max_github_spin", "sources_finder_max_github", "spin",
     {"range": (1, 100)}, "sf", "Макс. GitHub-источников:"),
    ("sf_max_m3uguide_spin", "sources_finder_max_m3uguide", "spin",
     {"range": (1, 50)}, "sf",
     "Макс. m3u.guide-источников:"),
    ("sf_update_interval_spin",
     "sources_finder_update_interval_hours", "spin",
     {"range": (1, 720), "suffix": " ч"},
     "sf", "Интервал авто-обновления:"),
]


class GeneralSettingsDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Общие настройки", parent,
                          size=(520, 940))
        self.core = core
        self._widgets: Dict[str, QWidget] = {}
        self._setup_ui()

    def _setup_ui(self):
        l = self.root
        form = QFormLayout()
        c = self.core.config
        for attr, cfg_key, kind, params, _g, label in GENERAL_SPEC:
            widget = _make_widget(kind, params, label, self)
            self._widgets[attr] = widget
            _write_value(kind, widget, c.get(cfg_key))
            if kind == "check":
                form.addRow(widget)
            else:
                form.addRow(label or "", widget)
        l.addLayout(form)
        l.addWidget(hint_label(
            "Приложение не содержит встроенного EPG. Укажите "
            "свой URL в поле «EPG по умолчанию» — он будет "
            "использоваться, если в шапке плейлиста нет url-tvg."))
        self.add_ok_cancel()
        self.add_reset_button(self._reset_to_defaults)

    def _reset_to_defaults(self):
        if not confirm(self,
                        "Сбросить все общие настройки к "
                        "значениям по умолчанию?"):
            return
        for attr, cfg_key, kind, _p, _g, _l in GENERAL_SPEC:
            w = self._widgets.get(attr)
            if w is None:
                continue
            _write_value(kind, w, Config.DEFAULT.get(cfg_key))

    def apply(self) -> bool:
        c = self.core.config
        epg_url = self._widgets[
            'default_epg_url_edit'].text().strip()
        if epg_url:
            p = urlparse(epg_url)
            if p.scheme not in ('http', 'https') or not p.netloc:
                warn_box(self,
                         "EPG по умолчанию: нужен http(s):// URL")
                return False
        for attr, cfg_key, kind, _p, _g, _l in GENERAL_SPEC:
            w = self._widgets.get(attr)
            if w is None:
                continue
            c.set(cfg_key, _read_value(kind, w))
        if not c.save():
            warn_box(self, "Не удалось сохранить настройки")
            return False
        self.core.apply_auto_update_setting()
        self.core.settings_changed.emit()
        return True


CACHE_SPEC: List[Tuple[str, str, str, Dict[str, Any],
                        str, str]] = [
    ("use_check_cache_check", "use_check_result_cache", "check",
     {}, "checks", "Использовать кэш результатов проверок"),
    ("check_ttl_spin", "check_result_cache_ttl_hours", "spin",
     {"range": (1, 720), "suffix": " ч"},
     "checks", "TTL результатов:"),
    ("use_lc_check", "use_link_cache", "check",
     {}, "lc", "Использовать LinkCache"),
    ("lc_hours_spin", "link_cache_hours", "spin",
     {"range": (1, 720), "suffix": " ч"},
     "lc", "TTL:"),
    ("lc_max_files_spin", "link_cache_max_files", "spin",
     {"range": (100, 1_000_000)},
     "lc", "Макс. файлов:"),
    ("lc_max_mb_spin", "link_cache_max_mb", "spin",
     {"range": (1, 8192), "suffix": " МБ"},
     "lc", "Макс. размер:"),
    ("epg_ttl_spin", "epg_cache_ttl_hours", "spin",
     {"range": (1, 720), "suffix": " ч"},
     "epg", "TTL:"),
]


class CacheManagerDialog(BaseDialog):
    def __init__(self, core: 'ApplicationCore', parent=None):
        super().__init__("Управление кэшем", parent,
                          size=(700, 620))
        self.core = core
        self._widgets: Dict[str, QWidget] = {}
        self._setup_ui()
        self._load_settings()
        self._refresh_stats()

    def _setup_ui(self):
        layout = self.root
        self.tabs = QTabWidget()

        tabs_by_key: Dict[str, Tuple[QWidget, QVBoxLayout,
                                     QFormLayout]] = {}
        for tab_key, title in (
                ("checks", "Проверки"),
                ("lc", "LinkCache"),
                ("epg", "EPG")):
            w = QWidget()
            vl = QVBoxLayout(w)
            form = QFormLayout()
            self.tabs.addTab(w, title)
            tabs_by_key[tab_key] = (w, vl, form)

        for attr, cfg_key, kind, params, tab_key, label in CACHE_SPEC:
            widget = _make_widget(kind, params, label, self)
            self._widgets[attr] = widget
            _, _, form = tabs_by_key[tab_key]
            if kind == "check":
                form.addRow(widget)
            else:
                form.addRow(label or "", widget)

        self.checks_stats_label = QLabel()
        self.checks_stats_label.setTextFormat(
            Qt.TextFormat.RichText)
        self.checks_stats_label.setWordWrap(True)
        tabs_by_key["checks"][1].insertWidget(
            0, self.checks_stats_label)
        tabs_by_key["checks"][1].addLayout(
            tabs_by_key["checks"][2])

        self.lc_stats_label = QLabel()
        self.lc_stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.lc_stats_label.setWordWrap(True)
        tabs_by_key["lc"][1].insertWidget(0, self.lc_stats_label)
        tabs_by_key["lc"][1].addLayout(tabs_by_key["lc"][2])

        self.epg_stats_label = QLabel()
        self.epg_stats_label.setTextFormat(
            Qt.TextFormat.RichText)
        self.epg_stats_label.setWordWrap(True)
        tabs_by_key["epg"][1].insertWidget(0, self.epg_stats_label)
        tabs_by_key["epg"][1].addLayout(tabs_by_key["epg"][2])

        common = QHBoxLayout()
        self.use_cache_check = QCheckBox(
            "SQLite-кэш включён")
        self.use_cache_check.toggled.connect(
            self._on_cache_toggled)
        common.addWidget(self.use_cache_check)
        common.addStretch()
        for text, slot in (
            ("Обновить", self._refresh_stats),
            ("VACUUM", self._vacuum),
            ("Очистить всё", self._clear_all),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            common.addWidget(b)
        layout.addLayout(common)

        for tab_key, label in (
                ("checks", "Очистить кэш проверок"),
                ("checks", "Удалить устаревшие"),
                ("lc", "Очистить LinkCache"),
                ("epg", "Очистить EPG-кэш")):
            b = QPushButton(label)
            b.clicked.connect(
                lambda _=False, t=tab_key, l=label:
                    self._on_tab_action(t, l))
            tabs_by_key[tab_key][1].addWidget(b)

        for w, _, _ in tabs_by_key.values():
            w.layout().addStretch()

        layout.addWidget(self.tabs)

        bb = QDialogButtonBox(OK_CANCEL_BB)
        bb.button(
            QDialogButtonBox.StandardButton.Ok).setText("ОК")
        bb.button(
            QDialogButtonBox.StandardButton.Cancel).setText(
            "Отмена")
        bb.accepted.connect(self._save_and_accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _load_settings(self):
        c = self.core.config
        self.use_cache_check.setChecked(
            bool(c.get('use_cache_manager', True)))
        for attr, cfg_key, kind, _p, _t, _l in CACHE_SPEC:
            w = self._widgets.get(attr)
            if w is not None:
                _write_value(kind, w, c.get(cfg_key))
        self._on_cache_toggled(self.use_cache_check.isChecked())

    def _on_cache_toggled(self, enabled: bool):
        for w in self._widgets.values():
            w.setEnabled(enabled)

    def _on_tab_action(self, tab_key: str, label: str):
        if tab_key == "checks":
            if "Очистить" in label:
                self._clear_checks()
            else:
                self._cleanup_checks()
        elif tab_key == "lc":
            self._clear_link_cache()
        elif tab_key == "epg":
            self._clear_epg()

    @staticmethod
    def _fmt_mb(b: int) -> str:
        return f"{b / (1024 * 1024):.1f} МБ"

    @staticmethod
    def _fmt_int(n: int) -> str:
        return f"{n:,}".replace(',', ' ')

    def _refresh_stats(self):
        cm = self.core.cache_manager
        if cm is None:
            empty = "<i>Кэш отключён в настройках.</i>"
            for lbl in (self.checks_stats_label,
                        self.lc_stats_label,
                        self.epg_stats_label):
                lbl.setText(empty)
            return

        check_ttl = self._widgets['check_ttl_spin'].value()
        epg_ttl = self._widgets['epg_ttl_spin'].value()
        st = cm.get_stats(check_ttl, epg_ttl)
        cr = st['check_results']
        self.checks_stats_label.setText(
            f"<b>Всего записей:</b> {self._fmt_int(cr['count'])}<br>"
            f"<b>Устаревших (>{check_ttl} ч):</b> "
            f"{self._fmt_int(cr['old_count'])}<br>"
            f"<b>Размер БД:</b> "
            f"{self._fmt_mb(st['db_size_bytes'])}")
        lc = st['link_cache']
        self.lc_stats_label.setText(
            f"<b>Файлов:</b> {self._fmt_int(lc['files'])}<br>"
            f"<b>Размер:</b> {self._fmt_mb(lc['bytes'])}")
        epg = st['epg_entries']
        epg_ch = st.get('epg_channels', {'count': 0})
        self.epg_stats_label.setText(
            f"<b>Программ в БД:</b> "
            f"{self._fmt_int(epg['count'])}<br>"
            f"<b>Каналов EPG в БД:</b> "
            f"{self._fmt_int(epg_ch['count'])}")

    def _clear_checks(self):
        if self.core.cache_manager is None:
            return
        if confirm(self, "Очистить кэш проверок ссылок?"):
            self.core.cache_manager.clear()
            self._refresh_stats()

    def _cleanup_checks(self):
        if self.core.cache_manager is None:
            return
        days = max(1, self._widgets['check_ttl_spin'].value() // 24)
        n = self.core.cache_manager.cleanup_old(days)
        info_box(self, f"Удалено записей: {n}", "Готово")
        self._refresh_stats()

    def _clear_link_cache(self):
        if self.core.cache_manager is None:
            return
        if confirm(self, "Очистить LinkCache?"):
            self.core.cache_manager.clear_link_cache()
            self.core.link_source_manager.invalidate_cache()
            self._refresh_stats()

    def _clear_epg(self):
        if self.core.cache_manager is None:
            return
        if confirm(self, "Очистить EPG-кэш?"):
            self.core.cache_manager.clear_epg()
            self.core.epg_db.clear()
            self._refresh_stats()

    def _vacuum(self):
        if self.core.cache_manager is None:
            return
        try:
            self.core.cache_manager.vacuum()
            info_box(self, "База сжата", "Готово")
            self._refresh_stats()
        except Exception as e:
            logger.exception("vacuum")
            error_box(self, str(e))

    def _clear_all(self):
        if self.core.cache_manager is None:
            return
        if not confirm(
                self,
                "Очистить ВСЕ кэши? Это действие необратимо."):
            return
        cm = self.core.cache_manager
        cm.clear()
        cm.clear_link_cache()
        cm.clear_epg()
        self.core.epg_db.clear()
        self.core.link_source_manager.invalidate_cache()
        with suppress(Exception):
            from iptv_org_aliases import clear_cache
            clear_cache()
        info_box(self, "Все кэши очищены", "Готово")

    def _save_and_accept(self):
        c = self.core.config
        old_cache = bool(c.get('use_cache_manager', True))
        new_cache = self.use_cache_check.isChecked()
        old_check_ttl = int(c.get('check_result_cache_ttl_hours',
                                  CHECK_RESULT_CACHE_TTL_HOURS))
        old_epg_ttl = int(c.get('epg_cache_ttl_hours',
                                EPG_CACHE_TTL_HOURS))
        c.set('use_cache_manager', new_cache)
        for attr, cfg_key, kind, _p, _t, _l in CACHE_SPEC:
            w = self._widgets.get(attr)
            if w is not None:
                c.set(cfg_key, _read_value(kind, w))
        if not c.save():
            warn_box(self, "Не удалось сохранить настройки")
            return
        if new_cache != old_cache:
            self.core.set_cache_manager_enabled(new_cache)
        if self.core.cache_manager is not None:
            with suppress(Exception):
                self.core.cache_manager.configure_link_cache(
                    max_files=(
                        self._widgets['lc_max_files_spin'].value()),
                    max_mb=(
                        self._widgets['lc_max_mb_spin'].value()))
            new_check_ttl = int(
                self._widgets['check_ttl_spin'].value())
            new_epg_ttl = int(self._widgets['epg_ttl_spin'].value())
            try:
                if new_check_ttl < old_check_ttl:
                    removed = self.core.cache_manager.cleanup_old(
                        days=max(1, new_check_ttl // 24))
                    logger.info(
                        "TTL уменьшен, удалено записей: %d",
                        removed)
                if new_epg_ttl < old_epg_ttl:
                    self.core.cache_manager.clear_epg()
                    self.core.epg_db.clear()
            except Exception:
                logger.exception("ttl cleanup")
        self.core.settings_changed.emit()
        super().accept()