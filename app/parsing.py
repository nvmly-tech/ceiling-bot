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


# Телефон, продиктованный словами: «восемь девятьсот двенадцать триста сорок пять…» — так его пишут клиенты
# и так его иногда отдаёт расшифровка голосового. Число в номере — группа «сотни + десятки + единицы».
_UNITS = {"ноль": 0, "нуль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
          "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
_TEENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
          "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
         "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
             "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
_NUMBER_WORDS = {**_UNITS, **_TEENS, **_TENS, **_HUNDREDS}
_TOKEN = re.compile(r"[а-яё]+|\d+", re.IGNORECASE)
_GAP = re.compile(r"[\s,.;:()\-–—]*")  # что может стоять между частями номера
_PLUS = "плюс"


def _spoken_digits(tokens: list[str]) -> str:
    """«восемь», «девятьсот», «двенадцать», «123» → «8» «912» «123»: каждая группа — одно число."""
    out: list[str] = []
    group: dict[str, int] = {}

    def flush() -> None:
        if group:
            out.append(str(sum(group.values())))
            group.clear()

    for token in tokens:
        if token.isdigit():
            flush()
            out.append(token)
            continue
        value = _NUMBER_WORDS[token]
        if token in _HUNDREDS:
            kind = "h"
        elif token in _TENS:
            kind = "t"
        else:
            kind = "u"  # единицы и 10–19
        # Группа продолжается, только если новое слово — младший разряд, которого в ней ещё нет:
        # «девятьсот двенадцать» — одно число, «восемь девятьсот» — два.
        order = {"h": 0, "t": 1, "u": 2}
        if value == 0 or (group and (kind in group or order[kind] < max(order[k] for k in group)
                                     or ("t" in group and value >= 10))):
            flush()
        if value == 0:
            out.append("0")
            continue
        group[kind] = value
        if value in _TEENS.values():
            flush()  # после «двенадцать» группа закончена
    flush()
    return "".join(out)


def _replace_spoken_phones(text: str, repl: Callable[[str], str]) -> str:
    """Заменить номера, где есть хотя бы одно число словами (номера только из цифр — дело _PHONE_CANDIDATE)."""
    result, pos = [], 0
    while (m := _TOKEN.search(text, pos)) is not None:
        start, tokens, end = m.start(), [], m.start()
        plus = m.group().lower() == _PLUS
        cursor = m.end() if plus else m.start()
        while (t := _TOKEN.match(text, _GAP.match(text, cursor).end())) and (
            t.group().isdigit() or t.group().lower() in _NUMBER_WORDS
        ):
            tokens.append(t.group().lower())
            end = cursor = t.end()
        digits = _spoken_digits(tokens) if any(not t.isdigit() for t in tokens) else ""
        phone = normalize_phone(("+" if plus else "") + digits) if len(digits) >= 10 else None
        if phone:
            result.append(text[pos:start] + repl(phone))
            pos = end
        else:
            result.append(text[pos:m.end()])
            pos = m.end()
    result.append(text[pos:])
    return "".join(result)


def replace_phones(text: str, repl: Callable[[str], str]) -> str:
    """Заменить номера телефонов в тексте на repl(номер в формате +7XXXXXXXXXX): цифрами и продиктованные
    словами («восемь девятьсот двенадцать…», «плюс семь…», вперемешку с цифрами)."""
    text = _replace_spoken_phones(text, repl)

    def one(m: re.Match) -> str:
        chunk = m.group()
        if phone := normalize_phone(chunk):
            return repl(phone)
        if "." in chunk:
            # «…67 89. 20 метров»: точка — конец предложения, а не часть номера. Проверяем части по отдельности.
            return ".".join(_PHONE_CANDIDATE.sub(one, part) for part in chunk.split("."))
        return chunk  # не телефон (например, «20 30 40»)

    return _PHONE_CANDIDATE.sub(one, text)
