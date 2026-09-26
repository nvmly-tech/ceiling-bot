"""Диалог квалификации: 4 вопроса → заявка.

Каждое входящее и исходящее сообщение пишется в таблицу messages — из неё потом
собирается полная переписка для карточки Trello.
"""

from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot import keyboards, texts
from app.bot.states import QUESTIONS, Lead, next_question
from app.config import Settings
from app.db import Database, now_iso
from app.parsing import normalize_phone, parse_area
from app.worktime import is_work_time, local_now, manager_eta

SCRIPT = "script"  # подпись модели для ответов по скрипту (без LLM)

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None


@dataclass
class Incoming:
    kind: str  # text | voice | contact | photo | other
    text: str | None
    file_id: str | None = None


def incoming(message: Message) -> Incoming:
    if message.contact:
        return Incoming("contact", message.contact.phone_number)
    if message.voice:
        # Этап 4: здесь будет расшифровка через Groq.
        return Incoming("voice", texts.VOICE_PLACEHOLDER, message.voice.file_id)
    if message.text:
        return Incoming("text", message.text)
    if message.photo:
        return Incoming("photo", message.caption, message.photo[-1].file_id)
    if message.document:
        return Incoming("document", message.caption, message.document.file_id)
    return Incoming("other", message.caption or f"[{message.content_type}]")


async def log_in(db: Database, lead_id: int, item: Incoming) -> None:
    await db.add_message(lead_id, direction="in", kind=item.kind, text=item.text, file_id=item.file_id)


async def say(message: Message, db: Database, lead_id: int, text: str, markup: Markup = None) -> None:
    await message.answer(text, reply_markup=markup)
    await db.add_message(lead_id, direction="out", kind="text", text=text, model=SCRIPT)


def question(state: State) -> tuple[str, Markup]:
    return {
        Lead.object.state: (texts.Q_OBJECT, keyboards.objects()),
        Lead.area.state: (texts.Q_AREA, keyboards.areas()),
        Lead.ceiling_type.state: (texts.Q_CEILING_TYPE, keyboards.ceilings()),
        Lead.phone.state: (texts.Q_PHONE, keyboards.phone()),
        Lead.measure_time.state: (texts.Q_MEASURE_TIME, keyboards.remove()),
    }[state.state]


async def ask(message: Message, db: Database, lead_id: int, state: State) -> None:
    text, markup = question(state)
    await say(message, db, lead_id, text, markup)


async def advance(message: Message, state: FSMContext, db: Database, settings: Settings, lead_id: int) -> None:
    """Перейти к следующему вопросу или завершить анкету."""
    nxt = next_question(await state.get_state())
    await state.set_state(nxt)
    if nxt != Lead.done:
        await ask(message, db, lead_id, nxt)
        return
    lead = await db.update_lead(lead_id, status="qualified", completed_at=now_iso())
    now = local_now(settings.zone)
    eta = (
        texts.DONE_ETA_DAY
        if is_work_time(now, settings.work_start, settings.work_end)
        else manager_eta(now, settings.work_start, settings.work_end)
    )
    await say(message, db, lead.id, texts.DONE.format(lead_id=lead.id, eta=eta), keyboards.remove())


async def start_lead(message: Message, state: FSMContext, db: Database, settings: Settings) -> int:
    user = message.from_user
    now = local_now(settings.zone)
    night = not is_work_time(now, settings.work_start, settings.work_end)
    lead = await db.create_lead(
        tg_user_id=user.id, chat_id=message.chat.id, name=user.full_name, username=user.username, is_night=night
    )
    await state.set_state(Lead.object)
    await state.set_data({"lead_id": lead.id})
    await log_in(db, lead.id, incoming(message))
    greeting = (
        texts.GREETING_NIGHT.format(name=user.first_name, eta=manager_eta(now, settings.work_start, settings.work_end))
        if night
        else texts.GREETING_DAY.format(name=user.first_name)
    )
    await say(message, db, lead.id, greeting)
    await ask(message, db, lead.id, Lead.object)
    return lead.id


# --- старт ---


