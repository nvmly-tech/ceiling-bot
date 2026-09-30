"""Сообщения клиенту после анкеты — по делу его заявки: замер назначен, напоминание накануне,
вопросы о качестве («с вами связался менеджер?», оценка замера).

Идут через outbox (очередь «tg:<заявка>»), попадают в переписку и в карточку Trello. Перед отправкой
проверяется, что сообщение ещё к месту: замер не перенесли, итог не отметили, клиент не ответил.
Клиент мог заблокировать бота — такое сообщение не повторяется: оно не должно держать очередь заявки.
"""

import logging
from datetime import UTC, datetime, time, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.config import Settings
from app.db import TG_TO_CLIENT, Database, Lead, OutboxTask, now_iso
from app.services.notifier import next_work_start
from app.stages import MEASURE, REFUSED, stamp, when_text
from app.worktime import is_work_time

log = logging.getLogger(__name__)

SCRIPT = "script"  # подпись в переписке: текст по шаблону, без LLM
REMIND_TIME = time(12, 0)  # напоминание о замере — накануне в это время (по часовому поясу студии)
RATINGS = range(1, 6)

Outgoing = tuple[str, InlineKeyboardMarkup]


def visit_keyboard(lead: Lead, answers: tuple[str, ...]) -> InlineKeyboardMarkup:
    """Кнопки ответа о замере. В callback — метка времени замера: после переноса старые кнопки не сработают."""
    mark = stamp(lead.measure_at)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.VISIT_OPTIONS[a], callback_data=f"visit:{lead.id}:{a}:{mark}") for a in answers
    ]])


def contact_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"contact:{lead.id}:{answer}")
        for answer, label in texts.CONTACT_OPTIONS.items()
    ]])


def rating_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(i), callback_data=f"rate:{lead.id}:{i}") for i in RATINGS
    ]])


def measure_is(lead: Lead, measure_at: str) -> bool:
    """Замер всё ещё назначен на это время (не перенесли и не отменили) и ещё не прошёл."""
    return (
        lead.stage == MEASURE and lead.measure_at == measure_at
        and datetime.fromisoformat(measure_at) > datetime.now(UTC)
    )


def measured(lead: Lead) -> bool:
    """Замер, судя по тому, что известно, состоялся. Отказ до назначенного времени — это отмена; клиент нажал
    «перенести» или «отменить», а менеджер этап не обновил — тоже: оценивать нечего."""
    cancelled_before = lead.stage == REFUSED and lead.stage_at < lead.measure_at
    return not cancelled_before and lead.visit_answer not in ("move", "cancel")


class ClientFollowUp:
    def __init__(self, bot: Bot, db: Database, settings: Settings):
        self.bot, self.db, self.settings = bot, db, settings

    @property
    def handlers(self):
        return {TG_TO_CLIENT: self.send}

    def _message(self, lead: Lead, payload: dict) -> Outgoing | None:
        """Текст и кнопки сообщения; None — оно уже не к месту (или неизвестного вида)."""
        what = payload["what"]
        if what in ("measure_set", "measure_remind"):
            if not measure_is(lead, payload["at"]):
                return None
            when = when_text(datetime.fromisoformat(lead.measure_at), self.settings.zone)
            if what == "measure_set":
                return texts.MEASURE_SET.format(lead_id=lead.id, when=when), visit_keyboard(lead, ("move", "cancel"))
            text = texts.MEASURE_REMIND.format(lead_id=lead.id, when=when)
            return text, visit_keyboard(lead, ("yes", "move", "cancel"))
        if what == "ask_contact":
            # Менеджер успел отметить итог или клиент уже ответил — спрашивать поздно.
            if not lead.taken_at or lead.stage_at or lead.contact_answer:
                return None
            return texts.CONTACT_ASK.format(lead_id=lead.id), contact_keyboard(lead)
        if what == "ask_rating":
            if lead.rating is not None or not lead.measure_at or not measured(lead):
                return None
            return texts.RATE_ASK.format(lead_id=lead.id), rating_keyboard(lead)
        log.error("Неизвестное сообщение клиенту: %s", payload)
        return None

    async def send(self, task: OutboxTask) -> None:
        lead = await self.db.get_lead(task.lead_id)
        if lead is None or lead.status in ("cancelled", "deleted"):
            return
        message = self._message(lead, task.payload)
        if message is None:
            return
        text, markup = message
        try:
            await self.bot.send_message(lead.chat_id, text, reply_markup=markup)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("Сообщение клиенту по заявке %s не доставлено: %s", lead.id, e.message)
            return
        await self.db.add_message(lead.id, direction="out", kind="text", text=text, model=SCRIPT)

    async def scan(self, now: datetime) -> None:
        """Что пора написать клиентам. Только в рабочее время студии — ночью клиента не беспокоим."""
        s = self.settings
        if not is_work_time(now.astimezone(s.zone), s.work_start, s.work_end):
            return
        await self._remind_measures(now)
        await self._ask_contact(now)
        await self._ask_rating(now)

    async def _remind_measures(self, now: datetime) -> None:
        """Напоминание о замере накануне в REMIND_TIME. Замер назначили позже этого времени — клиент только что
        получил подтверждение, второе сообщение лишнее. Накануне не успели (бот не работал) — в день замера
        «завтра» уже не напишешь, пропускаем."""
        zone = self.settings.zone
        for lead in await self.db.leads_to_remind_measure():
            day_before = datetime.fromisoformat(lead.measure_at).astimezone(zone).date() - timedelta(days=1)
            remind_at = datetime.combine(day_before, REMIND_TIME, tzinfo=zone)
            too_late = now.astimezone(zone).date() > day_before
            if datetime.fromisoformat(lead.stage_at) >= remind_at or too_late:
                await self.db.update_lead(lead.id, measure_reminded_for=lead.measure_at)
            elif now >= remind_at:
                event = (TG_TO_CLIENT, {"what": "measure_remind", "at": lead.measure_at})
                await self.db.update_lead(lead.id, measure_reminded_for=lead.measure_at, events=[event])

    async def _ask_contact(self, now: datetime) -> None:
        """Заявку взяли, а итога нет — спросить клиента, связались ли с ним."""
        s = self.settings
        wait = timedelta(minutes=s.ask_contact_after_min)
        for lead in await self.db.leads_to_ask_contact():
            if now >= next_work_start(datetime.fromisoformat(lead.taken_at), s) + wait:
                await self.db.update_lead(lead.id, contact_asked_at=now_iso(now),
                                          events=[(TG_TO_CLIENT, {"what": "ask_contact"})])

    async def _ask_rating(self, now: datetime) -> None:
        """Замер прошёл — попросить оценку."""
        s = self.settings
        wait = timedelta(minutes=s.rate_after_min)
        for lead in await self.db.leads_to_ask_rating():
            if measured(lead) and now >= next_work_start(datetime.fromisoformat(lead.measure_at) + wait, s):
                await self.db.update_lead(lead.id, rating_asked_at=now_iso(now),
                                          events=[(TG_TO_CLIENT, {"what": "ask_rating"})])
