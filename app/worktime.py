"""Рабочее время студии: часы и дни недели, праздники. Вне его клиенту честно говорим, когда ответит менеджер,
уведомления идут без звука, напоминания и вопросы клиентам ждут начала рабочего времени."""

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.config import Settings

SEARCH_DAYS = 400  # ищем рабочий день не дальше года с запасом — иначе ошибка настроек, а не вечный цикл
WEEKDAYS_ACC = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")


def is_work_time(now: datetime, start: time, end: time) -> bool:
    """Только часы, без дней недели."""
    return start <= now.time() < end


def work_day(day: date, settings: "Settings") -> bool:
    dates, yearly = settings.holidays
    return day.isoweekday() in settings.workdays and day not in dates and (day.month, day.day) not in yearly


def work_time(moment: datetime, settings: "Settings") -> bool:
    local = moment.astimezone(settings.zone)
    return work_day(local.date(), settings) and is_work_time(local, settings.work_start, settings.work_end)


def _next_work_day(day: date, settings: "Settings") -> date:
    for _ in range(SEARCH_DAYS):
        if work_day(day, settings):
            return day
        day += timedelta(days=1)
    raise RuntimeError("за год вперёд нет ни одного рабочего дня — проверьте WORK_DAYS и DAYS_OFF")


def next_work_start(moment: datetime, settings: "Settings") -> datetime:
    """Ближайший момент рабочего времени, начиная с moment (в часовом поясе студии)."""
    local = moment.astimezone(settings.zone)
    if work_time(local, settings):
        return local
    today = local.date()
    start = today if local.time() < settings.work_start else today + timedelta(days=1)
    return datetime.combine(_next_work_day(start, settings), settings.work_start, tzinfo=settings.zone)


def last_work_end(moment: datetime, settings: "Settings") -> datetime:
    """Конец последнего рабочего дня до moment: с него начинается «нерабочее время» (для утренней сводки)."""
    day = moment.astimezone(settings.zone).date() - timedelta(days=1)
    for _ in range(SEARCH_DAYS):
        if work_day(day, settings):
            return datetime.combine(day, settings.work_end, tzinfo=settings.zone)
        day -= timedelta(days=1)
    raise RuntimeError("за год назад нет ни одного рабочего дня — проверьте WORK_DAYS и DAYS_OFF")


def manager_eta(now: datetime, settings: "Settings") -> str:
    """Когда ответит менеджер: «сегодня в 9:00», «завтра в 9:00», «в понедельник в 9:00», «9 января в 9:00»."""
    local = now.astimezone(settings.zone)
    start = next_work_start(local, settings)
    clock = f"в {start.hour}:{start.minute:02d}"
    days = (start.date() - local.date()).days
    if days == 0:
        return f"сегодня {clock}"
    if days == 1:
        return f"завтра {clock}"
    if days < 7:
        return f"{WEEKDAYS_ACC[start.weekday()]} {clock}"
    return f"{start.day} {MONTHS_GEN[start.month - 1]} {clock}"


def local_now(zone: ZoneInfo) -> datetime:
    return datetime.now(zone)
