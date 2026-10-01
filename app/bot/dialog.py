"""Общее для диалога с клиентом: входящее сообщение (с расшифровкой голосового), запись переписки,
вопросы анкеты и переход к следующему. Используют анкета (handlers) и правка заявки (/order)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot import keyboards, texts
from app.bot.assistant import missing_fields
from app.bot.states import Lead
from app.config import Settings
from app.db import STT_SHADOW, STT_TRANSCRIBE, Database, now_iso
from app.parsing import clip
from app.services.stt import SpeechService
from app.worktime import is_work_time, local_now, manager_eta

log = logging.getLogger(__name__)

SCRIPT = "script"  # подпись модели для ответов по скрипту (без LLM)
STT_TIMEOUT = 25  # с: сколько клиент ждёт расшифровку, прежде чем бот пойдёт дальше без неё

# Защита от флуда и расхода токенов за счёт студии.
MAX_VOICE_SEC = 300     # голосовые длиннее не расшифровываем: менеджер прослушает вложение
# Каждое входящее — запись в базе и комментарий в Trello (у голосового ещё вложение и Groq). Сверх этих
# лимитов на заявку сообщения не сохраняются и никуда не уходят: человек так не пишет, это флуд.
FLOOD_PER_MIN = 20
FLOOD_PER_DAY = 300
FLOOD_NOTICE_EVERY = timedelta(hours=1)  # предупреждение клиенту — не чаще


FIELD_STATE = {
    "object": Lead.object,
    "area": Lead.area,
    "ceiling_type": Lead.ceiling_type,
    "phone": Lead.phone,
    "measure_time": Lead.measure_time,
}
STATE_FIELD = {s.state: f for f, s in FIELD_STATE.items()}

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None


@dataclass
class Incoming:
    kind: str  # text | voice | contact | photo | document | video_note | other
    text: str | None
    file_id: str | None = None
    duration: int = 0  # секунд, для голосовых
    stt_model: str | None = None  # какая модель расшифровала голосовое
    shadow: bool = False  # после записи — теневая расшифровка другой моделью (для сравнения)

    @property
    def pending(self) -> bool:
        """Голосовое, которое ещё не расшифровано."""
        return self.kind == "voice" and self.text is None

    @property
    def answer(self) -> str:
        """Значение для поля анкеты (не длиннее FIELD_MAX: клиент может прислать 4096 символов)."""
        return texts.VOICE_PLACEHOLDER if self.pending else clip(self.text or "")


def incoming(message: Message) -> Incoming:
    if message.contact:
        return Incoming("contact", message.contact.phone_number)
    if message.voice:
        return Incoming("voice", None, message.voice.file_id, message.voice.duration or 0)
    if message.text:
        return Incoming("text", message.text)
    if message.photo:
        return Incoming("photo", message.caption, message.photo[-1].file_id)
    if message.document:
        return Incoming("document", message.caption, message.document.file_id)
    if message.video_note:
        return Incoming("video_note", None, message.video_note.file_id)
    return Incoming("other", message.caption or f"[{message.content_type}]")


async def receive(message: Message, stt: SpeechService | None) -> Incoming:
    item = incoming(message)
    if item.kind == "voice" and item.duration > MAX_VOICE_SEC:
        item.text = texts.VOICE_TOO_LONG.format(minutes=round(item.duration / 60))
        return item
    if item.kind == "voice" and stt is not None:
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
            # Модели делят STT_TIMEOUT между собой; внешний таймаут — страховка на скачивание файла.
            result = await asyncio.wait_for(stt.transcribe(item.file_id, STT_TIMEOUT), STT_TIMEOUT + 5)
            item.text, item.stt_model, item.shadow = result.text, result.model, stt.shadow
        except Exception as e:  # noqa: BLE001 — любая ошибка: расшифруем позже из очереди
            log.warning("Голосовое не расшифровано сразу (%s), ставлю в очередь", str(e) or type(e).__name__)
    return item


async def receive_middleware(
    handler: Callable[[Message, dict[str, Any]], Awaitable[Any]], event: Message, data: dict[str, Any]
) -> Any:
    """Готовит входящее сообщение (с расшифровкой голосового) для обработчика — аргумент item.
    При SkipHandler следующий обработчик получает те же data — второй раз не расшифровываем."""
    if "item" not in data:
        data["item"] = await receive(event, data.get("stt"))
    return await handler(event, data)


async def flood_middleware(
    handler: Callable[[Message, dict[str, Any]], Awaitable[Any]], event: Message, data: dict[str, Any]
) -> Any:
    """Отсекает флуд до расшифровки и записи: лишние сообщения не стоят ни места в базе, ни вызовов API."""
    state: FSMContext | None = data.get("state")
    db: Database | None = data.get("db")
    lead_id = (await state.get_data()).get("lead_id") if state else None
    if lead_id is None or db is None:
        return await handler(event, data)
    now = datetime.now(UTC)
    if (
        await db.count_incoming_since(lead_id, now - timedelta(minutes=1)) < FLOOD_PER_MIN
        and await db.count_incoming_since(lead_id, now - timedelta(days=1)) < FLOOD_PER_DAY
    ):
        return await handler(event, data)
    warned = (await state.get_data()).get("flood_warned_at")
    if warned is None or now - datetime.fromisoformat(warned) >= FLOOD_NOTICE_EVERY:
        log.warning("Флуд от клиента по заявке %s — сообщения не сохраняются", lead_id)
        await state.update_data(flood_warned_at=now.isoformat())
        await event.answer(texts.FLOOD)
    return None


async def log_in(db: Database, lead_id: int, item: Incoming, field: str | None = None) -> None:
    """Записать входящее. Нерасшифрованное голосовое — в очередь; field — поле анкеты, куда лечь тексту."""
    msg_id = await db.add_message(
        lead_id, direction="in", kind=item.kind, text=item.text, file_id=item.file_id, model=item.stt_model
    )
    if item.shadow:
        await db.enqueue(STT_SHADOW, lead_id, {"message_id": msg_id})
    if item.pending:
        await db.enqueue(
            STT_TRANSCRIBE, lead_id, {"message_id": msg_id, "field": field, "placeholder": texts.VOICE_PLACEHOLDER}
        )


async def say(
    message: Message, db: Database, lead_id: int, text: str, markup: Markup = None, *, model: str = SCRIPT
) -> None:
    """Ответ клиенту. model — кто сформировал текст; клиент подпись не видит, она идёт в Trello и менеджеру."""
    await message.answer(text, reply_markup=markup)
    await db.add_message(lead_id, direction="out", kind="text", text=text, model=model)


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


def done_eta(settings: Settings) -> str:
    now = local_now(settings.zone)
    if is_work_time(now, settings.work_start, settings.work_end):
        return texts.DONE_ETA_DAY
    return manager_eta(now, settings.work_start, settings.work_end)


async def complete(message: Message, state: FSMContext, db: Database, settings: Settings, lead_id: int) -> None:
    await state.set_state(Lead.done)
    # Ответы анкеты менеджер увидит в уведомлении о заявке — в «клиент дописал» их не повторяем.
    answers = await db.get_messages(lead_id, direction="in")
    lead = await db.update_lead(
        lead_id, status="qualified", completed_at=now_iso(), client_msgs_notified=answers[-1].id if answers else 0
    )
    text = texts.DONE.format(lead_id=lead.id, eta=done_eta(settings))
    await say(message, db, lead.id, text, keyboards.remove())


async def advance(message: Message, state: FSMContext, db: Database, settings: Settings, lead_id: int) -> None:
    """Следующий вопрос — первое незаполненное поле (LLM могла заполнить несколько сразу), или завершение."""
    await state.update_data(llm_stalls=0)
    lead = await db.get_lead(lead_id)
    missing = missing_fields(lead)
    if not missing:
        await complete(message, state, db, settings, lead_id)
        return
    nxt = FIELD_STATE[missing[0]]
    await state.set_state(nxt)
    await ask(message, db, lead_id, nxt)


