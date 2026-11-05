"""Тесты интеграции Auto-Repair в LinkReplacementWorker."""
from unittest.mock import MagicMock, patch

import pytest

from models import ChannelData


def _make_worker(settings=None):
    from workers import LinkReplacementWorker
    from config import LinkReplacementSettings, Config
    cfg = Config()
    s = settings or LinkReplacementSettings(cfg)
    # Не нужен реальный LinkSourceManager для этих тестов —
    # проверяем только Auto-Repair.
    mgr = MagicMock()
    mgr.cache_manager = None
    return LinkReplacementWorker(
        channels=[], source_manager=mgr, settings=s, config=cfg)


def _make_broken_channel(url="http://example.com/x.m3u8"):
    ch = ChannelData()
    ch.meta.name = "Test"
    ch.link.url = url
    ch.link.has_url = True
    ch.status.url_status = False
    return ch


@patch("utils.URLUtils.try_repair")
def test_auto_repair_used_first(mock_repair):
    """Auto-Repair срабатывает ДО обращения к кэшу."""
    worker = _make_worker()
    ch = _make_broken_channel()
    mock_repair.return_value = "https://example.com/x.m3u8"

    result = worker._find_replacement_for(ch)

    assert result is not None
    old_url, new_url, from_alive, from_repair = result
    assert old_url == "http://example.com/x.m3u8"
    assert new_url == "https://example.com/x.m3u8"
    assert from_alive is False
    assert from_repair is True
    mock_repair.assert_called_once()


@patch("utils.URLUtils.try_repair")
def test_auto_repair_fails_falls_back_to_cache(mock_repair):
    """Если Auto-Repair не помог — идём в кэш живых URL."""
    worker = _make_worker()
    ch = _make_broken_channel()
    mock_repair.return_value = None

    # Мок кэша — возвращаем живой URL.
    worker._find_replacement = MagicMock(
        return_value=("https://cdn.example.com/good.m3u8", True))

    result = worker._find_replacement_for(ch)

    assert result is not None
    old_url, new_url, from_alive, from_repair = result
    assert new_url == "https://cdn.example.com/good.m3u8"
    assert from_alive is True
    assert from_repair is False


@patch("utils.URLUtils.try_repair")
def test_auto_repair_skips_blacklisted_url(mock_repair):
    """Заблокированные ссылки не чиним."""
    from config import Config, LinkReplacementSettings
    cfg = Config()
    s = LinkReplacementSettings(cfg)
    bl_mgr = MagicMock()
    bl_mgr.matches_url.return_value = True
    s.bind_managers(domain_blacklist_manager=bl_mgr)
    worker = _make_worker(settings=s)
    ch = _make_broken_channel()

    result = worker._try_auto_repair(ch)

    assert result is None
    mock_repair.assert_not_called()


@patch("utils.URLUtils.try_repair")
def test_auto_repair_rejects_blacklisted_result(mock_repair):
    """Если Auto-Repair вернул ЧС-домен — отбрасываем."""
    from config import Config, LinkReplacementSettings
    cfg = Config()
    s = LinkReplacementSettings(cfg)
    bl_mgr = MagicMock()
    # Исходный URL — не в ЧС, починенный — в ЧС.
    bl_mgr.matches_url.side_effect = lambda u: (
        "evil" in (u or ""))
    s.bind_managers(domain_blacklist_manager=bl_mgr)
    worker = _make_worker(settings=s)
    ch = _make_broken_channel("http://good.example/x")
    mock_repair.return_value = "https://evil.example/x"

    result = worker._try_auto_repair(ch)
    assert result is None