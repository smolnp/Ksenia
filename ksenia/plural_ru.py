# Copyright (C) 2025 SmolNP
# SPDX-License-Identifier: GPL-3.0-or-later
# См. LICENSE в корне проекта.
"""Склонение русских существительных после числительных.

Правило:
  1, 21, 31, 101, ... → "канал"   (one)
  2-4, 22-24, 32-34, ... → "канала" (few)
  0, 5-20, 25-30, 35-100, ... → "каналов" (many)

Исключение: 11-14 → "каналов" (many), даже если
последняя цифра 1-4.
"""
from __future__ import annotations


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Возвращает правильную форму существительного.

    n — целое число (может быть отрицательным — берём abs).
    one — форма для 1, 21, 31...
    few — форма для 2-4, 22-24...
    many — форма для 0, 5-20, 25-30...
    """
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    n %= 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def channels_word(n: int) -> str:
    return plural_ru(n, "канал", "канала", "каналов")


def links_word(n: int) -> str:
    return plural_ru(n, "ссылка", "ссылки", "ссылок")


def rules_word(n: int) -> str:
    return plural_ru(n, "правило", "правила", "правил")


def sources_word(n: int) -> str:
    return plural_ru(n, "источник", "источника", "источников")


def errors_word(n: int) -> str:
    return plural_ru(n, "ошибка", "ошибки", "ошибок")


def entries_word(n: int) -> str:
    return plural_ru(n, "запись", "записи", "записей")


def rows_word(n: int) -> str:
    return plural_ru(n, "строка", "строки", "строк")


def files_word(n: int) -> str:
    return plural_ru(n, "файл", "файла", "файлов")


def minutes_word(n: int) -> str:
    return plural_ru(n, "минута", "минуты", "минут")


def seconds_word(n: int) -> str:
    return plural_ru(n, "секунда", "секунды", "секунд")


def hours_word(n: int) -> str:
    return plural_ru(n, "час", "часа", "часов")


def updated_verb(n: int) -> str:
    return "обновлён" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "обновлено"


def deleted_verb(n: int) -> str:
    return "удалён" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "удалено"


def replaced_verb(n: int) -> str:
    return "заменён" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "заменено"


def added_verb(n: int) -> str:
    return "добавлен" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "добавлено"


def found_verb(n: int) -> str:
    return "найден" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "найдено"


def working_word(n: int) -> str:
    return "Работает" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "Работают"


def unchecked_word(n: int) -> str:
    return "Не проверен" if abs(int(n)) % 10 == 1 and abs(int(n)) % 100 != 11 else "Не проверены"