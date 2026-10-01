"""Диалог квалификации: 4 вопроса → заявка (4-й — телефон и время замера вместе).

Каждое входящее и исходящее сообщение пишется в таблицу messages — из неё потом
собирается полная переписка для карточки Trello. Голосовые расшифровываются сразу
(middleware receive_middleware), а если Groq не успел — расшифровка ждёт в очереди.

Свободный текст и голос сначала обрабатывает LLM (on_llm_answer): отвечает на вопросы клиента
и извлекает поля анкеты. Если LLM недоступна или дважды не продвинула текущий вопрос —
SkipHandler, и сообщение обрабатывает скрипт (обработчики ниже), как без LLM.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Message,
    User,
)

from app.bot import keyboards, texts
from app.bot.assistant import LeadAssistant, merge_object, missing_fields
from app.bot.dialog import (
    FIELD_STATE,
    STATE_FIELD,
    Incoming,
    advance,
    ask,
    complete,
    done_eta,
    flood_middleware,
    log_in,
    question,
    receive_middleware,
    say,
)
from app.bot.order import leave_edit, on_delete_button, on_edit_button, on_edit_choice, on_edit_text, on_order
from app.bot.states import EDITS, QUESTIONS, EditLead, Lead
from app.config import Settings
from app.db import TG_CLIENT_MSG, Database
from app.parsing import clip, normalize_phone, parse_area, replace_phones
from app.services.llm import LLMError
from app.worktime import is_work_time, local_now, manager_eta

log = logging.getLogger(__name__)

LLM_MAX_STALLS = 2  # столько ответов подряд LLM ничего не извлекла из сообщения — дальше вопрос ведёт скрипт
LLM_RATE_MAX = 8        # сообщений клиента за минуту — больше, и отвечает скрипт, без LLM
LLM_CALLS_MAX = 40      # обращений к LLM на одну заявку (счётчик в базе — /start его не обнуляет)
LEADS_PER_DAY = 3       # новых заявок от одного человека за сутки; дальше — продолжаем последнюю
# Метка источника из ссылки t.me/<бот>?start=<метка> — в том виде, в каком её допускает Telegram.
_SOURCE = re.compile(r"[A-Za-z0-9_-]{1,64}")


async def llm_turn(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming,
    assistant: LeadAssistant, *, logged: bool = False,
) -> bool:
    """Шаг диалога через LLM. False — бюджет заявки исчерпан или ни одна модель не справилась;
    тогда в базе ничего не изменено, кроме счётчика обращений."""
    data = await state.get_data()
    lead_id = data["lead_id"]
    if not await db.spend_llm_call(lead_id, LLM_CALLS_MAX):
        return False  # лимит обращений к LLM на заявку исчерпан — дальше скрипт
    current = await state.get_state()
    done = current == Lead.done.state
    lead = await db.get_lead(lead_id)
    history = await db.get_messages(lead_id)
    if logged:
        # Сообщение уже записано, а после него — приветствие бота. Модели оно нужно последним,
        # иначе она решит, что уже ответила.
        last_in = max((i for i, m in enumerate(history) if m.direction == "in"), default=None)
        if last_in is not None:
            history = history[:last_in] + history[last_in + 1 :]
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        turn = await assistant.dialog_turn(lead, history, item.text, done=done, eta=done_eta(settings))
    except LLMError as e:
        log.warning("LLM недоступна, отвечаю по скрипту: %s", e)
        return False

    if not logged:
        await log_in(db, lead_id, item, STATE_FIELD.get(current))
    if turn.updates:
        if "object" in turn.updates:  # «в спальню» после кнопки «Квартира» — дописываем, а не заменяем
            turn.updates["object"] = merge_object(lead.object, turn.updates["object"])
        lead = await db.update_lead(lead_id, **turn.updates)

    if done:
        await say(message, db, lead_id, turn.reply, model=turn.model)
        await db.enqueue(TG_CLIENT_MSG, lead_id, coalesce=True, delay=timedelta(seconds=settings.client_msg_delay_sec))
        return True

    missing = missing_fields(lead)
    await state.update_data(llm_stalls=0 if turn.updates else data.get("llm_stalls", 0) + 1)
    if not missing:
        # Ответ модели нужен, только если клиент о чём-то спросил, а модель не задаёт вопросов сама.
        if item.text and "?" in item.text and turn.asks is None:
            await say(message, db, lead_id, turn.reply, model=turn.model)
        await complete(message, state, db, settings, lead_id)
        return True
    # Переходим к вопросу, который задала модель (чтобы кнопки совпали с вопросом), иначе — к первому недостающему.
    nxt = FIELD_STATE[turn.asks if turn.asks in missing else missing[0]]
    await state.set_state(nxt)
    await say(message, db, lead_id, turn.reply, question(nxt)[1], model=turn.model)
    return True


def start_source(command: CommandObject | None) -> str | None:
    """Источник заявки из /start <метка>. Всё, что не похоже на метку (её мог набрать и сам клиент), — не источник."""
    arg = (command.args or "") if command else ""
    return arg.lower() if _SOURCE.fullmatch(arg) else None


async def start_lead(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming,
    assistant: LeadAssistant | None = None, *, user: User | None = None, source: str | None = None,
) -> int:
    """Новая заявка. user — клиент, если message — не его сообщение (кнопка под сообщением бота);
    source — метка источника из ссылки, по которой клиент пришёл."""
    user = user or message.from_user
    if await db.count_leads_since(user.id, datetime.now(UTC) - timedelta(days=1)) >= LEADS_PER_DAY:
        # Кто-то жмёт /start по кругу — не плодим карточки и уведомления, продолжаем последнюю заявку.
        last = await db.last_lead(user.id)
        await state.set_state(Lead.done)
        await state.set_data({"lead_id": last.id, "acked": True})
        await log_in(db, last.id, item)
        await say(message, db, last.id, texts.LEADS_LIMIT.format(lead_id=last.id), keyboards.remove())
        return last.id
    now = local_now(settings.zone)
    night = not is_work_time(now, settings.work_start, settings.work_end)
    lead = await db.create_lead(
        tg_user_id=user.id, chat_id=message.chat.id, name=user.full_name, username=user.username, is_night=night,
        source=source,
    )
    await state.set_state(Lead.object)
    await state.set_data({"lead_id": lead.id})
    await log_in(db, lead.id, item)
    greeting = (
        texts.GREETING_NIGHT.format(name=user.first_name, eta=manager_eta(now, settings.work_start, settings.work_end))
        if night
        else texts.GREETING_DAY.format(name=user.first_name)
    )
    await say(message, db, lead.id, greeting)
    # Клиент сразу что-то написал (не /start) — пусть LLM ответит на это и спросит недостающее.
    free_text = item.kind in ("text", "voice") and item.text and not item.text.startswith("/")
    if not (free_text and assistant and await llm_turn(message, state, db, settings, item, assistant, logged=True)):
        await ask(message, db, lead.id, Lead.object)
    return lead.id


# --- старт ---


async def on_start(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming,
    assistant: LeadAssistant | None = None, command: CommandObject | None = None,
) -> None:
    current = await leave_edit(state)
    lead_id = (await state.get_data()).get("lead_id")
    source = start_source(command)
    if lead_id and current in {s.state for s in QUESTIONS}:
        # Анкета не закончена — спрашиваем: продолжить её или закрыть и начать новую.
        await state.update_data(start_source=source)  # метка достанется новой заявке, если клиент выберет её
        await log_in(db, lead_id, item)
        await say(message, db, lead_id, texts.RESTART_CHOICE.format(lead_id=lead_id), keyboards.restart(lead_id))
        return
    await start_lead(message, state, db, settings, item, assistant, source=source)


async def on_restart_button(
    cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings, assistant: LeadAssistant | None = None,
) -> None:
    _, action, raw_id = (cb.data.split(":") + ["", ""])[:3]
    current = await state.get_state()
    data = await state.get_data()
    lead_id = data.get("lead_id")
    label = {"continue": texts.RESTART_CONTINUE, "new": texts.RESTART_NEW}.get(action)
    await cb.answer()
    if (
        label is None or not isinstance(cb.message, Message) or raw_id != str(lead_id)
        or current not in {s.state for s in QUESTIONS}
    ):
        return  # старая кнопка (заявка уже сменилась или закончена) или подделанный callback
    await cb.message.edit_text(f"{cb.message.text}\n\n✓ {label}")
    await db.add_message(lead_id, direction="in", kind="button", text=label)
    question = next(s for s in QUESTIONS if s.state == current)
    if action == "continue":
        await say(cb.message, db, lead_id, texts.CONTINUE)
        await ask(cb.message, db, lead_id, question)
        return
    if await db.count_leads_since(cb.from_user.id, datetime.now(UTC) - timedelta(days=1)) >= LEADS_PER_DAY:
        # Лимит заявок в сутки: старую не закрываем (иначе клиент остался бы без заявки), продолжаем её.
        await say(cb.message, db, lead_id, texts.RESTART_LIMIT.format(limit=LEADS_PER_DAY, lead_id=lead_id))
        await ask(cb.message, db, lead_id, question)
        return
    # Закрыта клиентом: менеджеру о ней не сообщаем и не напоминаем, в Trello — метка «закрыта клиентом».
    await db.update_lead(lead_id, status="cancelled")
    await say(cb.message, db, lead_id, texts.RESTART_CLOSED.format(lead_id=lead_id))
    await start_lead(cb.message, state, db, settings, Incoming("button", label), assistant, user=cb.from_user,
                     source=data.get("start_source"))


# --- ответы через LLM ---


async def on_llm_answer(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming,
    assistant: LeadAssistant | None = None,
) -> None:
    if (
        assistant is None
        or item.pending
        or item.kind not in ("text", "voice")
        or not item.text
        or item.text.startswith("/")
        or item.text == texts.NO_PHONE  # кнопка «напишите в Telegram» — детерминированно, скриптом
    ):
        raise SkipHandler
    current = await state.get_state()
    data = await state.get_data()
    if current != Lead.done.state and data.get("llm_stalls", 0) >= LLM_MAX_STALLS:
        raise SkipHandler
    minute_ago = datetime.now(UTC) - timedelta(minutes=1)
    if await db.count_incoming_since(data["lead_id"], minute_ago) >= LLM_RATE_MAX:
        raise SkipHandler  # флуд — отвечаем скриптом, токены не тратим
    if not await llm_turn(message, state, db, settings, item, assistant):
        raise SkipHandler


# --- ответы кнопками ---


async def on_choice(
    cb: CallbackQuery, state: FSMContext, db: Database, settings: Settings, field: str, options: dict[str, str]
) -> None:
    code = cb.data.split(":", 1)[1]
    label = options.get(code)
    lead_id = (await state.get_data()).get("lead_id")
    if label is None or lead_id is None or not isinstance(cb.message, Message):
        # Неизвестный код (подделанный callback) или сообщение старше 48 ч, которое уже нельзя править.
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

async def on_object_text(message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming) -> None:
    lead_id = (await state.get_data())["lead_id"]
    await log_in(db, lead_id, item, "object")
    await db.update_lead(lead_id, object=item.answer)
    await advance(message, state, db, settings, lead_id)


async def on_area_text(message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming) -> None:
    data = await state.get_data()
    lead_id = data["lead_id"]
    await log_in(db, lead_id, item, "area")
    area = parse_area(item.text) if item.text else None
    if area is None and not item.pending and not data.get("area_retry"):
        # Переспрашиваем один раз, дальше принимаем как есть — менеджер разберётся.
        await state.update_data(area_retry=True)
        await say(message, db, lead_id, texts.Q_AREA_RETRY, keyboards.areas())
        return
    await db.update_lead(lead_id, area_m2=area, area_text=item.answer)
    await advance(message, state, db, settings, lead_id)


async def on_ceiling_text(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming
) -> None:
    lead_id = (await state.get_data())["lead_id"]
    await log_in(db, lead_id, item, "ceiling_type")
    await db.update_lead(lead_id, ceiling_type=item.answer)
    await advance(message, state, db, settings, lead_id)


# Признаки времени замера в ответе рядом с номером; без них остаток («мой номер») временем не считаем.
_TIME_CUES = re.compile(
    r"понедельн|вторник|сред[ауы]|четверг|пятниц|суббот|воскресен|выходн|будн|сегодня|завтра|утр|вечер|"
    r"дн[её]м|обед|ноч|час|любое|недел|числ|\bпосле\b|\bс\s*\d|\bдо\s*\d|\d{1,2}[:.]\d{2}",
    re.IGNORECASE,
)
_FILLER = re.compile(r"\b(мой|моя|номер|телефон|тел|звоните|позвоните|пишите|вот)\b\.?", re.IGNORECASE)


def measure_time_from(rest: str) -> str | None:
    """Время замера из остатка ответа после номера телефона, если оно там есть."""
    if not _TIME_CUES.search(rest):
        return None
    when = re.sub(r"\s+", " ", _FILLER.sub(" ", rest)).strip(" ,.;:—–-")
    return clip(when) if when else None


async def on_phone(message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming) -> None:
    lead_id = (await state.get_data())["lead_id"]
    await log_in(db, lead_id, item, "phone")
    if item.kind == "contact":
        phone = normalize_phone(item.text) or clip(item.text)
    elif item.kind == "text" and item.text == texts.NO_PHONE:
        phone = texts.NO_PHONE_VALUE
    elif item.pending:
        phone = item.answer  # номер подставится из расшифровки, когда она будет готова
    else:
        # 4-й вопрос просит номер и время замера сразу: «8 912 345-67-89, в субботу после обеда».
        found: list[str] = []
        rest = replace_phones(item.text or "", lambda p: found.append(p) or " ")
        phone = found[0] if found else normalize_phone(item.text or "")
        if phone is None:
            await say(message, db, lead_id, texts.Q_PHONE_RETRY, keyboards.phone())
            return
        if when := measure_time_from(rest):
            await db.update_lead(lead_id, phone=phone, measure_time=when)
            await advance(message, state, db, settings, lead_id)
            return
    await db.update_lead(lead_id, phone=phone)
    await advance(message, state, db, settings, lead_id)


async def on_measure_time(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming
) -> None:
    lead_id = (await state.get_data())["lead_id"]
    await log_in(db, lead_id, item, "measure_time")
    await db.update_lead(lead_id, measure_time=item.answer)
    await advance(message, state, db, settings, lead_id)


# --- после анкеты ---


async def on_after_done(message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming) -> None:
    data = await state.get_data()
    lead_id = data["lead_id"]
    await log_in(db, lead_id, item)
    # Несколько сообщений подряд собираем в одно уведомление менеджеру.
    await db.enqueue(TG_CLIENT_MSG, lead_id, coalesce=True, delay=timedelta(seconds=settings.client_msg_delay_sec))
    # Клиенту подтверждаем только первый раз, чтобы не спамить.
    if not data.get("acked"):
        await state.update_data(acked=True)
        await say(message, db, lead_id, texts.AFTER_DONE_ACK)


# --- первое сообщение без /start ---


async def on_first_message(
    message: Message, state: FSMContext, db: Database, settings: Settings, item: Incoming,
    assistant: LeadAssistant | None = None,
) -> None:
    await start_lead(message, state, db, settings, item, assistant)


# --- фото/стикеры/документы посреди анкеты ---


async def on_other_content(message: Message, state: FSMContext, db: Database, item: Incoming) -> None:
    lead_id = (await state.get_data()).get("lead_id")
    current = await state.get_state()
    if lead_id is None or current is None:
        return
    await log_in(db, lead_id, item)
    await say(message, db, lead_id, texts.NON_TEXT_ACK)
    if question_state := next((s for s in QUESTIONS if s.state == current), None):
        await ask(message, db, lead_id, question_state)


def create_router() -> Router:
    """Порядок регистрации важен: aiogram берёт первый подходящий обработчик."""
    r = Router(name="dialog")
    # Анкета — только в личке с клиентом; в группе менеджеров бот не задаёт вопросов.
    r.message.filter(F.chat.type == "private")
    r.callback_query.filter(F.message.chat.type == "private")
    r.message.outer_middleware(flood_middleware)  # до фильтров и расшифровки: один раз на сообщение
    r.message.middleware(receive_middleware)
    r.message.register(on_start, CommandStart())
    r.message.register(on_order, Command("order"))

    r.callback_query.register(on_object_button, Lead.object, F.data.startswith("obj:"))
    r.callback_query.register(on_area_button, Lead.area, F.data.startswith("area:"))
    r.callback_query.register(on_ceiling_button, Lead.ceiling_type, F.data.startswith("ct:"))
    r.callback_query.register(on_restart_button, F.data.startswith("restart:"))
    r.callback_query.register(on_edit_choice, EditLead.object, F.data.startswith("obj:"))
    r.callback_query.register(on_edit_choice, EditLead.area, F.data.startswith("area:"))
    r.callback_query.register(on_edit_choice, EditLead.ceiling_type, F.data.startswith("ct:"))
    r.callback_query.register(on_edit_button, F.data.startswith("edit:"))
    r.callback_query.register(on_delete_button, F.data.startswith("delete:"))
    r.callback_query.register(on_stale_button)

    r.message.register(on_llm_answer, StateFilter(*QUESTIONS, Lead.done), TEXT_OR_VOICE)
    r.message.register(on_object_text, Lead.object, TEXT_OR_VOICE)
    r.message.register(on_area_text, Lead.area, TEXT_OR_VOICE)
    r.message.register(on_ceiling_text, Lead.ceiling_type, TEXT_OR_VOICE)
    r.message.register(on_phone, Lead.phone, F.contact | TEXT_OR_VOICE)
    r.message.register(on_measure_time, Lead.measure_time, TEXT_OR_VOICE)

    r.message.register(on_edit_text, StateFilter(*EDITS.values()))
    r.message.register(on_after_done, Lead.done)
    r.message.register(on_first_message, StateFilter(None))
    r.message.register(on_other_content)
    return r