async def on_start(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    current = await state.get_state()
    lead_id = (await state.get_data()).get("lead_id")
    if lead_id and current in {s.state for s in QUESTIONS}:
        # Анкета не закончена — продолжаем её, а не заводим новый лид.
        await log_in(db, lead_id, incoming(message))
        await say(message, db, lead_id, texts.CONTINUE)
        await ask(message, db, lead_id, next(s for s in QUESTIONS if s.state == current))
        return
    await start_lead(message, state, db, settings)


# --- ответы кнопками ---


async def on_choice(
    cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings, field: str, options: dict[str, str]
) -> None:
    code = cb.data.split(":", 1)[1]
    label = options.get(code)
    lead_id = (await state.get_data()).get("lead_id")
    if label is None or lead_id is None:
        await cb.answer()
        return
    await cb.answer()
    # Убираем кнопки у вопроса и показываем выбранный ответ.
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ {label}")
    await db.add_message(lead_id, direction="in", kind="button", text=label)
    await db.update_lead(lead_id, **({field: label} if field != "area" else {"area_text": label}))
    await advance(cb.message, state, db, settings, lead_id)


async def on_object_button(cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await on_choice(cb, state, db, settings, "object", texts.OBJECT_OPTIONS)


async def on_area_button(cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await on_choice(cb, state, db, settings, "area", texts.AREA_OPTIONS)


async def on_ceiling_button(cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await on_choice(cb, state, db, settings, "ceiling_type", texts.CEILING_OPTIONS)


async def on_stale_button(cb: CallbackQuery) -> None:
    """Кнопка от старого вопроса — молча игнорируем."""
    await cb.answer()


# --- ответы текстом / голосом ---

TEXT_OR_VOICE = F.text | F.voice


async def on_object_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    lead_id = (await state.get_data())["lead_id"]
    item = incoming(message)
    await log_in(db, lead_id, item)
    await db.update_lead(lead_id, object=item.text)
    await advance(message, state, db, settings, lead_id)


async def on_area_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    lead_id = data["lead_id"]
    item = incoming(message)
    await log_in(db, lead_id, item)
    area = parse_area(item.text) if item.kind == "text" else None
    if area is None and item.kind == "text" and not data.get("area_retry"):
        # Переспрашиваем один раз, дальше принимаем как есть — менеджер разберётся.
        await state.update_data(area_retry=True)
        await say(message, db, lead_id, texts.Q_AREA_RETRY, keyboards.areas())
        return
    await db.update_lead(lead_id, area_m2=area, area_text=item.text)
    await advance(message, state, db, settings, lead_id)


async def on_ceiling_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    lead_id = (await state.get_data())["lead_id"]
    item = incoming(message)
    await log_in(db, lead_id, item)
    await db.update_lead(lead_id, ceiling_type=item.text)
    await advance(message, state, db, settings, lead_id)


async def on_phone(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    lead_id = (await state.get_data())["lead_id"]
    item = incoming(message)
    await log_in(db, lead_id, item)
    if item.kind == "contact":
        phone = normalize_phone(item.text) or item.text
    elif item.kind == "text" and item.text == texts.NO_PHONE:
        phone = texts.NO_PHONE_VALUE
    elif item.kind == "voice":
        phone = item.text  # номер разберём из расшифровки (этап 4/5)
    else:
        phone = normalize_phone(item.text)
        if phone is None:
            await say(message, db, lead_id, texts.Q_PHONE_RETRY, keyboards.phone())
            return
    await db.update_lead(lead_id, phone=phone)
    await advance(message, state, db, settings, lead_id)


async def on_measure_time(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    lead_id = (await state.get_data())["lead_id"]
    item = incoming(message)
    await log_in(db, lead_id, item)
    await db.update_lead(lead_id, measure_time=item.text)
    await advance(message, state, db, settings, lead_id)


# --- после анкеты ---


async def on_after_done(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    lead_id = data["lead_id"]
    await log_in(db, lead_id, incoming(message))
    # Этап 3: уведомление менеджеру. Подтверждаем клиенту только первый раз, чтобы не спамить.
    if not data.get("acked"):
        await state.update_data(acked=True)
        await say(message, db, lead_id, texts.AFTER_DONE_ACK)


# --- первое сообщение без /start ---


async def on_first_message(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    await start_lead(message, state, db, settings)


# --- фото/стикеры/документы посреди анкеты ---


async def on_other_content(message: Message, state: FSMContext, db: Database) -> None:
    lead_id = (await state.get_data()).get("lead_id")
    current = await state.get_state()
    if lead_id is None or current is None:
        return
    await log_in(db, lead_id, incoming(message))
    await say(message, db, lead_id, texts.NON_TEXT_ACK)
    await ask(message, db, lead_id, next(s for s in QUESTIONS if s.state == current))


def create_router() -> Router:
    """Порядок регистрации важен: aiogram берёт первый подходящий обработчик."""
    r = Router(name="dialog")
    r.message.register(on_start, CommandStart())

    r.callback_query.register(on_object_button, Lead.object, F.data.startswith("obj:"))
    r.callback_query.register(on_area_button, Lead.area, F.data.startswith("area:"))
    r.callback_query.register(on_ceiling_button, Lead.ceiling_type, F.data.startswith("ct:"))
    r.callback_query.register(on_stale_button)

    r.message.register(on_object_text, Lead.object, TEXT_OR_VOICE)
    r.message.register(on_area_text, Lead.area, TEXT_OR_VOICE)
    r.message.register(on_ceiling_text, Lead.ceiling_type, TEXT_OR_VOICE)
    r.message.register(on_phone, Lead.phone, F.contact | TEXT_OR_VOICE)
    r.message.register(on_measure_time, Lead.measure_time, TEXT_OR_VOICE)

    r.message.register(on_after_done, Lead.done)
    r.message.register(on_first_message, StateFilter(None))
    r.message.register(on_other_content)
    return r
