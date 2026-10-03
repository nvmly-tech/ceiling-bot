"""/order: клиент смотрит свою заявку, правит поля и может удалить её целиком."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.assistant import known_fields
from app.bot.dialog import Incoming, Markup, advance, ask, log_in
from app.bot.states import EDITS, QUESTIONS, Lead
from app.config import Settings
from app.db import TG_CLIENT_MSG, Database
from app.db import Lead as LeadRow
from app.parsing import clip, normalize_phone, parse_area, replace_phones
from app.stages import CONTRACT, MEASURE, REFUSED, when_text

log = logging.getLogger(__name__)

EDIT_STATES = {s.state for s in EDITS.values()}
FIELD_BY_EDIT = {s.state: field for field, s in EDITS.items()}
EDIT_OPTIONS = {"object": texts.OBJECT_OPTIONS, "area": texts.AREA_OPTIONS, "ceiling_type": texts.CEILING_OPTIONS}


def client_status(lead: LeadRow, zone: ZoneInfo) -> str:
    if lead.stage == MEASURE and lead.measure_at:
        return texts.ORDER_STATUS["measure"].format(when=when_text(datetime.fromisoformat(lead.measure_at), zone))
    if lead.stage in (CONTRACT, REFUSED):
        return texts.ORDER_STATUS[lead.stage]
    if lead.taken_at:
        return texts.ORDER_STATUS["taken"]
    return texts.ORDER_STATUS["sent" if lead.status == "qualified" else "filling"]


def order_text(lead: LeadRow, zone: ZoneInfo) -> str:
    known = known_fields(lead)
    fields = "\n".join(f"{label}: {known[f] or texts.EMPTY_VALUE}" for f, label in texts.FIELD_LABELS.items())
    return texts.ORDER_VIEW.format(lead_id=lead.id, status=client_status(lead, zone), fields=fields)


def edit_keyboard(field: str) -> Markup:
    return {"object": keyboards.objects(), "area": keyboards.areas(), "ceiling_type": keyboards.ceilings(),
            "phone": keyboards.phone()}.get(field, keyboards.remove())


async def leave_edit(state: FSMContext) -> str | None:
    """Выйти из правки поля (если в ней) — вернуться туда, откуда клиент открыл /order."""
    current = await state.get_state()
    if current in EDIT_STATES:
        current = (await state.get_data()).get("edit_return") or Lead.done.state
        await state.set_state(current)
        await state.update_data(edit_return=None, edit_retry=False)
    return current


async def on_order(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    """Показать заявку с кнопками правки. Просмотр — не часть переписки: в базу и карточку не пишем."""
    await leave_edit(state)
    lead_id = (await state.get_data()).get("lead_id")
    lead = await db.get_lead(lead_id) if lead_id else None
    if lead is None:
        await message.answer(texts.ORDER_NONE)
        return
    await message.answer(order_text(lead, settings.zone), reply_markup=keyboards.order(lead.id))


async def on_edit_button(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    _, field, raw_id = (cb.data.split(":") + ["", ""])[:3]
    current = await state.get_state()
    lead_id = (await state.get_data()).get("lead_id")
    await cb.answer()
    allowed = {s.state for s in QUESTIONS} | {Lead.done.state} | EDIT_STATES
    if (
        not isinstance(cb.message, Message) or raw_id != str(lead_id) or current not in allowed
        or (field != "ok" and field not in EDITS)
    ):
        return  # старая кнопка, чужая заявка или подделанный callback
    if field == "ok":
        await cb.message.edit_text(f"{cb.message.text}\n\n✓ {texts.ORDER_OK}")
        current = await leave_edit(state)
        if question_state := next((s for s in QUESTIONS if s.state == current), None):
            await ask(cb.message, db, lead_id, question_state)  # анкета не закончена — продолжаем её
        return
    if current not in EDIT_STATES:
        await state.update_data(edit_return=current)
    await state.update_data(edit_retry=False)
    await state.set_state(EDITS[field])
    label = texts.FIELD_LABELS[field]
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ Изменить: {label}")
    current_value = known_fields(await db.get_lead(lead_id))[field] or texts.EMPTY_VALUE
    prompt = texts.EDIT_PROMPT.format(label=label, current=current_value, hint=texts.EDIT_HINTS.get(field, ""))
    await cb.message.answer(prompt, reply_markup=edit_keyboard(field))


async def on_edit_choice(cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    field = FIELD_BY_EDIT[await state.get_state()]
    label = EDIT_OPTIONS[field].get(cb.data.split(":", 1)[1])
    lead_id = (await state.get_data()).get("lead_id")
    await cb.answer()
    if label is None or lead_id is None or not isinstance(cb.message, Message):
        return
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ {label}")
    # Диапазон площади — не число: старое area_m2 обнуляем, чтобы в карточке не осталось прежнего значения.
    values = {"area_text": label, "area_m2": None} if field == "area" else {field: label}
    await apply_edit(cb.message, state, db, settings, lead_id, field, values)


async def on_edit_text(message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming) -> None:
    field = FIELD_BY_EDIT[await state.get_state()]
    data = await state.get_data()
    lead_id = data["lead_id"]
    usable = item.kind in ("text", "voice") or (item.kind == "contact" and field == "phone")
    if not usable or (item.text or "").startswith("/"):
        await message.answer(texts.EDIT_TEXT_ONLY)
        return
    if field == "area":
        area = parse_area(item.text) if item.text else None
        if area is None and not item.pending and not data.get("edit_retry"):
            await state.update_data(edit_retry=True)  # переспрашиваем один раз, дальше принимаем как есть
            await message.answer(texts.Q_AREA_RETRY, reply_markup=keyboards.areas())
            return
        values = {"area_m2": area, "area_text": item.answer}
    elif field == "phone":
        phone = edited_phone(item)
        if phone is None:
            await message.answer(texts.Q_PHONE_RETRY, reply_markup=keyboards.phone())
            return
        values = {"phone": phone, **({"phone_verified": item.own_contact} if item.kind == "contact" else {})}
    else:
        values = {field: item.answer}
    if item.kind != "text":
        # Голосовое и контакт — в переписку как есть (вложение, расшифровка); текст несёт сама строка правки.
        await log_in(db, lead_id, item, field)
    await apply_edit(message, state, db, settings, lead_id, field, values)


async def on_delete_button(cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    """Удаление заявки: «delete:ask» — вопрос «Точно?», «delete:yes/no» — ответ (принимается только после вопроса)."""
    _, action, raw_id = (cb.data.split(":") + ["", ""])[:3]
    current = await state.get_state()
    data = await state.get_data()
    lead_id = data.get("lead_id")
    await cb.answer()
    allowed = {s.state for s in QUESTIONS} | {Lead.done.state} | EDIT_STATES
    if (
        not isinstance(cb.message, Message) or raw_id != str(lead_id) or current not in allowed
        or action not in ("ask", "yes", "no")
    ):
        return  # старая кнопка, чужая заявка или подделанный callback
    if action == "ask":
        await cb.message.edit_text(f"{cb.message.text}\n\n✓ {texts.ORDER_DELETE}")
        await state.update_data(delete_pending=lead_id)
        await cb.message.answer(texts.DELETE_CONFIRM.format(lead_id=lead_id),
                                reply_markup=keyboards.delete_confirm(lead_id))
        return
    if data.get("delete_pending") != lead_id:
        return  # «Да, удалить» без показанного вопроса — не принимаем
    await state.update_data(delete_pending=None)
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ {texts.DELETE_YES if action == 'yes' else texts.DELETE_NO}")
    if action == "no":
        await cb.message.answer(texts.DELETE_KEPT)
        current = await leave_edit(state)
        if question_state := next((s for s in QUESTIONS if s.state == current), None):
            await ask(cb.message, db, lead_id, question_state)  # анкета не закончена — продолжаем её
        return
    # Полное удаление по просьбе клиента: данные, переписка, карточка; менеджеру — без личных данных.
    await db.delete_lead_data(lead_id)
    await state.clear()
    log.info("Заявка %s удалена клиентом", lead_id)
    await cb.message.answer(texts.DELETED.format(lead_id=lead_id), reply_markup=keyboards.remove())


def edited_phone(item: Incoming) -> str | None:
    if item.kind == "contact":
        return normalize_phone(item.text) or clip(item.text)
    if item.text == texts.NO_PHONE:
        return texts.NO_PHONE_VALUE
    if item.pending:
        return item.answer  # номер подставится из расшифровки
    found: list[str] = []
    replace_phones(item.text or "", lambda p: found.append(p) or " ")
    return found[0] if found else normalize_phone(item.text or "")


async def apply_edit(
    message: Message, state: FSMContext, db: Database, settings: Settings, lead_id: int, field: str, values: dict,
) -> None:
    label = texts.FIELD_LABELS[field]
    old = known_fields(await db.get_lead(lead_id))[field] or texts.EMPTY_VALUE
    lead = await db.update_lead(lead_id, **values)
    new = known_fields(lead)[field] or texts.EMPTY_VALUE
    # Строка правки — в переписке: карточка Trello получает комментарий, описание обновляется само.
    line = texts.EDIT_LOG.format(label=label, old=old, new=new)
    await db.add_message(lead_id, direction="in", kind="edit", text=line)
    if lead.notified_at:  # менеджер уже знает о заявке — сообщаем и о правке
        await db.enqueue(TG_CLIENT_MSG, lead_id, coalesce=True, delay=timedelta(seconds=settings.client_msg_delay_sec))
    return_to = (await state.get_data()).get("edit_return")
    await state.update_data(edit_return=None, edit_retry=False)
    await message.answer(texts.EDIT_DONE.format(label=label, value=new), reply_markup=keyboards.remove())
    if return_to in {s.state for s in QUESTIONS}:
        await state.set_state(return_to)
        await advance(message, state, db, settings, lead_id)  # следующий незаполненный вопрос
        return
    await state.set_state(Lead.done)
    await message.answer(order_text(lead, settings.zone), reply_markup=keyboards.order(lead.id))


