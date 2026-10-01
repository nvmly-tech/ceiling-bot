"""Рабочие часы студии: ночью клиенту честно говорим, когда ответит менеджер."""

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.config import Settings


def is_work_time(now: datetime, start: time, end: time) -> bool:
    return start <= now.time() < end


def manager_eta(now: datetime, start: time, end: time) -> str:
    """Когда ответит менеджер, если сейчас нерабочее время: «сегодня в 9:00» / «завтра в 9:00»."""
    when = "сегодня" if now.time() < start else "завтра"
    return f"{when} в {start.hour}:{start.minute:02d}"


def local_now(zone: ZoneInfo) -> datetime:
    return datetime.now(zone)


def next_work_start(moment: datetime, settings: "Settings") -> datetime:
    """Ближайший момент рабочего времени, начиная с moment (в часовом поясе студии)."""
    local = moment.astimezone(settings.zone)
    if is_work_time(local, settings.work_start, settings.work_end):
        return local
    day = local.date() if local.time() < settings.work_start else local.date() + timedelta(days=1)
    return datetime.combine(day, settings.work_start, tzinfo=settings.zone)
