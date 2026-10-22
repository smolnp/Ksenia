# -*- coding: utf-8 -*-
"""Нормализация имён каналов.

NormMode: STRICT (только casefold) / LIGHT (без скобок) /
FULL (полная, для поиска) / CANONICAL (для записи).

ВАЖНО о транслитерации:
    - В режиме FULL (поиск) транслитерация латиницы → кириллица
      применяется: это нужно, чтобы «Perviy Kanal» и «Первый канал»
      находились как одно и то же.
    - В display_name() транслитерация НЕ применяется: официальные
      имена каналов (BBC Earth, Cartoon Network, TV BRICS, Mezzo,
      RTG, Viju TV1000...) должны оставаться на латинице.
    - Для русских каналов, записанных латиницей, используйте
      _SYNONYMS (Perviy Kanal → Первый канал) или загруженный EPG.
"""
from __future__ import annotations

import functools
import re
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Dict, Set, Tuple


class NormMode(str, Enum):
    STRICT = "strict"
    LIGHT = "light"
    FULL = "full"
    CANONICAL = "canonical"


_CYR_LO, _CYR_HI = 'а', 'я'
_HEB_LO, _HEB_HI = '\u0590', '\u05ff'
_ARM_LO, _ARM_HI = '\u0530', '\u058f'
_GEO_LO, _GEO_HI = '\u10a0', '\u10ff'
_ARA_LO, _ARA_HI = '\u0600', '\u06ff'

_PROVIDER_PREFIXES = (
    'magenta', 'sky', 'canal', 'orange', 'vtv', 'iptvx', 'lanet',
    'mts', 'beeline', 'rostelecom', 'domru', 'ttk', 'ertelecom',
)
_ORIG_SUFFIXES = ('orig', 'ориг', 'original')
_STUB_MARKERS = ('заглушка', 'no_epg', 'no epg', 'placeholder',
                 'не используется', 'test channel')

