"""Ответы клиента на сообщения бота после анкеты: замер «жду / перенести / отменить».

Кнопки не зависят от состояния диалога (клиент мог уже начать новую заявку): заявка — в callback,
и принимаем только от её автора. Ответ идёт в переписку и отдельным уведомлением тому, кто ведёт заявку.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.db import TG_VISIT, Database
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


def create_followup_router() -> Router:
    r = Router(name="followup")
    r.callback_query.filter(F.message.chat.type == "private")
    r.callback_query.register(on_visit_button, F.data.startswith("visit:"))
    return r
