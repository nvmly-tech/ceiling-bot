"""Разбор свободных ответов клиента: площадь и телефон."""

import re
from collections.abc import Callable

# Число, перед которым нет буквы/цифры: «м2» и «м²» не дают ложную площадь 2.
_NUM = r"(?<![\w.,])(\d+(?:[.,]\d+)?)"
_RANGE_RE = re.compile(_NUM + r"\s*(?:-|–|—|до)\s*(\d+(?:[.,]\d+)?)")
_NUM_RE = re.compile(_NUM)

AREA_MIN, AREA_MAX = 1.0, 5000.0
FIELD_MAX = 200  # длина поля анкеты: больше — уведомление менеджеру не влезет в лимит Telegram (4096)


def clip(text: str, limit: int = FIELD_MAX) -> str:
    """Обрезать текст до limit символов с многоточием."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_area(text: str) -> float | None:
    """Площадь в м² из свободного текста: «18», «около 20 кв.м», «18,5 м2», «20-25» (→ 22.5).
    Номера телефонов сначала убираются: «8-912-…» иначе читалось бы как диапазон 8–912."""
    text = replace_phones(text, lambda _phone: " ")
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


# Кандидат в телефон: цифры с пробелами, дефисами, скобками и точками («8912. 345 67 89» — так пишет GigaAM).
_PHONE_CANDIDATE = re.compile(r"\+?\d[\d\s\-().]{8,}\d")


def replace_phones(text: str, repl: Callable[[str], str]) -> str:
    """Заменить номера телефонов в тексте на repl(номер в формате +7XXXXXXXXXX)."""

    def one(m: re.Match) -> str:
        chunk = m.group()
        if phone := normalize_phone(chunk):
            return repl(phone)
        if "." in chunk:
            # «…67 89. 20 метров»: точка — конец предложения, а не часть номера. Проверяем части по отдельности.
            return ".".join(_PHONE_CANDIDATE.sub(one, part) for part in chunk.split("."))
        return chunk  # не телефон (например, «20 30 40»)

    return _PHONE_CANDIDATE.sub(one, text)