# FIX: базовые синонимы — работают даже без iptv_org_aliases.json.
# Ключи — casefold + сжатие пробелов (_raw_alias_key).
#
# ВАЖНО: словарь применяется ТОЛЬКО к точному совпадению всего
# «сырого» имени (после casefold и сжатия пробелов). Поэтому
# официальные латинские имена каналов (BBC Earth, Cartoon Network,
# Mezzo, RTG, TV BRICS, Viju TV1000 и т.п.) не затрагиваются —
# их просто нет в словаре.
_SYNONYMS: Dict[str, str] = {
    # ── Первый канал ──
    "1 канал": "Первый канал",
    "1-й канал": "Первый канал",
    "первый": "Первый канал",
    "орт": "Первый канал",
    "орт 1": "Первый канал",
    "channel one": "Первый канал",
    "channel 1": "Первый канал",
    "channel one russia": "Первый канал",
    "1tv": "Первый канал",
    "1 tv": "Первый канал",
    "perviy kanal": "Первый канал",
    "pervy kanal": "Первый канал",
    "pervyi kanal": "Первый канал",
    "pervyy kanal": "Первый канал",
    "perviy": "Первый канал",
    "pervy": "Первый канал",
    "pervyi": "Первый канал",
    "ort hd": "Первый канал HD",
    "perviy kanal hd": "Первый канал HD",
    "pervy kanal hd": "Первый канал HD",

    # ── Россия 1 ──
    "россия 1": "Россия 1",
    "россия hd": "Россия 1 HD",
    "russia 1": "Россия 1",
    "russia hd": "Россия 1 HD",
    "russia1": "Россия 1",
    "rossia 1": "Россия 1",
    "rossiya 1": "Россия 1",
    "rossia": "Россия 1",
    "rossiya": "Россия 1",
    "russia": "Россия 1",
    "rossiya 1 hd": "Россия 1 HD",
    "russia 1 hd": "Россия 1 HD",

    # ── Россия К / Культура ──
    "россия к": "Россия К",
    "russia k": "Россия К",
    "russia culture": "Россия К",
    "культура": "Россия К",
    "kultura": "Россия К",
    "rossia k": "Россия К",
    "rossiya k": "Россия К",

    # ── Россия 24 ──
    "россия 24": "Россия 24",
    "russia 24": "Россия 24",
    "rossia 24": "Россия 24",
    "rossiya 24": "Россия 24",

    # ── НТВ ──
    "нтв": "НТВ",
    "ntv": "НТВ",
    "нтв hd": "НТВ HD",
    "ntv hd": "НТВ HD",
    "ntv mir": "НТВ Мир",
    "нтв мир": "НТВ Мир",
    "ntv pravo": "НТВ Право",
    "нтв право": "НТВ Право",
    "ntv serial": "НТВ Сериал",
    "нтв сериал": "НТВ Сериал",
    "ntv hit": "НТВ Хит",
    "нтв хит": "НТВ Хит",
    "ntv style": "НТВ Стиль жизни",
    "нтв стиль": "НТВ Стиль жизни",
    "ntv belarus": "НТВ-Беларусь",
    "ntv by": "НТВ-Беларусь",

    # ── ТНТ ──
    "тнт": "ТНТ",
    "tnt": "ТНТ",
    "тнт hd": "ТНТ HD",
    "tnt hd": "ТНТ HD",
    "тнт4": "ТНТ4",
    "tnt4": "ТНТ4",
    "tnt4 international": "ТНТ4 International",
    "tnt music": "ТНТ-Music",
    "тнт music": "ТНТ-Music",
    "tnt-music": "ТНТ-Music",

    # ── СТС ──
    "стс": "СТС",
    "sts": "СТС",
    "стс love": "СТС Love",
    "sts love": "СТС Love",
    "стс kids": "СТС Kids",
    "sts kids": "СТС Kids",
    "sts kids hd": "СТС Kids HD",

    # ── РЕН ТВ ──
    "рен тв": "РЕН ТВ",
    "ren tv": "РЕН ТВ",
    "ren": "РЕН ТВ",
    "rentv": "РЕН ТВ",
    "рен тв hd": "РЕН ТВ HD",
    "ren tv hd": "РЕН ТВ HD",
    "рен тв international": "РЕН ТВ International",
    "ren tv international": "РЕН ТВ International",
    "rentv int": "РЕН ТВ International",

    # ── ТВ-3 ──
    "тв-3": "ТВ-3",
    "тв3": "ТВ-3",
    "tv3": "ТВ-3",
    "tv 3": "ТВ-3",
    "тв 3": "ТВ-3",
    "tv3 hd": "ТВ-3 HD",
    "тв3 hd": "ТВ-3 HD",

    # ── ТВЦ / ТВ Центр ──
    "твц": "ТВЦ",
    "tvc": "ТВЦ",
    "tv centr": "ТВ Центр",
    "tv centre": "ТВ Центр",
    "tv center": "ТВ Центр",
    "тв центр": "ТВ Центр",

    # ── Матч ТВ ──
    "матч тв": "Матч ТВ",
    "match tv": "Матч ТВ",
    "match": "Матч ТВ",
    "match!": "Матч ТВ",
    "match tv hd": "Матч ТВ HD",
    "match futbol 1": "Матч! Футбол 1",
    "match futbol 2": "Матч! Футбол 2",
    "match futbol 3": "Матч! Футбол 3",
    "match premer": "Матч! Премьер",
    "match premier": "Матч! Премьер",
    "match arena": "Матч! Арена",
    "match igra": "Матч! Игра",
    "match strana": "Матч! Страна",
    "match planeta": "Матч! Планета",

    # ── Мир ──
    "мир": "Мир",
    "mir": "Мир",
    "мир 24": "Мир 24",
    "mir 24": "Мир 24",
    "mir24": "Мир 24",

    # ── Звезда ──
    "звезда": "Звезда",
    "zvezda": "Звезда",
    "звезда hd": "Звезда HD",
    "zvezda hd": "Звезда HD",
    "звезда плюс": "Звезда Плюс",
    "zvezda plus": "Звезда Плюс",

    # ── Пятый канал ──
    "пятый канал": "Пятый канал",
    "5 канал": "Пятый канал",
    "channel 5": "Пятый канал",
    "5 kanal": "Пятый канал",
    "5 tv": "Пятый канал",
    "5tv": "Пятый канал",
    "pyatyy kanal": "Пятый канал",
    "pyaty kanal": "Пятый канал",

    # ── Карусель ──
    "карусель": "Карусель",
    "karusel": "Карусель",
    "carousel": "Карусель",
    "karusel int": "Карусель International",
    "карусель international": "Карусель International",

    # ── ОТР ──
    "отр": "ОТР",
    "otr": "ОТР",

    # ── РБК ──
    "рбк": "РБК",
    "rbc": "РБК",
    "rbk": "РБК",

    # ── Пятница / Суббота ──
    "пятница": "Пятница!",
    "пятница!": "Пятница!",
    "friday": "Пятница!",
    "friday!": "Пятница!",
    "пятница int": "Пятница! International",
    "friday int": "Пятница! International",
    "суббота": "Суббота!",
    "суббота!": "Суббота!",
    "subbota": "Суббота!",
    "subbota!": "Суббота!",

    # ── Спас ──
    "спас": "Спас",
    "spas": "Спас",

    # ── Ю ──
    "ю": "Ю",
    "u": "Ю",
    "yu": "Ю",

    # ── 2x2 ──
    "2х2": "2x2",
    "2x2": "2x2",
    "2 2": "2x2",

    # ── Че ──
    "че": "Че",
    "че!": "Че",
    "che": "Че",
    "che!": "Че",

    # ── Победа ──
    "победа": "Победа",
    "pobeda": "Победа",

    # ── Телекафе ──
    "телекафе": "Телекафе",
    "telecafe": "Телекафе",

    # ── Дом кино ──
    "дом кино": "Дом кино",
    "dom kino": "Дом кино",
    "domkino": "Дом кино",
    "дом кино премиум": "Дом кино Премиум",
    "dom kino premium": "Дом кино Премиум",

    # ── ТВ БРИКС ──
    "tv brics": "TV BRICS",
    "тв брикс": "TV BRICS",
    "tv brics russian": "TV BRICS",

    # ── Euronews ──
    "euronews": "Euronews",
    "евроновости": "Euronews",
    "евронюс": "Euronews",
    "euronews russian": "Euronews",
    "euronews русский": "Euronews",

    # ── RT / RTД ──
    "rt": "RT",
    "rt news": "RT News",
    "rt русский": "RT",
    "rtd": "RTД",
    "rt doc": "RTД",
    "rtд": "RTД",
    "rt документальный": "RTД",

    # ── CNN / BBC / DW ──
    "cnn": "CNN",
    "cnn international": "CNN International",
    "bbc": "BBC",
    "bbc news": "BBC News",
    "bbc world news": "BBC World News",
    "dw": "DW",
    "deutsche welle": "DW",
    "dw english": "DW English",
    "dw russian": "DW Russian",
    "dw русский": "DW Russian",

    # ── France 24 ──
    "france 24": "France 24",
    "france24": "France 24",
    "france 24 english": "France 24 English",
    "france 24 russian": "France 24 Russian",

    # ── National Geographic ──
    "national geographic": "National Geographic",
    "nat geo": "National Geographic",
    "natgeo": "National Geographic",
    "nat geo wild": "Nat Geo Wild",
    "natgeowild": "Nat Geo Wild",
    "national geographic wild": "Nat Geo Wild",

    # ── Discovery ──
    "discovery": "Discovery Channel",
    "discovery channel": "Discovery Channel",
    "discovery science": "Discovery Science",
    "discovery turbo": "Discovery Turbo",
    "animal planet": "Animal Planet",

    # ── History ──
    "history": "History",
    "history channel": "History",
    "history 2": "History 2",
    "h2": "History 2",

    # ── Viasat / Viju ──
    "viasat explore": "Viju Explore",
    "viju explore": "Viju Explore",
    "viasat history": "Viju History",
    "viju history": "Viju History",
    "viasat nature": "Viju Nature",
    "viju nature": "Viju Nature",
    "viasat sport": "Viju+ Sport",
    "viju sport": "Viju+ Sport",
    "viju+ sport": "Viju+ Sport",
    "viasat sport hd": "Viju+ Sport HD",
    "viju sport hd": "Viju+ Sport HD",

    # ── TV1000 ──
    "tv1000": "Viju TV1000",
    "tv 1000": "Viju TV1000",
    "viju tv1000": "Viju TV1000",
    "viju tv 1000": "Viju TV1000",
    "tv1000 action": "Viju TV1000 Action",
    "viju tv1000 action": "Viju TV1000 Action",
    "tv1000 russian": "Viju TV1000 Русское",
    "viju tv1000 russian": "Viju TV1000 Русское",
    "viju tv1000 русское": "Viju TV1000 Русское",
    "tv1000 romantica": "Viju TV1000 Romantica",
    "viju tv1000 romantica": "Viju TV1000 Romantica",

    # ── Paramount ──
    "paramount comedy": "Paramount Comedy",
    "paramount channel": "Paramount Channel",
    "paramount network": "Paramount Network",

    # ── Sony ──
    "sony turbo": "Sony Turbo",
    "sony sci-fi": "Sony Sci-Fi",
    "sony scifi": "Sony Sci-Fi",

    # ── Amedia ──
    "amedia 1": "Amedia 1",
    "amedia1": "Amedia 1",
    "amedia 2": "Amedia 2",
    "amedia2": "Amedia 2",
    "amedia premium": "Amedia Premium",
    "amedia premium hd": "Amedia Premium HD",
    "amedia hit": "Amedia Hit",

    # ── Кино ──
    "кино тв": "Кино ТВ",
    "kino tv": "Кино ТВ",
    "kinotv": "Кино ТВ",
    "киномикс": "Киномикс",
    "kinomiks": "Киномикс",
    "киносерия": "Киносерия",
    "kinoseria": "Киносерия",
    "киносвидание": "Киносвидание",
    "kinosvidanie": "Киносвидание",
    "киносемья": "Киносемья",
    "kinosemya": "Киносемья",
    "киноужас": "Киноужас",
    "kinouzhas": "Киноужас",
    "кино 1": "Кино 1",
    "kino 1": "Кино 1",
    "киноман": "Киноман",
    "kinoman": "Киноман",

    # ── Музыка ──
    "муз-тв": "Муз-ТВ",
    "muz tv": "Муз-ТВ",
    "muztv": "Муз-ТВ",
    "муз тв": "Муз-ТВ",
    "ru tv": "RU.TV",
    "ru.tv": "RU.TV",
    "rutv": "RU.TV",
    "тнт music": "ТНТ-Music",
    "mtv": "MTV",
    "mtv live": "MTV Live",
    "mtv 90s": "MTV 90s",
    "mtv 00s": "MTV 00s",
    "bridge tv": "Bridge TV",
    "bridge tv hd": "Bridge TV HD",
    "bridge tv deluxe": "Bridge TV Deluxe",
    "bridge tv classic": "Bridge TV Classic",
    "bridge tv rock": "Bridge TV Rock",
    "bridge tv russian hit": "Bridge TV Русский хит",
    "bridge tv шлягер": "Bridge TV Шлягер",
    "bridge schlager": "Bridge TV Шлягер",

    # ── Детские ──
    "карусель": "Карусель",
    "мульт": "Мульт",
    "mult": "Мульт",
    "мультимания": "Мультимания",
    "multimania": "Мультимания",
    "мультиландия": "Мультиландия",
    "multilandia": "Мультиландия",
    "мультимузыка": "Мультимузыка",
    "multimuzyka": "Мультимузыка",
    "детский мир": "Детский мир",
    "detsky mir": "Детский мир",
    "disney channel": "Disney Channel",
    "disney junior": "Disney Junior",
    "disney xd": "Disney XD",
    "nickelodeon": "Nickelodeon",
    "nick jr": "Nick Jr",
    "cartoon network": "Cartoon Network",
    "boomerang": "Boomerang",
    "jimjam": "JimJam",
    "tiji": "TiJi",
    "duck tv": "Duck TV",
    "ducktv": "Duck TV",
    "ani": "Ani",
    "мама": "Мама",
    "mama": "Мама",
    "радость моя": "Радость моя",
    "radost moya": "Радость моя",
    "радость моя hd": "Радость моя HD",

    # ── Христианские ──
    "союз": "Союз",
    "soyuz": "Союз",
    "союз hd": "Союз HD",
    "надежда": "Надежда",
    "nadezhda": "Надежда",
    "спас": "Спас",
    "spas": "Спас",
    "музсоюз": "МузСоюз",
    "muzsoyuz": "МузСоюз",

    # ── Познавательные ──
    "моя планета": "Моя Планета",
    "moya planeta": "Моя Планета",
    "наука": "Наука",
    "nauka": "Наука",
    "наука 2.0": "Наука 2.0",
    "nauka 2.0": "Наука 2.0",
    "живая планета": "Живая планета",
    "zhivaya planeta": "Живая планета",
    "365 дней": "365 дней ТВ",
    "365 dney": "365 дней ТВ",
    "история": "История",
    "istoriya": "История",
    "телепутешествия": "Телепутешествия",
    "teleputeshestviya": "Телепутешествия",
    "телепутешествия hd": "Телепутешествия HD",

    # ── Спорт ──
    "матч тв": "Матч ТВ",
    "match tv": "Матч ТВ",
    "матч футбол 1": "Матч! Футбол 1",
    "match futbol 1": "Матч! Футбол 1",
    "матч футбол 2": "Матч! Футбол 2",
    "match futbol 2": "Матч! Футбол 2",
    "матч футбол 3": "Матч! Футбол 3",
    "match futbol 3": "Матч! Футбол 3",
    "матч премьер": "Матч! Премьер",
    "match premer": "Матч! Премьер",
    "матч арена": "Матч! Арена",
    "match arena": "Матч! Арена",
    "матч игра": "Матч! Игра",
    "match igra": "Матч! Игра",
    "матч страна": "Матч! Страна",
    "match strana": "Матч! Страна",
    "матч планета": "Матч! Планета",
    "match planeta": "Матч! Планета",
    "футбол": "Футбол",
    "futbol": "Футбол",
    "футбол hd": "Футбол HD",
    "futbol hd": "Футбол HD",
    "кхл": "КХЛ",
    "khl": "КХЛ",
    "кхл hd": "КХЛ HD",
    "khl hd": "КХЛ HD",
    "кхл прайм": "КХЛ Прайм",
    "khl prime": "КХЛ Прайм",

    # ── Развлекательные ──
    "домашний": "Домашний",
    "domashniy": "Домашний",
    "домашний hd": "Домашний HD",
    "domashniy hd": "Домашний HD",
    "домашний international": "Домашний International",
    "domashniy international": "Домашний International",
    "перец": "Перец",
    "perets": "Перец",
    "перец int": "Перец International",
    "perets int": "Перец International",
    "ю": "Ю",
    "u": "Ю",
    "ю hd": "Ю HD",
    "u hd": "Ю HD",
    "продвижение": "Продвижение",
    "prodvizhenie": "Продвижение",
    "сарафан": "Сарафан",
    "sarafan": "Сарафан",
    "твое тв": "Твоё ТВ",
    "tvoe tv": "Твоё ТВ",
    "твое тв hd": "Твоё ТВ HD",
    "tvoe tv hd": "Твоё ТВ HD",
    "телекафе": "Телекафе",
    "telecafe": "Телекафе",

    # ── Кино и сериалы ──
    "еврокино": "Еврокино",
    "evrokino": "Еврокино",
    "иллюзион +": "Иллюзион+",
    "illuzion+": "Иллюзион+",
    "русский иллюзион": "Русский Иллюзион",
    "russkiy illuzion": "Русский Иллюзион",
    "русский роман": "Русский роман",
    "russkiy roman": "Русский роман",
    "русский бестселлер": "Русский бестселлер",
    "russkiy bestseller": "Русский бестселлер",
    "русский детектив": "Русский детектив",
    "russkiy detektiv": "Русский детектив",
    "русская комедия": "Русская комедия",
    "russkaya komediya": "Русская комедия",
    "советское кино": "Советское кино",
    "sovetskoe kino": "Советское кино",
    "кинопремьера": "Кинопремьера",
    "kinopremyera": "Кинопремьера",
    "кинопоказ": "Кинопоказ",
    "kinopokaz": "Кинопоказ",
    "киносезон": "КиноСезон",
    "kinosezon": "КиноСезон",

    # ── Хобби ──
    "охота и рыбалка": "Охота и рыбалка",
    "okhota i rybalka": "Охота и рыбалка",
    "охота и рыбалка hd": "Охота и рыбалка HD",
    "охотник и рыболов": "Охотник и рыболов",
    "okhotnik i rybolov": "Охотник и рыболов",
    "охотник и рыболов hd": "Охотник и рыболов HD",
    "дикий": "Дикий",
    "dikiy": "Дикий",
    "дикий hd": "Дикий HD",
    "dikiy hd": "Дикий HD",
    "загородная жизнь": "Загородная жизнь",
    "zagorodnaya zhizn": "Загородная жизнь",
    "загородный": "Загородный",
    "zagorodny": "Загородный",
    "доктор": "Доктор",
    "doktor": "Доктор",
    "драйв": "Драйв",
    "drayv": "Драйв",
    "drive": "Драйв",
    "еда": "Еда",
    "eda": "Еда",
    "кухня тв": "Кухня ТВ",
    "kukhnya tv": "Кухня ТВ",
    "kuhnya tv": "Кухня ТВ",
    "мужской": "Мужской",
    "muzhskoy": "Мужской",
    "оружие": "Оружие",
    "oruzhie": "Оружие",
    "поехали!": "Поехали!",
    "poekhali": "Поехали!",

    # ── Авто ──
    "авто 24": "Авто24",
    "avto 24": "Авто24",
    "авто24": "Авто24",
    "avto24": "Авто24",
    "авто плюс": "Авто Плюс",
    "avto plus": "Авто Плюс",

    # ── Информационные ──
    "известия": "Известия",
    "izvestiya": "Известия",
    "известия hd": "Известия HD",
    "izvestiya hd": "Известия HD",
    "рбк": "РБК",
    "rbk": "РБК",
    "рбк hd": "РБК HD",
    "rbk hd": "РБК HD",
    "рбк 2": "РБК 2",
    "rbk 2": "РБК 2",
    "россия 24": "Россия 24",
    "rossiya 24": "Россия 24",
    "мир 24": "Мир 24",
    "mir 24": "Мир 24",
    "беларусь 24": "Беларусь 24",
    "belarus 24": "Беларусь 24",
    "вместе-рф": "Вместе-РФ",
    "vmeste rf": "Вместе-РФ",
    "соловьев live": "Соловьев.Live",
    "soloviev live": "Соловьев.Live",
    "соловьёв live": "Соловьев.Live",
    "конгресс тв": "Конгресс ТВ",
    "kongress tv": "Конгресс ТВ",
    "красная линия": "Красная линия",
    "krasnaya liniya": "Красная линия",
    "крым 24": "Крым 24",
    "krym 24": "Крым 24",
    "крым24": "Крым 24",
    "krym24": "Крым 24",
    "тaсс": "ТАСС",
    "tass": "ТАСС",
    "так": "ТАСС",

    # ── Региональные (частые) ──
    "360 новости": "360° Новости",
    "360 novosti": "360° Новости",
    "москва 24": "Москва 24",
    "moskva 24": "Москва 24",
    "москва-24": "Москва 24",
    "moskva-24": "Москва 24",
    "петербург": "Санкт-Петербург",
    "peterburg": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург",
    "sankt-peterburg": "Санкт-Петербург",
    "москва доверие": "Москва Доверие",
    "moskva doverie": "Москва Доверие",
}

