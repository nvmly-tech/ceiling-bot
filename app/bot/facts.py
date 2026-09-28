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
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "facts.md"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)  # служебные пометки для студии — в промпт не идут
# Сумма в рублях: «500 ₽», «1 700 ₽», «≈ 34 000 руб.» (пробелы внутри числа — любые).
_RUBLES = re.compile(r"(\d[\d\s  ]*)\s*(?:₽|руб)")


def rubles(text: str) -> set[int]:
    return {int(re.sub(r"\D", "", m.group(1))) for m in _RUBLES.finditer(text)}


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
        self._text, self._amounts = text, frozenset(rubles(text))

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


_default: StudioFacts | None = None


def default_facts() -> StudioFacts:
    """Образец из репозитория — для кода и тестов, которым не передали свой файл."""
    global _default
    if _default is None:
        _default = StudioFacts(DEFAULT_FILE)
    return _default
