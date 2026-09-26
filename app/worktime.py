"""Рабочие часы студии: ночью клиенту честно говорим, когда ответит менеджер."""

from datetime import datetime, time
from zoneinfo import ZoneInfo


def is_work_time(now: datetime, start: time, end: time) -> bool:
    return start <= now.time() < end


def manager_eta(now: datetime, start: time, end: time) -> str:
    """Когда ответит менеджер, если сейчас нерабочее время: «сегодня в 9:00» / «завтра в 9:00»."""
    when = "сегодня" if now.time() < start else "завтра"
    return f"{when} в {start.hour}:{start.minute:02d}"


def local_now(zone: ZoneInfo) -> datetime:
    return datetime.now(zone)
