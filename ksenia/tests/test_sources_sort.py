"""Тесты tie-breaker по TTFB/resolution в LinkSourceManager."""
from unittest.mock import MagicMock

import pytest

from models import ChannelData


def _make_channel(url, ttfb_ms=None, resolution="",
                   url_status=True):
    ch = ChannelData()
    ch.link.url = url
    ch.link.has_url = True
    ch.status.url_status = url_status
    ch.status.ttfb_ms = ttfb_ms
    ch.status.resolution = resolution
    return ch


def _make_manager_with_alive(channels):
    """Создаёт LinkSourceManager с подготовленным _alive_index."""
    from sources import LinkSourceManager
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        mgr = LinkSourceManager(tmp)
    # Подменим _alive_index напрямую.
    src = MagicMock()
    src.name = "test"
    src.enabled = True
    src.priority = 5
    mgr._sources = [src]
    mgr._alive_index = {"test": {"key": channels}}
    return mgr


def test_sort_by_ttfb():
    """Быстрый URL — раньше медленного."""
    from config import Config, LinkReplacementSettings
    s = LinkReplacementSettings(Config())
    ch_fast = _make_channel("http://fast", ttfb_ms=50.0)
    ch_slow = _make_channel("http://slow", ttfb_ms=500.0)
    mgr = _make_manager_with_alive([ch_slow, ch_fast])

    urls = mgr._get_alive_urls_for_keys_locked(
        frozenset({"key"}), s, limit=10)
    assert urls == ["http://fast", "http://slow"]


def test_sort_by_resolution_when_ttfb_equal():
    """При равном TTFB лучшее разрешение — раньше."""
    from config import Config, LinkReplacementSettings
    s = LinkReplacementSettings(Config())
    ch_4k = _make_channel("http://4k", ttfb_ms=100.0,
                           resolution="4K")
    ch_720 = _make_channel("http://720", ttfb_ms=100.0,
                            resolution="720p")
    mgr = _make_manager_with_alive([ch_720, ch_4k])

    urls = mgr._get_alive_urls_for_keys_locked(
        frozenset({"key"}), s, limit=10)
    assert urls == ["http://4k", "http://720"]


def test_dead_urls_excluded():
    """url_status=False не попадает в результат."""
    from config import Config, LinkReplacementSettings
    s = LinkReplacementSettings(Config())
    ch_alive = _make_channel("http://alive", ttfb_ms=100.0)
    ch_dead = _make_channel("http://dead", url_status=False)
    mgr = _make_manager_with_alive([ch_alive, ch_dead])

    urls = mgr._get_alive_urls_for_keys_locked(
        frozenset({"key"}), s, limit=10)
    assert "http://alive" in urls
    assert "http://dead" not in urls


def test_alive_rank_primary():
    """Живые раньше неизвестных, даже если у тех TTFB меньше."""
    from config import Config, LinkReplacementSettings
    s = LinkReplacementSettings(Config())
    ch_alive = _make_channel("http://alive", ttfb_ms=500.0,
                              url_status=True)
    ch_unknown = _make_channel("http://unknown", ttfb_ms=10.0,
                                url_status=None)
    mgr = _make_manager_with_alive([ch_unknown, ch_alive])

    urls = mgr._get_alive_urls_for_keys_locked(
        frozenset({"key"}), s, limit=10)
    assert urls == ["http://alive", "http://unknown"]