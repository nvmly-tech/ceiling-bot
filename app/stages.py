"""Этапы заявки после «Взял в работу»: менеджер отмечает итог кнопками, карточка Trello едет по спискам.

Этап — leads.stage: None — заявку взяли, итога ещё нет. Итоговые этапы — договор и отказ;
их можно вернуть в работу, если ошиблись кнопкой.
"""

from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.db import Lead  # db сам импортирует этот модуль

NO_ANSWER = "no_answer"
MEASURE = "measure"
THINKING = "thinking"
CONTRACT = "contract"
REFUSED = "refused"
REOPEN = "reopen"  # не этап, а действие: вернуть итоговую заявку в работу

STAGE_NAMES = {
    None: "в работе, итога ещё нет",
    NO_ANSWER: "не дозвонился",
    MEASURE: "замер назначен",
    THINKING: "клиент думает",
    CONTRACT: "договор",
    REFUSED: "отказ",
}
STAGE_ICONS = {None: "🛠", NO_ANSWER: "📵", MEASURE: "📅", THINKING: "🤔", CONTRACT: "✅", REFUSED: "❌"}
REFUSE_REASONS = {
    "price": "дорого",
    "changed": "передумал",
    "competitor": "выбрал другую студию",
    "unreachable": "не дозвонились",
    "other": "другое",
}
# Что можно отметить на каждом этапе (MEASURE на этапе «замер назначен» — перенести замер).
NEXT: dict[str | None, tuple[str, ...]] = {
    None: (MEASURE, NO_ANSWER, REFUSED),
    NO_ANSWER: (MEASURE, NO_ANSWER, REFUSED),
    MEASURE: (CONTRACT, THINKING, MEASURE, REFUSED),
    THINKING: (CONTRACT, REFUSED),
    CONTRACT: (REOPEN,),
    REFUSED: (REOPEN,),
}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
HISTORY_SHOWN = 3  # сколько прошлых заявок клиента показывать


def when_text(moment: datetime, zone: ZoneInfo) -> str:
    """«пт 02.10 в 14:00» — по часовому поясу студии."""
    local = moment.astimezone(zone)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m} в {local:%H:%M}"


def stage_text(stage: str | None, measure_at: str | None, reason: str | None, zone: ZoneInfo) -> str:
    """«📅 замер назначен: пт 02.10 в 14:00» — для панели заявки, карточки и отчётов."""
    text = f"{STAGE_ICONS[stage]} {STAGE_NAMES[stage]}"
    if stage == MEASURE and measure_at:
        text += f": {when_text(datetime.fromisoformat(measure_at), zone)}"
    if stage == REFUSED and reason:
        text += f": {REFUSE_REASONS.get(reason, reason)}"
    return text


def history_text(lead: "Lead", zone: ZoneInfo) -> str:
    """«№5 от 12.09.26 — ✅ договор (Иван)» — прошлая заявка клиента одной строкой."""
    created = datetime.fromisoformat(lead.created_at).astimezone(zone)
    if lead.taken_at:
        state = stage_text(lead.stage, lead.measure_at, lead.refuse_reason, zone)
        if lead.taken_by_name:
            state += f" ({lead.taken_by_name})"
    else:
        state = "в работу не взята" if lead.status == "qualified" else "анкета не завершена"
    return f"№{lead.id} от {created:%d.%m.%y} — {state}"


def history_lines(previous: "Sequence[Lead]", zone: ZoneInfo) -> list[str]:
    """Прошлые заявки клиента (новые — первыми) для уведомления и карточки: чтобы не звонили двое
    и не называли разные цены."""
    lines = [history_text(lead, zone) for lead in previous[:HISTORY_SHOWN]]
    if len(previous) > HISTORY_SHOWN:
        lines.append(f"и ещё {len(previous) - HISTORY_SHOWN}")
    return lines


def stamp(measure_at: str) -> str:
    """Короткая метка времени замера для кнопок клиента: ответ на перенесённый замер не принимается."""
    return datetime.fromisoformat(measure_at).strftime("%Y%m%d%H%M")


def measure_hours(work_start: time, work_end: time) -> list[int]:
    """Часы, которые предлагаются для замера: рабочее время студии (конец 20:30 — последний час 20:00)."""
    last = work_end.hour + (1 if (work_end.minute or work_end.second) else 0)
    return list(range(work_start.hour, last))
