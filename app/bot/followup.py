"""Ответы клиента на сообщения бота после анкеты: замер «жду / перенести / отменить»,
«связался ли менеджер», оценка замера.

Кнопки не зависят от состояния диалога (клиент мог уже начать новую заявку): заявка — в callback,
и принимаем только от её автора. Ответ идёт в переписку и отдельным уведомлением тому, кто ведёт заявку.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.db import TG_FEEDBACK, TG_VISIT, Database, Lead, now_iso
from app.services.client_followup import RATINGS
from app.stages import MEASURE, stamp

SCRIPT = "script"
VISIT_REPLIES = {"yes": texts.VISIT_YES_REPLY, "move": texts.VISIT_MOVE_REPLY, "cancel": texts.VISIT_CANCEL_REPLY}


async def on_visit_button(cb: CallbackQuery, db: Database) -> None:
    parts = (cb.data or "").split(":")
    valid = len(parts) == 4 and parts[1].isdigit() and parts[2] in VISIT_REPLIES
    if not valid or not isinstance(cb.message, Message):
        await cb.answer()  # подделанные данные кнопки
        return
    lead_id, answer, mark = int(parts[1]), parts[2], parts[3]
    lead = await db.get_lead(lead_id)
    if lead is None or lead.tg_user_id != cb.from_user.id or lead.status == "deleted":
        await cb.answer()
        return
    if lead.stage != MEASURE or not lead.measure_at or stamp(lead.measure_at) != mark:
        # Замер перенесли или отменили — ответ на старое время не передаём.
        await cb.answer(texts.VISIT_STALE, show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    await cb.answer()
    label = texts.VISIT_OPTIONS[answer]
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ {label}")
    # kind «visit»: в переписке и карточке есть, а менеджеру — отдельным уведомлением, не в «клиент дописал».
    await db.add_message(lead.id, direction="in", kind="visit", text=label)
    await db.enqueue(TG_VISIT, lead.id, {"answer": answer, "at": lead.measure_at})
    reply = VISIT_REPLIES[answer]
    await cb.message.answer(reply)
    await db.add_message(lead.id, direction="out", kind="text", text=reply, model=SCRIPT)


async def _own_lead(cb: CallbackQuery, db: Database, parts: list[str]) -> Lead | None:
    """Заявка из callback, если кнопку нажал её автор; иначе None (подделка или чужая заявка)."""
    if len(parts) != 3 or not parts[1].isdigit() or not isinstance(cb.message, Message):
        return None
    lead = await db.get_lead(int(parts[1]))
    if lead is None or lead.tg_user_id != cb.from_user.id or lead.status == "deleted":
        return None
    return lead


async def _record(
    cb: CallbackQuery, db: Database, lead: Lead, *, field: str, value: str | int, kind: str, label: str, reply: str,
) -> None:
    """Принять ответ клиента: только первый; в переписку, менеджерам и клиенту — «спасибо»."""
    if not await db.set_once(lead.id, field, value):
        await cb.answer(texts.ANSWER_TAKEN)
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    await cb.answer()
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ {label}")
    await db.add_message(lead.id, direction="in", kind=kind, text=label)
    await db.enqueue(TG_FEEDBACK, lead.id, {"about": kind, "answer": value, "at": now_iso()})
    await cb.message.answer(reply)
    await db.add_message(lead.id, direction="out", kind="text", text=reply, model=SCRIPT)


async def on_contact_button(cb: CallbackQuery, db: Database) -> None:
    parts = (cb.data or "").split(":")
    lead = await _own_lead(cb, db, parts)
    if lead is None or parts[2] not in texts.CONTACT_OPTIONS:
        await cb.answer()
        return
    answer = parts[2]
    reply = texts.CONTACT_YES_REPLY if answer == "yes" else texts.CONTACT_NO_REPLY
    await _record(cb, db, lead, field="contact_answer", value=answer, kind="contact",
                  label=texts.CONTACT_OPTIONS[answer], reply=reply)


async def on_rate_button(cb: CallbackQuery, db: Database) -> None:
    parts = (cb.data or "").split(":")
    lead = await _own_lead(cb, db, parts)
    if lead is None or not parts[2].isdigit() or int(parts[2]) not in RATINGS:
        await cb.answer()
        return
    rating = int(parts[2])
    await _record(cb, db, lead, field="rating", value=rating, kind="rating",
                  label=f"Оценка замера: {rating} из {RATINGS[-1]}", reply=texts.RATE_REPLY)


def create_followup_router() -> Router:
    r = Router(name="followup")
    r.callback_query.filter(F.message.chat.type == "private")
    r.callback_query.register(on_visit_button, F.data.startswith("visit:"))
    r.callback_query.register(on_contact_button, F.data.startswith("contact:"))
    r.callback_query.register(on_rate_button, F.data.startswith("rate:"))
    return r
