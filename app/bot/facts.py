"""Факты о студии из файла (facts.md) — всё, что бот может сообщать клиенту сверх анкеты.

Студия правит файл сама, без программиста: изменения подхватываются на следующем шаге диалога
(проверяется время изменения файла), перезапуск не нужен. Суммы в рублях из файла — единственные,
которые бот может назвать (см. assistant.check_amounts).

На сервере рабочая копия — /etc/ceiling-bot/facts.md (FACTS_PATH в unit-файле), её создаёт install.sh
из образца в репозитории и больше не трогает. Если файл пропал или испорчен — бот работает на последней
удачной версии, а если его не было с самого начала — на образце из репозитория.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "facts.md"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)  # служебные пометки для студии — в промпт не идут
# Название студии для приветствия: строка «- Название: Потолки Мастер» (владелец пишет её сам).
_NAME = re.compile(r"^\s*-\s*название\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
NAME_MAX = 60
# Сумма в рублях: «500 ₽», «1 700 ₽», «≈ 34 000 руб.» (пробелы внутри числа — любые).
_RUBLES = re.compile(r"(\d[\d\s  ]*)\s*(?:₽|руб)")


# Окончания прилагательных: «матовый / матовые / матового…» → основа «матов».
_ADJ_END = re.compile(r"(ыми|ими|ого|его|ому|ему|ый|ий|ой|ая|яя|ое|ее|ые|ие|ым|им|ом|ем|ую|юю|ых|их)$")


def stem(word: str) -> str:
    return _ADJ_END.sub("", word.lower().replace("ё", "е"))


def is_adjective(word: str) -> bool:
    return len(word) >= 5 and bool(_ADJ_END.search(word.lower()))


@dataclass(frozen=True)
class CeilingVocab:
    """Виды потолков для проверки ответа модели. core — сами виды («Виды:» в фактах и кнопки анкеты): по ним видно,
    что модель перечисляет виды; known — все прилагательные из фактов: только их можно называть в перечислении."""

    core: frozenset[str]
    known: frozenset[str]
    words: frozenset[str]  # сами названия видов целиком (для поиска опечаток: «парижский» ~ «парящий»)


def ceiling_vocab(text: str) -> CeilingVocab:
    from app.bot import texts

    buttons = " ".join(texts.CEILING_OPTIONS.values())
    kinds = " ".join(line.split(":", 1)[1] for line in text.splitlines() if "Виды:" in line)
    words = lambda s: re.findall(r"[А-Яа-яЁё]+", s)  # noqa: E731
    kind_words = {w.lower().replace("ё", "е") for w in words(kinds + " " + buttons) if is_adjective(w)}
    core = {stem(w) for w in kind_words}
    known = core | {stem(w) for w in words(text) if is_adjective(w)}
    return CeilingVocab(frozenset(core), frozenset(known), frozenset(kind_words))


def rubles(text: str) -> set[int]:
    return {int(re.sub(r"\D", "", m.group(1))) for m in _RUBLES.finditer(text)}


# Обещания сверх цен: проценты, скидки, рассрочка, подарки, «бесплатно». Модель может повторять только те, что есть
# в фактах (см. assistant.check_promises).
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|процент)")
_OFFER = re.compile(r"\b(скидк|рассрочк|кредит|акци|подар|промокод|бонус|к[еэ]шб[эе]к|предоплат|оплат)")
FREE = "бесплатн"


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def percents(text: str) -> set[str]:
    return {m.group(1).replace(",", ".") for m in _PERCENT.finditer(_norm(text))}


def offers(text: str) -> set[str]:
    """Основы слов-обещаний в тексте: «скидку» → «скидк»."""
    return {m.group(1) for m in _OFFER.finditer(_norm(text))}


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"[.!?\n]+", _norm(text)) if s.strip()]


def free_words(clause: str) -> set[str]:
    """Начала слов в части предложения с «бесплатно» — что именно бесплатно: «на бесплатном замере» → {«замер»}."""
    return {w[:5] for w in re.findall(r"[а-я]+", _norm(clause)) if len(w) >= 4 and not w.startswith(FREE)}


@dataclass(frozen=True)
class PromiseVocab:
    """Обещания из фактов: проценты, основы слов «скидка / рассрочка / подарок…» и что бесплатно («Замер бесплатный»
    → «замер»; берётся часть предложения до запятой: «Замер бесплатный, монтаж — от 500 ₽» — монтаж не бесплатный)."""

    percents: frozenset[str]
    offers: frozenset[str]
    free: frozenset[str]


def promise_vocab(text: str) -> PromiseVocab:
    clauses = (c for s in sentences(text) for c in re.split(r"[,;]", s) if FREE in c)
    free = {w for c in clauses for w in free_words(c)}
    return PromiseVocab(frozenset(percents(text)), frozenset(offers(text)), frozenset(free))


def studio_name(text: str) -> str | None:
    match = _NAME.search(text)
    if not match:
        return None
    return match.group(1).strip().strip("«»\"'“”").strip()[:NAME_MAX] or None


def _read(path: Path) -> str:
    text = _COMMENT.sub("", path.read_text(encoding="utf-8")).strip()
    if not text:
        raise ValueError("в файле нет фактов (пусто или одни пометки)")
    return text


class StudioFacts:
    def __init__(self, path: str | Path = DEFAULT_FILE):
        self.path = Path(path)
        self._text = ""
        self._amounts: frozenset[int] = frozenset()
        self._mtime: int | None = None
        self._problem: str | None = None  # чтобы не писать в лог одно и то же на каждом сообщении
        self.refresh()
        if not self._text:
            self._set(_read(DEFAULT_FILE))
            log.warning("Факты о студии: %s не найден — работаю на образце из репозитория", self.path)

    def _set(self, text: str) -> None:
        self._text, self._amounts, self._vocab = text, frozenset(rubles(text)), ceiling_vocab(text)
        self._promises = promise_vocab(text)
        self._name = studio_name(text)

    def _report(self, problem: str) -> None:
        # На старте прежней версии нет — об этом отдельное предупреждение в __init__ (переход на образец).
        if problem != self._problem and self._text:
            log.error("Факты о студии: %s — %s; работаю на прежней версии", self.path, problem)
        self._problem = problem

    def refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError as e:
            self._report(f"файл недоступен ({type(e).__name__})")
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            text = _read(self.path)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            self._report(str(e) or type(e).__name__)
            return
        self._set(text)
        self._problem = None
        log.info("Факты о студии загружены из %s: сумм в рублях — %s", self.path, len(self._amounts))

    @property
    def text(self) -> str:
        self.refresh()
        return self._text

    @property
    def allowed_amounts(self) -> frozenset[int]:
        self.refresh()
        return self._amounts

    @property
    def name(self) -> str | None:
        """Название студии из строки «- Название: …»; None — строки нет (приветствие без названия)."""
        self.refresh()
        return self._name

    @property
    def ceiling_types(self) -> CeilingVocab:
        self.refresh()
        return self._vocab

    @property
    def promises(self) -> PromiseVocab:
        self.refresh()
        return self._promises


_default: StudioFacts | None = None


def default_facts() -> StudioFacts:
    """Образец из репозитория — для кода и тестов, которым не передали свой файл."""
    global _default
    if _default is None:
        _default = StudioFacts(DEFAULT_FILE)
    return _default
