"""Сообщения клиенту после анкеты — по делу его заявки: замер назначен, напоминание накануне.

Идут через outbox (очередь «tg:<заявка>»), попадают в переписку и в карточку Trello. Клиент мог заблокировать
бота — такое сообщение не повторяется: оно не должно держать очередь заявки.
"""

import logging
from datetime import UTC, datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.config import Settings
from app.db import TG_TO_CLIENT, Database, Lead, OutboxTask
from app.stages import MEASURE, stamp, when_text
from app.worktime import is_work_time

log = logging.getLogger(__name__)

SCRIPT = "script"  # подпись в переписке: текст по шаблону, без LLM
REMIND_TIME = time(12, 0)  # напоминание о замере — накануне в это время (по часовому поясу студии)


def visit_keyboard(lead: Lead, answers: tuple[str, ...]) -> InlineKeyboardMarkup:
    """Кнопки ответа о замере. В callback — метка времени замера: после переноса старые кнопки не сработают."""
    mark = stamp(lead.measure_at)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.VISIT_OPTIONS[a], callback_data=f"visit:{lead.id}:{a}:{mark}") for a in answers
    ]])


def measure_is(lead: Lead | None, measure_at: str) -> bool:
    """Замер всё ещё назначен на это время (не перенесли, не отменили, заявку не удалили)."""
    return (
        lead is not None and lead.status not in ("cancelled", "deleted")
        and lead.stage == MEASURE and lead.measure_at == measure_at
    )


class ClientFollowUp:
    def __init__(self, bot: Bot, db: Database, settings: Settings):
        self.bot, self.db, self.settings = bot, db, settings

    @property
    def handlers(self):
        return {TG_TO_CLIENT: self.send}

    def _message(self, lead: Lead, what: str) -> tuple[str, InlineKeyboardMarkup] | None:
        when = when_text(datetime.fromisoformat(lead.measure_at), self.settings.zone)
        if what == "measure_set":
            return texts.MEASURE_SET.format(lead_id=lead.id, when=when), visit_keyboard(lead, ("move", "cancel"))
        if what == "measure_remind":
            return (texts.MEASURE_REMIND.format(lead_id=lead.id, when=when),
                    visit_keyboard(lead, ("yes", "move", "cancel")))
        return None

    async def send(self, task: OutboxTask) -> None:
        lead = await self.db.get_lead(task.lead_id)
        at = task.payload["at"]
        if not measure_is(lead, at) or datetime.fromisoformat(at) <= datetime.now(UTC):
            return  # замер перенесли или отменили, пока сообщение ждало очереди; или он уже прошёл
        message = self._message(lead, task.payload["what"])
        if message is None:
            log.error("Неизвестное сообщение клиенту: %s", task.payload)
            return
        text, markup = message
        try:
            await self.bot.send_message(lead.chat_id, text, reply_markup=markup)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("Сообщение клиенту по заявке %s не доставлено: %s", lead.id, e.message)
            return
        await self.db.add_message(lead.id, direction="out", kind="text", text=text, model=SCRIPT)

    async def scan(self, now: datetime) -> None:
        """Напоминание о замере накануне в REMIND_TIME. Замер назначили позже этого времени — клиент только что
        получил подтверждение, второе сообщение лишнее. Накануне не успели (бот не работал) — в день замера
        «завтра» уже не напишешь, пропускаем."""
        s = self.settings
        if not is_work_time(now.astimezone(s.zone), s.work_start, s.work_end):
            return
        for lead in await self.db.leads_to_remind_measure():
            measure_at = datetime.fromisoformat(lead.measure_at)
            day_before = measure_at.astimezone(s.zone).date() - timedelta(days=1)
            remind_at = datetime.combine(day_before, REMIND_TIME, tzinfo=s.zone)
            too_late = now.astimezone(s.zone).date() > day_before
            if datetime.fromisoformat(lead.stage_at) >= remind_at or too_late:
                await self.db.update_lead(lead.id, measure_reminded_for=lead.measure_at)
            elif now >= remind_at:
                event = (TG_TO_CLIENT, {"what": "measure_remind", "at": lead.measure_at})
                await self.db.update_lead(lead.id, measure_reminded_for=lead.measure_at, events=[event])