# Транслитерация латиница→кириллица.
# ВНИМАНИЕ: используется ТОЛЬКО в NormMode.FULL (для поиска),
# НЕ используется в display_name() — чтобы не портить официальные
# латинские имена каналов (BBC Earth, Cartoon Network, Mezzo...).
_DISPLAY_TRANSLIT_PAIRS = (
    ('shch', 'щ'), ('sch', 'щ'), ('zh', 'ж'), ('kh', 'х'),
    ('ts', 'ц'), ('ch', 'ч'), ('sh', 'ш'), ('yu', 'ю'),
    ('ya', 'я'), ('yo', 'ё'), ('ye', 'е'),
    ('a', 'а'), ('b', 'б'), ('v', 'в'), ('g', 'г'), ('d', 'д'),
    ('e', 'е'), ('z', 'з'), ('i', 'и'), ('y', 'й'), ('k', 'к'),
    ('l', 'л'), ('m', 'м'), ('n', 'н'), ('o', 'о'), ('p', 'п'),
    ('r', 'р'), ('s', 'с'), ('t', 'т'), ('u', 'у'), ('f', 'ф'),
    ('h', 'х'), ('c', 'к'), ('w', 'в'), ('x', 'кс'),
)

_TRANSLIT = {
    "ren": "рен", "sts": "стс", "tnt": "тнт", "ntv": "нтв",
    "russia": "россия", "match": "матч", "mir": "мир",
    "kultura": "культура", "zvezda": "звезда", "kino": "кино",
    "tv3": "тв3", "che": "че", "tvc": "твц", "tv": "тв",
    "news": "новости", "sport": "спорт", "music": "музыка",
    "kids": "детский", "science": "наука", "history": "история",
    "friday": "пятница", "spas": "спас", "subbota": "суббота",
    "pobeda": "победа", "telecafe": "телекафе", "dom": "дом",
    "rbk": "рбк", "rbc": "рбк",
    "pervy": "первый", "perviy": "первый", "channel": "канал",
    "one": "один", "rossia": "россия", "rossiya": "россия",
    "russian": "русский", "russkiy": "русский",
    "travel": "путешествия", "adventure": "приключения",
    "nature": "природа", "planet": "планета", "world": "мир",
    "auto": "авто", "drive": "драйв", "zoo": "зоо",
    "family": "семья", "cinema": "кино", "comedy": "комедия",
    "drama": "драма", "series": "сериалы", "serial": "сериал",
    "premium": "премиум", "classic": "классика",
    "hit": "хит", "hits": "хиты", "gold": "голд",
}

