from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.parsing import normalize_phone, parse_area, replace_phones
from app.worktime import is_work_time, manager_eta


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18", 18.0),
        ("около 20 кв.м", 20.0),
        ("18,5 м2", 18.5),
        ("20-25", 22.5),
        ("от 30 до 40 м²", 35.0),
        ("м2 не знаю", None),
        ("много", None),
        ("0", None),
        # Номер телефона — не площадь: «8-912» раньше читалось как диапазон 8–912 → 460 м².
        ("Площадь примерно 18,5 метров, мой номер 8-912-345-67-89", 18.5),
        ("мой номер 8-912-345-67-89", None),
        ("звоните +7 (900) 123-45-67, площадь 20-25", 22.5),
        ("Площадь примерно 18,5 м. Мой номер 8912. 345 67 89", 18.5),  # так пишет GigaAM
    ],
)
def test_parse_area(text, expected):
    assert parse_area(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+7 900 123-45-67", "+79001234567"),
        ("89001234567", "+79001234567"),
        ("9001234567", "+79001234567"),
        ("+375 29 123 45 67", "+375291234567"),
        ("123", None),
        ("позвоните вечером", None),
    ],
)
def test_normalize_phone(text, expected):
    assert normalize_phone(text) == expected


def test_worktime():
    start, end = time(9), time(21)
    assert is_work_time(datetime(2026, 9, 25, 12, 0), start, end)
    assert not is_work_time(datetime(2026, 9, 25, 23, 0), start, end)
    settings, zone = Settings(bot_token="1:T", work_start=start, work_end=end), ZoneInfo("Europe/Moscow")
    assert manager_eta(datetime(2026, 9, 25, 3, 0, tzinfo=zone), settings) == "сегодня в 9:00"
    assert manager_eta(datetime(2026, 9, 25, 23, 0, tzinfo=zone), settings) == "завтра в 9:00"


def test_mask_phones_with_dot_inside_number():
    # GigaAM ставит точку внутри продиктованного номера: «8912. 345 67 89».
    from app.bot.assistant import PHONE_MASK, mask_phones

    assert mask_phones("Мой номер 8912. 345 67 89 Замер в субботу") == (
        f"Мой номер {PHONE_MASK} Замер в субботу", ["+79123456789"])
    assert mask_phones("площадь 18.5, 2026 год") == ("площадь 18.5, 2026 год", [])
    # Точка в конце предложения не склеивает номер со следующим числом.
    assert mask_phones("номер 8 912 345 67 89. 20 метров") == (f"номер {PHONE_MASK}. 20 метров", ["+79123456789"])
    assert parse_area("номер 8 912 345 67 89. 20 метров") == 20.0


# --- телефон, продиктованный словами ---

SPOKEN = "восемь девятьсот двенадцать триста сорок пять шестьдесят семь восемьдесят девять"


def phones_in(text: str) -> tuple[list[str], str]:
    found: list[str] = []
    rest = replace_phones(text, lambda p: found.append(p) or "[телефон]")
    return found, rest


@pytest.mark.parametrize(("text", "phone"), [
    (SPOKEN, "+79123456789"),
    ("Восемь Девятьсот Двенадцать Триста Сорок Пять Шестьдесят Семь Восемьдесят Девять", "+79123456789"),
    ("плюс семь девятьсот двенадцать триста сорок пять шестьдесят семь восемьдесят девять", "+79123456789"),
    ("восемь девять один два три четыре пять шесть семь восемь девять", "+79123456789"),  # по цифре
    ("8 девятьсот 123 45 67", "+79001234567"),  # вперемешку
    ("девятьсот двенадцать, триста сорок пять — шестьдесят семь, восемьдесят девять", "+79123456789"),
])
def test_spoken_phone(text, phone):
    found, rest = phones_in(text)
    assert found == [phone] and rest.strip(" ,.—") == "[телефон]"


def test_spoken_phone_inside_sentence_keeps_the_rest():
    found, rest = phones_in(f"мой номер {SPOKEN}, звоните в субботу после обеда")
    assert found == ["+79123456789"]
    assert rest == "мой номер [телефон], звоните в субботу после обеда"


@pytest.mark.parametrize("text", [
    "два окна и двадцать пять метров", "сто двадцать квадратов, три комнаты", "в пятницу после пяти",
    "один два три четыре пять",  # слишком коротко для телефона
])
def test_numbers_in_words_are_not_phones(text):
    assert phones_in(text) == ([], text)


def test_spoken_phone_masked_before_llm():
    from app.bot.assistant import mask_phones

    masked, phones = mask_phones(f"звоните: {SPOKEN}")
    assert phones == ["+79123456789"] and "девятьсот" not in masked
