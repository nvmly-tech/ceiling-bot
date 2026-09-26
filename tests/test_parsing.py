from datetime import datetime, time

import pytest

from app.parsing import normalize_phone, parse_area
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
    assert manager_eta(datetime(2026, 9, 25, 3, 0), start, end) == "сегодня в 9:00"
    assert manager_eta(datetime(2026, 9, 25, 23, 0), start, end) == "завтра в 9:00"
