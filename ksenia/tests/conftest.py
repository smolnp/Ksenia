"""Общие фикстуры для тестов Ksenia M3U Editor."""
import os
import sys

import pytest

# Чтобы тесты видели модули проекта из корня.
sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def sample_channel():
    """Простой канал без URL."""
    from models import ChannelData
    ch = ChannelData()
    ch.meta.name = "Test Channel"
    ch.meta.group = "Test Group"
    return ch


@pytest.fixture
def sample_channel_with_url():
    """Канал с URL."""
    from models import ChannelData
    ch = ChannelData()
    ch.meta.name = "Test Channel"
    ch.link.url = "http://example.com/stream.m3u8"
    ch.link.has_url = True
    return ch