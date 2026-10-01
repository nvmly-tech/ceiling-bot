"""Взятые заявки: когда напомнить менеджеру, когда сообщить владельцу, и тексты этих сообщений — а также
сообщений об ответах клиента (замер, «связались ли», оценка). Отправляет Notifier."""

from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import Lead
from app.stages import CONTRACT, MEASURE, NO_ANSWER, REFUSED, THINKING, stage_text, when_text
from app.worktime import next_work_start

STUCK_NUDGES_MAX = 2                # напоминаний менеджеру о заявке без итога — на каждый этап
LOW_RATING = 3                      # оценка замера не выше — сообщить владельцу
RATING_MAX = 5


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def span(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 48 * 60:
        return f"{minutes // 60} ч"
    return f"{minutes // (24 * 60)} дн."


def mention(lead: Lead, *, link: bool = True) -> str:
    """Кто ведёт заявку — с упоминанием, чтобы напоминание пришло ему лично."""
    if lead.taken_by_username:
        return f"@{escape(lead.taken_by_username)}"
    name = escape(lead.taken_by_name or "менеджер")
    return f'<a href="tg://user?id={lead.taken_by_id}">{name}</a>' if link and lead.taken_by_id else name


def nudge_text(lead: Lead, who: str, at: datetime, zone: ZoneInfo) -> str:
    head = f"<b>№{lead.id}</b> · {escape(lead.name or 'Клиент')}"
    phone = f" · {escape(lead.phone)}" if lead.phone else ""
    since = span(at - parse_ts(lead.stage_at or lead.taken_at))
    if lead.stage == NO_ANSWER:
        return f"📵 {head}{phone} — не дозвонились {since} назад\n{who}, попробуйте ещё раз 👇"
    if lead.stage == MEASURE:
        return f"📅 {head}{phone} — замер был {when_text(parse_ts(lead.measure_at), zone)}\n{who}, чем закончился? 👇"
    if lead.stage == THINKING:
        return f"🤔 {head}{phone} — клиент думает {since}\n{who}, может, позвонить? 👇"
    return f"⏳ {head} — в работе {since} без итога\n{who}, отметьте, чем закончилось 👇"


def escalation_text(lead: Lead, reason: str, at: datetime, zone: ZoneInfo) -> str:
    head = f"⚠️ <b>Заявка №{lead.id}</b> · {escape(lead.name or 'Клиент')}"
    if reason == "untaken":
        lines = [f"{head} — никто не взял за {span(at - parse_ts(lead.notified_at))}"]
    else:
        base = lead.measure_at if lead.stage == MEASURE else lead.stage_at or lead.taken_at
        stage = escape(stage_text(lead.stage, lead.measure_at, lead.refuse_reason, zone))
        lines = [f"{head} — {stage}, без итога {span(at - parse_ts(base))}",
                 f"Ведёт: <b>{escape(lead.taken_by_name or '—')}</b>"]
    if lead.trello_card_url:
        lines.append(f'📋 <a href="{escape(lead.trello_card_url)}">Карточка в Trello</a>')
    return "\n".join(lines)


VISIT_NOTES = {
    "yes": "✅ {head} подтвердил(а) замер: {when}",
    "move": "🔁 {head}{phone} просит перенести замер ({when})\n{who}, позвоните и выберите новое время 👇",
    "cancel": "❌ {head}{phone} отменил(а) замер ({when})\n{who}, уточните, что случилось 👇",
}


def visit_text(lead: Lead, answer: str, measure_at: str, who: str, zone: ZoneInfo) -> str:
    """Клиент ответил на сообщение о замере."""
    return VISIT_NOTES[answer].format(
        head=f"<b>№{lead.id}</b> · {escape(lead.name or 'Клиент')}",
        phone=f" · {escape(lead.phone)}" if lead.phone else "",
        when=when_text(parse_ts(measure_at), zone), who=who,
    )


def feedback_text(lead: Lead, about: str, answer: str | int, who: str | None, at: datetime) -> str:
    """Клиент ответил, связались ли с ним, или оценил замер. who — упоминание того, кто ведёт заявку
    (сообщение в группу, с просьбой к нему); None — сообщение владельцу: только факты."""
    head = f"<b>№{lead.id}</b> · {escape(lead.name or 'Клиент')}"
    if about == "rating":
        return (f"{'⭐' * int(answer)} {head} оценил(а) замер: <b>{answer} из {RATING_MAX}</b>\n"
                f"Вёл(а): {escape(lead.taken_by_name or '—')}")
    if answer == "yes":
        return f"✅ {head}: клиент говорит, что с ним связались\n{who}, отметьте итог 👇"
    phone = f" · {escape(lead.phone)}" if lead.phone else ""
    waited = span(at - parse_ts(lead.taken_at))
    complaint = f"❗ {head}{phone}: клиент говорит, что с ним ещё не связались"
    if who is None:
        return f"{complaint}\nЗаявку взял(а) {escape(lead.taken_by_name or '—')} {waited} назад"
    return f"{complaint} (заявку взяли {waited} назад)\n{who}, позвоните, пожалуйста 👇"


def nudge_due(lead: Lead, s: Settings) -> datetime | None:
    """Когда напомнить тому, кто взял заявку, что итога нет; None — не напоминаем."""
    if lead.nudges_sent >= STUCK_NUDGES_MAX:
        return None
    every = timedelta(days=s.thinking_remind_days) if lead.stage == THINKING else timedelta(minutes=s.stuck_after_min)
    if lead.last_nudge_at:
        return next_work_start(parse_ts(lead.last_nudge_at), s) + every
    if lead.stage == MEASURE:
        # Первый раз — когда замер уже прошёл: спросить, чем закончился.
        if not lead.measure_at:
            return None
        return next_work_start(parse_ts(lead.measure_at) + timedelta(minutes=s.measure_result_after_min), s)
    return next_work_start(parse_ts(lead.stage_at or lead.taken_at), s) + every


def escalate_due(lead: Lead, s: Settings) -> datetime | None:
    """Когда сообщить владельцу, что итога нет; «клиент думает» — не повод."""
    if lead.escalated_at or lead.stage == THINKING:
        return None
    base = lead.measure_at if lead.stage == MEASURE else lead.stage_at or lead.taken_at
    return parse_ts(base) + timedelta(hours=s.escalate_after_hours) if base else None


def in_work_on(lead: Lead | None, stage: str | None) -> bool:
    """Заявка всё ещё взята и на том же этапе, что при постановке задачи (иначе напоминание устарело)."""
    return (
        lead is not None and lead.status not in ("cancelled", "deleted") and bool(lead.taken_at)
        and lead.stage == stage and stage not in (CONTRACT, REFUSED)
    )
