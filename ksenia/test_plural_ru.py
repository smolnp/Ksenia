"""Тесты plural_ru на все ключевые случаи."""
import pytest
from utils.plural_ru import (
    plural_ru, channels_word, links_word, rules_word,
    updated_verb, deleted_verb, working_word, unchecked_word,
)


@pytest.mark.parametrize("n,expected", [
    (0, "каналов"),
    (1, "канал"),
    (2, "канала"),
    (4, "канала"),
    (5, "каналов"),
    (10, "каналов"),
    (11, "каналов"),
    (12, "каналов"),
    (14, "каналов"),
    (15, "каналов"),
    (20, "каналов"),
    (21, "канал"),
    (22, "канала"),
    (24, "канала"),
    (25, "каналов"),
    (101, "канал"),
    (102, "канала"),
    (105, "каналов"),
    (111, "каналов"),
    (121, "канал"),
    (-1, "канал"),
    (-2, "канала"),
    (-5, "каналов"),
])
def test_channels_word(n, expected):
    assert channels_word(n) == expected


def test_links_word():
    assert links_word(1) == "ссылка"
    assert links_word(2) == "ссылки"
    assert links_word(5) == "ссылок"


def test_rules_word():
    assert rules_word(1) == "правило"
    assert rules_word(2) == "правила"
    assert rules_word(5) == "правил"


def test_updated_verb():
    assert updated_verb(1) == "обновлён"
    assert updated_verb(2) == "обновлено"
    assert updated_verb(5) == "обновлено"
    assert updated_verb(11) == "обновлено"
    assert updated_verb(21) == "обновлён"


def test_working_word():
    assert working_word(1) == "Работает"
    assert working_word(2) == "Работают"
    assert working_word(11) == "Работают"
    assert working_word(21) == "Работает"


def test_invalid_input():
    assert plural_ru("abc", "а", "б", "в") == "в"
    assert plural_ru(None, "а", "б", "в") == "в"