# ── Качество ──

_QUALITY_PARENS = re.compile(
    r'\(\s*(?:720|1080|1280|2160|4K|8K|UHD|FHD|FullHD|'
    r'Full\s+HD|Ultra\s+HD|HD|SD|HDR|HDR10|DV|HEVC|H\.?265|'
    r'H\.?264|x265|x264|VP9|AV1|AC3|AAC|HQ|LQ|SQ)'
    r'\s*(?:[pPiI]\d*|\d{2,3}fps)?\s*\)',
    re.IGNORECASE | re.UNICODE)

_QUALITY_BRACKETS = re.compile(
    r'\[\s*(?:720|1080|1280|2160|4K|8K|UHD|FHD|FullHD|'
    r'HD|SD|HDR|HEVC|H\.?265|H\.?264|x265|x264|HQ|LQ|SQ)'
    r'\s*(?:[pPiI]\d*|\d{2,3}fps)?\s*\]',
    re.IGNORECASE | re.UNICODE)

_QUALITY_WORD_END = re.compile(
    r'\s+(?:720p|1080p|1080i|2160p|4K|8K|UHD|FHD|FullHD|'
    r'HEVC|H\.?265|H\.?264|x265|x264|HDR10|HDR|DV)\s*$',
    re.IGNORECASE | re.UNICODE)

