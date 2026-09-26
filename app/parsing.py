"""Разбор свободных ответов клиента: площадь и телефон."""

import re

# Число, перед которым нет буквы/цифры: «м2» и «м²» не дают ложную площадь 2.
_NUM = r"(?<![\w.,])(\d+(?:[.,]\d+)?)"
_RANGE_RE = re.compile(_NUM + r"\s*(?:-|–|—|до)\s*(\d+(?:[.,]\d+)?)")
_NUM_RE = re.compile(_NUM)

AREA_MIN, AREA_MAX = 1.0, 5000.0


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_area(text: str) -> float | None:
    """Площадь в м² из свободного текста: «18», «около 20 кв.м», «18,5 м2», «20-25» (→ 22.5)."""
    if m := _RANGE_RE.search(text):
        value = (_num(m.group(1)) + _num(m.group(2))) / 2
    elif m := _NUM_RE.search(text):
        value = _num(m.group(1))
    else:
        return None
    return value if AREA_MIN <= value <= AREA_MAX else None


def normalize_phone(text: str) -> str | None:
    """Телефон в формате +7XXXXXXXXXX (или +<код><номер> для иностранных), иначе None."""
    raw = text.strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if raw.startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    return None