_QUALITY_PARENS_TAG = re.compile(
    r'\(\s*(?P<tag>720[pPiI]?|1080[pPiI]?|1280[pPiI]?|2160[pPiI]?|'
    r'480[pPiI]?|360[pPiI]?|576[pPiI]?|540[pPiI]?|'
    r'4K|8K|UHD|FHD|FullHD|Full\s+HD|Ultra\s+HD|'
    r'HD|SD|HDR10|HDR|DV|HEVC|H\.?265|H\.?264|x265|x264|'
    r'VP9|AV1|AC3|AAC|HQ|LQ|SQ)'
    r'\s*(?:[pPiI]\d*|\d{2,3}fps)?\s*\)',
    re.IGNORECASE | re.UNICODE)

_QUALITY_BRACKETS_TAG = re.compile(
    r'\[\s*(?P<tag>720[pPiI]?|1080[pPiI]?|2160[pPiI]?|'
    r'480[pPiI]?|360[pPiI]?|576[pPiI]?|540[pPiI]?|'
    r'4K|8K|UHD|FHD|FullHD|HD|SD|HDR|'
    r'HEVC|H\.?265|H\.?264|x265|x264|HQ|LQ|SQ)'
    r'\s*(?:[pPiI]\d*|\d{2,3}fps)?\s*\]',
    re.IGNORECASE | re.UNICODE)

_QUALITY_WORD_END_TAG = re.compile(
    r'\s+(?P<tag>720p|1080p|1080i|2160p|480p|360p|576p|540p|'
    r'4K|8K|UHD|FHD|FullHD|HEVC|H\.?265|H\.?264|x265|x264|'
    r'HDR10|HDR|DV)\s*$',
    re.IGNORECASE | re.UNICODE)

_REGION_BRACKETS = re.compile(
    r'[\(\[\{]\s*(?:RU|UA|BY|KZ|EN|US|UK|GB|DE|FR|ES|IT|PL|TR|'
    r'CZ|NL|SE|PT|GR|IL|AM|GE|AZ|MD|LV|LT|EE|RO|BG|HU|SK|HR|'
    r'RS|SI|JP|KR|CN|IN|BR|AR|MX|CA|AU|NZ|EU|CIS|CEE|MENA)'
    r'\s*[\)\]\}]',
    re.IGNORECASE | re.UNICODE)

_REGION_WORD_END = re.compile(
    r'\s+(?:RU|UA|BY|KZ|EN|US|UK|DE|FR|ES|IT|PL|TR|IL|AM|GE|AZ|'
    r'MD|LV|LT|EE|RO|BG|HU|CZ|NL|SE|PT|GR)\s*$',
    re.IGNORECASE | re.UNICODE)

_SPECIAL_CHARS = re.compile(r'[^\w\s\-]', re.UNICODE)
_WS_MULTI = re.compile(r'\s+')

_EMOJI = re.compile(
    '[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF'
    '\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF'
    '\U00002702-\U000027B0\U000024C2-\U0001F251'
    '\U0001F900-\U0001F9FF]+',
    flags=re.UNICODE)

_DECORATIVE = re.compile(
    '['
    '\u24B6-\u24E9'
    '\u24EA-\u24FF'
    '\u2460-\u2473'
    '\u2100-\u214F'
    '\u2122\u00AE\u00A9'
    '\u2600-\u26FF'
    '\u2700-\u27BF'
    '\u2B00-\u2BFF'
    '\u25A0-\u25FF'
    '\u2756-\u275F'
    ']+',
    flags=re.UNICODE)

_JUNK_BRACKETS = re.compile(
    r'[\(\[\{]\s*(?:Backup|Alt(?:ernative)?|Test|Temp(?:orary)?|'
    r'Резерв|Тест|Временн(?:ый|ая)|'
    r'Not\s+24[/x]?7|24/7|не\s+24[/x]?7|н[её]\s*24[/x]?7|'
    r'Geo[-\s]?blocked|Blocked|Restricted|SD|HD|FHD)'
    r'\s*\d*\s*[\)\]\}]',
    re.IGNORECASE | re.UNICODE)

_SHIFT_PARENS = re.compile(r'\s*\(\s*\+\s*(\d+)\s*\)\s*$')
_SHIFT_PLUS = re.compile(r'\s+\+\s*(\d+)\s*$')
_SHIFT_DASH = re.compile(r'\s*-\s*\(\s*(\d+)\s*\)\s*$')

_MOJIBAKE_CHARS = frozenset(
    'ÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ'
    '–—√©®')


# ── Базовые утилиты ──

def _script_ratio(s: str, lo: str, hi: str) -> float:
    if not s:
        return 0.0
    return sum(1 for c in s if lo <= c <= hi) / len(s)


@functools.lru_cache(maxsize=8192)
def fix_encoding(text: str) -> str:
    if not text:
        return text
    suspicious = sum(1 for c in text if c in _MOJIBAKE_CHARS)
    if suspicious == 0:
        return text
    if suspicious / max(1, len(text)) < 0.05:
        return text
    original = _script_ratio(text, _CYR_LO, _CYR_HI)
    best, best_ratio = text, original
    for enc in ('cp1251', 'cp866', 'latin1'):
        try:
            fixed = text.encode(enc).decode('utf-8', errors='strict')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not fixed or any(ord(c) >= 0x10000 for c in fixed):
            continue
        r = _script_ratio(fixed, _CYR_LO, _CYR_HI)
        if r > best_ratio:
            best, best_ratio = fixed, r
    return best


@functools.lru_cache(maxsize=4096)
def _strip_provider_prefix(text: str) -> str:
    if not text:
        return text
    s = text.strip()
    for open_c, close_c in (('[', ']'), ('(', ')'), ('{', '}')):
        if s.startswith(open_c):
            end = s.find(close_c)
            if end > 0:
                tag = s[1:end].strip().lower()
                if any(p in tag for p in _PROVIDER_PREFIXES):
                    return s[end + 1:].strip()
    m = re.match(r'^([\w\-]+)\s*[-:]\s+(.+)$', s)
    if m and m.group(1).strip().lower() in _PROVIDER_PREFIXES:
        return m.group(2).strip()
    return s


@functools.lru_cache(maxsize=4096)
def _strip_orig_suffix(text: str) -> str:
    if not text:
        return text
    s = text.strip()
    low = s.casefold()
    for suf in _ORIG_SUFFIXES:
        for sep in (' ', '-', '_', '  '):
            tail = sep + suf
            if low.endswith(tail):
                return s[:len(s) - len(tail)].strip()
    return s


def strip_provider_prefix(text: str) -> str:
    return _strip_provider_prefix(text)


def strip_orig_suffix(text: str) -> str:
    return _strip_orig_suffix(text)


def is_stub_name(text: str) -> bool:
    if not text:
        return False
    low = text.casefold()
    return any(m in low for m in _STUB_MARKERS)


def is_stub_tvg_id(tvg_id: str) -> bool:
    if not tvg_id:
        return True
    t = tvg_id.strip().lower()
    if t.startswith('no_epg'):
        return True
    if '://' in t or t.startswith('[') or t.startswith('www.'):
        return True
    return False


def extract_shift(name: str) -> Tuple[str, str | None]:
    if not name:
        return "", None
    m = _SHIFT_PARENS.search(name)
    if m:
        return name[:m.start()].strip(), f"+{m.group(1)}"
    m = _SHIFT_PLUS.search(name)
    if m:
        return name[:m.start()].strip(), f"+{m.group(1)}"
    return name.strip(), None


def extract_dash_tail(name: str) -> Tuple[str, str | None]:
    if not name:
        return "", None
    m = _SHIFT_DASH.search(name)
    if m:
        return name[:m.start()].strip(), f"-{m.group(1)}"
    return name.strip(), None


def transliterate_tokens(text: str) -> str:
    if not text:
        return text
    return " ".join(_TRANSLIT.get(w.casefold(), w)
                    for w in text.split())


def _transliterate_word(word: str) -> str:
    """Транслитерирует одно слово латиница → кириллица.

    Сохраняет регистр первой буквы. Не трогает слова с цифрами
    (обычно это технические обозначения: HD, 4K, TV21).
    """
    if not word or not word[0].isalpha():
        return word
    if not all(c.isalpha() or c in "-'" for c in word):
        return word
    if not all(ord(c) < 128 for c in word):
        return word
    low = word.casefold()
    out: list[str] = []
    i = 0
    while i < len(low):
        matched = False
        for src, dst in _DISPLAY_TRANSLIT_PAIRS:
            if low.startswith(src, i):
                out.append(dst)
                i += len(src)
                matched = True
                break
        if not matched:
            out.append(low[i])
            i += 1
    result = "".join(out)
    if word[0].isupper():
        result = result[:1].upper() + result[1:]
    return result


def _transliterate_latin_fallback(text: str) -> str:
    """Транслитерация латиницы → кириллица.

    ВНИМАНИЕ: НЕ ИСПОЛЬЗУЙТЕ для отображения имён каналов!
    Официальные имена (BBC Earth, Cartoon Network, TV BRICS,
    Mezzo, RTG, Viju TV1000...) будут испорчены:
        BBC Earth       → Ббк Еартх
        Cartoon Network → Картоон Нетворк
        TV BRICS        → Тв Брикс

    Использовать ТОЛЬКО если точно известно, что имя — транслит
    русского названия (например, для CLI-экспорта без EPG и без
    _SYNONYMS). Для отображения используйте display_name() —
    он берёт каноническое имя из EPG или _SYNONYMS.
    """
    if not text:
        return text
    cyr = sum(1 for c in text if _CYR_LO <= c.casefold() <= _CYR_HI)
    lat = sum(1 for c in text if 'a' <= c.casefold() <= 'z')
    if cyr >= lat:
        return text
    parts = re.split(r'(\s+)', text)
    out: list[str] = []
    for p in parts:
        if not p or p.isspace():
            out.append(p)
            continue
        out.append(_transliterate_word(p))
    return "".join(out)


def _raw_alias_key(text: str) -> str:
    """Ключ для поиска в алиасах/синонимах: casefold + сжатие."""
    if not text:
        return ""
    return _WS_MULTI.sub(' ', text.strip()).casefold()


def _apply_synonyms(s: str) -> str:
    """Подставляет канон из _SYNONYMS, если найдено.

    Применяется к «сырому» s ДО нормализации качества.
    Возвращает канон в правильном регистре.

    ВАЖНО: применяется ТОЛЬКО к точному совпадению всего имени
    (после casefold и сжатия пробелов). Поэтому «Cartoon Network»
    не превратится в «Картоон Нетворк» — его просто нет в словаре.
    """
    if not s:
        return s
    key = _raw_alias_key(s)
    canon = _SYNONYMS.get(key)
    if canon:
        return canon
    return s


def _apply_iptv_org_aliases(s: str) -> str:
    """Подставляет канон из алиасов iptv-org, если найдено.

    Применяется к «сырому» s ДО нормализации качества.
    """
    if not s:
        return s
    try:
        import iptv_org_aliases as _ioa
        if getattr(getattr(_ioa, "_building", None), "value", False):
            return s
        aliases = _ioa.get_aliases()
        if not aliases:
            return s
        key = _raw_alias_key(s)
        canon = aliases.get(key)
        if canon:
            return canon
    except Exception:
        pass
    return s


def _strip_decorative(text: str) -> str:
    """Убирает декоративные маркеры (Ⓢ, ™, ★, эмодзи и т.п.) везде."""
    if not text:
        return text
    s = _DECORATIVE.sub(' ', text)
    s = _EMOJI.sub(' ', s)
    return _WS_MULTI.sub(' ', s).strip()


# ── Нормализация качества (для display_name) ──

def _classify_quality_tag(tag: str) -> str:
    """Классифицирует тег качества: 'HD' / 'SD' / '4K' / '' (удалить)."""
    if not tag:
        return ""
    t = tag.strip().casefold()
    if (t in ('4k', '8k', 'uhd', 'ultra hd',
              '2160p', '2160i', '4320p', '4320i')
            or t.startswith('2160') or t.startswith('4320')):
        return "4K" if t in ('4k', '8k', 'uhd', 'ultra hd') \
            or t.startswith('2160') or t.startswith('4320') else ""
    if (t in ('hd', 'fhd', 'fullhd', 'full hd')
            or t.startswith('1080') or t.startswith('720')
            or t.startswith('1280')):
        return "HD"
    if t in ('sd',) or t.startswith('480') or t.startswith('360'):
        return "SD"
    if t.startswith('576') or t.startswith('540'):
        return ""
    if t in ('hdr', 'hdr10', 'dv', 'hevc', 'h.265', 'h.264',
             'x265', 'x264', 'vp9', 'av1', 'ac3', 'aac',
             'hq', 'lq', 'sq'):
        return ""
    return ""


def _normalize_quality_tag_display(s: str) -> str:
    """Заменяет теги качества на HD/SD/4K (или удаляет)."""
    if not s:
        return s

    def _repl_parens(m: re.Match) -> str:
        tag = m.group('tag') or ''
        cls = _classify_quality_tag(tag)
        return f" {cls} " if cls else " "

    def _repl_brackets(m: re.Match) -> str:
        tag = m.group('tag') or ''
        cls = _classify_quality_tag(tag)
        return f" {cls} " if cls else " "

    def _repl_word(m: re.Match) -> str:
        tag = m.group('tag') or ''
        cls = _classify_quality_tag(tag)
        return f" {cls}" if cls else ""

    s = _QUALITY_PARENS_TAG.sub(_repl_parens, s)
    s = _QUALITY_BRACKETS_TAG.sub(_repl_brackets, s)
    s = _QUALITY_WORD_END_TAG.sub(_repl_word, s)
    return _WS_MULTI.sub(' ', s).strip()


# ── search_key (FULL, агрессивная) ──

@functools.lru_cache(maxsize=4096)
def _normalize_impl(text: str, mode: str,
                    remove_parentheses: bool,
                    remove_brackets: bool,
                    remove_emojis: bool,
                    remove_special: bool,
                    use_aliases: bool) -> str:
    """Агрессивная нормализация — для поиска (search_key).

    ВСЕГДА удаляет HD/SD/4K/сдвиг, чтобы поиск был максимально
    широким. Для отображения используйте display_name().
    """
    if not text:
        return ""
    s = fix_encoding(text)

    if use_aliases and mode == NormMode.FULL.value:
        s = _apply_synonyms(s)
        s = _apply_iptv_org_aliases(s)

    if remove_emojis and mode != NormMode.STRICT.value:
        s = _EMOJI.sub('', s)
    if mode in (NormMode.FULL.value, NormMode.CANONICAL.value):
        s = _strip_provider_prefix(s)
        s = _strip_orig_suffix(s)
    if remove_parentheses and mode in (NormMode.LIGHT.value,
                                       NormMode.FULL.value):
        s = _QUALITY_PARENS.sub('', s)
    if remove_brackets and mode in (NormMode.LIGHT.value,
                                    NormMode.FULL.value):
        s = _QUALITY_BRACKETS.sub('', s)
        s = _JUNK_BRACKETS.sub('', s)
        s = _REGION_BRACKETS.sub('', s)
    if mode == NormMode.FULL.value and remove_special:
        s = _QUALITY_WORD_END.sub('', s)
        s = _REGION_WORD_END.sub('', s)
        s = _SPECIAL_CHARS.sub(' ', s)
    s = _WS_MULTI.sub(' ', s).strip()
    if mode in (NormMode.FULL.value, NormMode.LIGHT.value,
                NormMode.STRICT.value):
        s = s.casefold()
    if mode == NormMode.FULL.value and s:
        try:
            s = transliterate_tokens(s)
        except Exception:
            pass
    return s


def normalize(text: str, mode: str = NormMode.FULL.value) -> str:
    return _normalize_impl(text or "", mode, True, True, True,
                            True, True)


def normalize_with_settings(text: str, settings: Any,
                            mode: NormMode = NormMode.FULL) -> str:
    if settings is None:
        return _normalize_impl(text or "", mode.value, True, True,
                                True, True, True)
    try:
        rp = bool(getattr(settings,
                          'remove_parentheses_in_names', True))
        rb = bool(getattr(settings,
                          'remove_brackets_in_names', True))
        re_ = bool(getattr(settings, 'remove_emojis_in_names', True))
        rs = bool(getattr(settings,
                          'ignore_special_chars_in_names', True))
        ua = bool(getattr(settings, 'use_iptv_org_aliases', True))
    except Exception:
        return _normalize_impl(text or "", mode.value, True, True,
                                True, True, True)
    return _normalize_impl(text or "", mode.value, rp, rb, re_,
                            rs, ua)


# ── display_name (для показа) ──

def _smart_title(s: str) -> str:
    """Приводит к 'Первый канал', но HD/SD/4K/UHD/FHD — в верхнем."""
    if not s:
        return s
    acronyms = frozenset({
        'HD', 'FHD', 'UHD', 'SD', 'HDR', 'HDR10', 'DV', 'TV', 'IPTV',
        'RU', 'UA', 'BY', 'KZ', 'EN', 'US', 'UK', 'DE', 'FR', 'ES',
        'IT', 'PL', 'TR', '4K', '8K'})
    result = []
    for p in s.split(' '):
        if not p:
            continue
        if p.upper() in acronyms or (p.isupper() and len(p) > 1):
            result.append(p.upper())
        elif len(p) == 1:
            result.append(p.upper())
        else:
            sub = p.split('-')
            if len(sub) > 1:
                result.append('-'.join(
                    w[:1].upper() + w[1:].lower() if w else w
                    for w in sub))
            else:
                result.append(p[:1].upper() + p[1:].lower())
    return ' '.join(result)


def _normalize_display_impl(text: str) -> str:
    """Нормализация для показа: убрать провайдер/Orig/мусор,
    качество → HD/SD/4K, сдвиг сохранить, регистр → Title Case.

    FIX: НЕ транслитерирует латиницу в кириллицу автоматически.
    Официальные имена каналов (BBC Earth, Cartoon Network, TV BRICS,
    Mezzo, RTG, Viju TV1000...) должны оставаться на латинице.
    Для русских каналов, записанных латиницей, используйте
    _SYNONYMS (Perviy Kanal → Первый канал) или загруженный EPG.
    """
    if not text:
        return ""
    s = fix_encoding(text)

    s = _apply_synonyms(s)
    s = _apply_iptv_org_aliases(s)

    s = _strip_decorative(s)

    base, shift = extract_shift(s)

    base = _strip_provider_prefix(base)
    base = _strip_orig_suffix(base)

    base = _JUNK_BRACKETS.sub(' ', base)
    base = _REGION_BRACKETS.sub(' ', base)

    base = _normalize_quality_tag_display(base)
    base = _REGION_WORD_END.sub('', base)

    base = _SPECIAL_CHARS.sub(' ', base)
    base = _WS_MULTI.sub(' ', base).strip()

    base = _smart_title(base)

    # НЕ транслитерируем: официальные латинские имена должны
    # остаться как есть. См. docstring выше.

    if shift:
        base = f"{base} {shift}".strip()

    return base


def _epg_fuzzy_params(settings: Any) -> Tuple[float, int, float]:
    """Вытаскивает параметры fuzzy-поиска EPG из настроек.

    Раньше display_name() жёстко использовал fuzzy_threshold=1.0,
    из-за чего матчинг по «сырым» именам источников не работал.
    """
    if settings is None:
        return 1.0, 5, 0.05
    try:
        enabled = bool(getattr(settings,
                                'epg_fuzzy_match_enabled', True))
        threshold = (float(getattr(settings,
                                    'epg_fuzzy_threshold', 0.85))
                     if enabled else 1.0)
        min_len = int(getattr(settings,
                                'epg_fuzzy_min_length', 5))
        min_gap = float(getattr(settings,
                                 'epg_fuzzy_min_gap', 0.05))
        return threshold, min_len, min_gap
    except Exception:
        return 1.0, 5, 0.05


def display_name(name: str, settings: Any = None,
                 epg_db: Any = None,
                 tvg_id: str = "",
                 tvg_name: str = "",
                 alternative_names: Any = None) -> str:
    """Каноническое имя для показа.

    1. Если EPG загружен и для канала есть match — используем
       EPG display-name (вариант A: всегда с EPG, если доступен).
    2. Иначе — обычная нормализация без транслитерации.
    3. _SYNONYMS применяется внутри нормализации к точному
       совпадению «сырого» имени — это позволяет превратить
       «Perviy Kanal» в «Первый канал», но НЕ трогает
       «Cartoon Network», «BBC Earth» и другие официальные
       латинские имена.

    FIX: fuzzy-порог берётся из настроек, а не 1.0.
    FIX: передаются alternative_names для повторного матчинга
    после первого применения EPG.
    """
    if not name and not tvg_id:
        return ""
    base = _normalize_display_impl(name or "")

    if epg_db is not None:
        try:
            if getattr(epg_db, 'has_channel_info', False):
                from models import ChannelData
                tmp = ChannelData()
                tmp.meta.name = name or ""
                tmp.meta.tvg_id = tvg_id or ""
                tmp.meta.tvg_name = tvg_name or ""
                if alternative_names:
                    tmp.meta.epg_alternative_names = list(
                        alternative_names)
                fuzzy_threshold, fuzzy_min_len, fuzzy_min_gap = \
                    _epg_fuzzy_params(settings)
                info = epg_db.find_channel_info(
                    tmp,
                    fuzzy_threshold=fuzzy_threshold,
                    fuzzy_min_length=fuzzy_min_len,
                    fuzzy_min_gap=fuzzy_min_gap,
                    settings=settings)
                if info is not None and info.display_name:
                    return info.display_name
        except Exception:
            pass
    return base


# ── canonical_key (для stable_key, НЕ для склейки) ──

def canonical_key(name: str, settings: Any = None,
                  tvg_id: str = "", keep_shift: bool = False
                  ) -> str:
    """Ключ для stable_state.json. Агрессивная нормализация."""
    base, shift = extract_shift(name or "")
    base, _ = extract_dash_tail(base)
    norm = normalize_with_settings(base, settings, NormMode.FULL)
    if keep_shift and shift:
        norm = f"{norm}|{shift}"
    if tvg_id and not is_stub_tvg_id(tvg_id):
        return f"{norm}|{tvg_id.strip().lower()}"
    return norm


# ── similarity / jaccard ──

def jaccard(a: str, b: str, settings: Any = None) -> float:
    if not a or not b:
        return 0.0
    if settings is not None:
        a = normalize_with_settings(a, settings, NormMode.FULL)
        b = normalize_with_settings(b, settings, NormMode.FULL)
    sa = set(re.findall(r'\w+', a.casefold()))
    sb = set(re.findall(r'\w+', b.casefold()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def similarity(a: str, b: str, settings: Any = None) -> float:
    na = normalize_with_settings(a, settings, NormMode.FULL) if a else ""
    nb = normalize_with_settings(b, settings, NormMode.FULL) if b else ""
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    return 0.6 * seq + 0.4 * jaccard(na, nb, settings=None)


# ── apply_case_mode ──

_ACRONYMS = frozenset({
    'HD', 'FHD', 'UHD', 'SD', 'HDR', 'HDR10', 'DV', 'TV', 'IPTV',
    'RU', 'UA', 'BY', 'KZ', 'EN', 'US', 'UK', 'DE', 'FR', 'ES',
    'IT', 'PL', 'TR', '4K', '8K'})


def apply_case_mode(name: str, mode: str) -> str:
    s = _WS_MULTI.sub(' ', (name or "").strip())
    if not s:
        return s
    if mode == 'canonical':
        return strip_orig_suffix(strip_provider_prefix(s))
    if mode == 'lower':
        return s.casefold()
    if mode == 'upper':
        return s.upper()
    if mode == 'title':
        return _smart_title(s)
    return s


def dominant_script(text: str) -> str:
    if not text:
        return 'unknown'
    ratios = {
        'cyrillic': _script_ratio(text, _CYR_LO, _CYR_HI),
        'hebrew': _script_ratio(text, _HEB_LO, _HEB_HI),
        'armenian': _script_ratio(text, _ARM_LO, _ARM_HI),
        'georgian': _script_ratio(text, _GEO_LO, _GEO_HI),
        'arabic': _script_ratio(text, _ARA_LO, _ARA_HI),
    }
    latin = sum(1 for c in text
                if ('a' <= c <= 'z') or ('A' <= c <= 'Z'))
    ratios['latin'] = latin / max(1, len(text))
    best = max(ratios.items(), key=lambda x: x[1])
    return 'mixed' if best[1] < 0.3 else best[0]


# ── Совместимость с прежним кодом ──

class ChannelNameNormalizer:
    @staticmethod
    def fix_encoding(text: str) -> str:
        return fix_encoding(text)

    @staticmethod
    def normalize(name: str, remove_parentheses: bool = True,
                  remove_brackets: bool = True,
                  remove_emojis: bool = True,
                  remove_special_chars: bool = True) -> str:
        return _normalize_impl(name or "", NormMode.FULL.value,
                                remove_parentheses, remove_brackets,
                                remove_emojis, remove_special_chars,
                                True)

    @staticmethod
    def token_set(name: str) -> Set[str]:
        if not name:
            return set()
        tokens = re.findall(r'\w+', name.casefold())
        return {str(int(t)) if t.isdigit() else t for t in tokens}

    @staticmethod
    def jaccard(a: str, b: str) -> float:
        return jaccard(a, b)