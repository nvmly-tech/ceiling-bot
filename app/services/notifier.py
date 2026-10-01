"""Уведомления менеджерам в Telegram: новый лид, брошенная анкета, напоминания, утренний дайджест,
панель взятой заявки с кнопками этапов, напоминания о взятых заявках без итога и эскалации владельцу.

Все отправки идут через outbox (ретраи, порядок). Решения «пора уведомить / напомнить» принимает
планировщик scan() по состоянию базы, поэтому после рестарта ничего не теряется и не дублируется.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from html import escape, unescape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReplyParameters

from app.bot.assistant import LeadAssistant
from app.config import Settings
from app.db import (
    TG_CLIENT_MSG,
    TG_DELETED,
    TG_DIGEST,
    TG_ESCALATE,
    TG_FEEDBACK,
    TG_LEAD,
    TG_NUDGE,
    TG_PANEL,
    TG_REMIND,
    TG_VISIT,
    Database,
    Lead,
    OutboxTask,
    now_iso,
)
from app.parsing import clip
from app.services.inwork import (
    LOW_RATING,
    escalate_due,
    escalation_text,
    feedback_text,
    in_work_on,
    mention,
    nudge_due,
    nudge_text,
    parse_ts,
    span,
    talking_to_manager,
    visit_text,
)
from app.services.llm import LLMError
from app.stages import (
    CONTRACT,
    MEASURE,
    NEXT,
    NO_ANSWER,
    REFUSED,
    REOPEN,
    THINKING,
    history_lines,
    stage_text,
)
from app.worktime import last_work_end, next_work_start, work_time

log = logging.getLogger(__name__)

KV_CHAT_ID = "manager_chat_id"      # новый id группы после её превращения в супергруппу
KV_LAST_DIGEST = "last_digest_date"  # дата (по часовому поясу студии) последнего дайджеста
CARD_WAIT_ATTEMPTS = 3              # сколько раз подождать карточку Trello, прежде чем слать без ссылки
TG_LIMIT = 4096                     # лимит длины сообщения Telegram
CLIENT_LINE_MAX = 300               # «клиент дописал»: длина одной строки
CLIENT_TOTAL_MAX = 3000             # и всех строк вместе
# Резюме — дополнение, а не суть уведомления. Очередь outbox однопоточная: пока ждём LLM, стоят и Trello,
# и другие уведомления. Без бюджета две «висящие» модели держали бы её 2×LLM_TIMEOUT_SEC (~30 с).
SUMMARY_BUDGET = 8                  # с
# Telegram даёт боту удалять свои сообщения только 48 ч; берём с запасом — удаление идёт через очередь.
TG_DELETE_WINDOW = timedelta(hours=47)
LIST_MAX = 20                       # заявок в одном списке (сводка, напоминание, эскалация); дальше — «и ещё N»
# Списки заявок одним сообщением: вид → (заголовок, кнопки «Взял», сколько ждёт). Утром после ночи с заявками
# напоминания о каждой по отдельности — это десятки сообщений разом, а Telegram пускает в группу 20 в минуту.
LISTS = {
    "digest": ("☀️ <b>Доброе утро! Заявки за нерабочее время ждут менеджера: {n}</b>", True, False),
    "remind_list": ("⏰ <b>Заявки ждут менеджера: {n}</b>", True, True),
    "untaken_list": ("⚠️ <b>Заявки так никто и не взял: {n}</b>", False, True),
}
# Ответы клиента на вопросы бота уходят своими уведомлениями — в «клиент дописал» их не повторяем.
ANSWER_KINDS = {"visit", "contact", "rating"}


class NotReady(Exception):
    """Задачу рано выполнять — outbox повторит её позже."""


# --- тексты ---


HOT_ICONS = {"горячий": "🔥", "тёплый": "🌤", "холодный": "❄️"}


def _area(lead: Lead) -> str | None:
    return f"~{lead.area_m2:g} м²" if lead.area_m2 is not None else lead.area_text


def lead_body(lead: Lead, previous: Sequence[Lead] = (), zone: ZoneInfo | None = None) -> str:
    who = escape(lead.name or "Клиент")
    if lead.username:
        who += f" (@{escape(lead.username)})"
    facts = " · ".join(escape(x) for x in (lead.object, _area(lead), lead.ceiling_type) if x)
    lines = [f"👤 {who}"]
    if facts:
        lines.append(f"🏠 {facts}")
    if lead.phone:
        lines.append(f"📱 {escape(lead.phone)}")
    if lead.measure_time:
        lines.append(f"🗓 Замер: {escape(lead.measure_time)}")
    if lead.source:
        lines.append(f"📣 Источник: {escape(lead.source)}")
    if previous and zone:
        lines.append("🔁 <b>Уже обращался:</b>")
        lines.extend(f"• {escape(line)}" for line in history_lines(previous, zone))
    if lead.hotness:
        reason = f" — {escape(lead.hotness_reason)}" if lead.hotness_reason else ""
        lines.append(f"{HOT_ICONS.get(lead.hotness, '')} <b>{escape(lead.hotness)}</b>{reason}")
    if lead.summary:
        lines.append(f"📝 {escape(lead.summary)}")
        lines.append(f"<i>— резюме: {escape(lead.summary_model or '')}</i>")
    if lead.trello_card_url:
        lines.append(f'📋 <a href="{escape(lead.trello_card_url)}">Карточка в Trello</a>')
    return "\n".join(lines)


def lead_text(
    lead: Lead, reason: str, waiting_min: int | None = None, *, previous: Sequence[Lead] = (),
    zone: ZoneInfo | None = None,
) -> str:
    night = " · 🌙 ночная" if lead.is_night else ""
    header = {
        "qualified": f"🔥 <b>Новая заявка №{lead.id}</b>{night}",
        "abandoned": f"⏸ <b>Заявка №{lead.id}: анкета не завершена</b>{night}",
        "remind": f"⏰ <b>Заявку №{lead.id} никто не взял</b> — ждёт {waiting_min} мин",
    }[reason]
    return f"{header}\n\n{lead_body(lead, previous, zone)}"


def digest_text(
    leads: list[Lead], kind: str = "digest", settings: Settings | None = None, now: datetime | None = None,
) -> str:
    """Список заявок одним сообщением (сводка, напоминание, эскалация) — не больше LIST_MAX строк."""
    header, _, waits = LISTS[kind]
    lines = [header.format(n=len(leads)), ""]
    for lead in leads[:LIST_MAX]:
        facts = ", ".join(escape(x) for x in (lead.object, _area(lead), lead.phone) if x)
        status = "" if lead.status == "qualified" else " — <i>анкета не завершена</i>"
        link = f' · <a href="{escape(lead.trello_card_url)}">Trello</a>' if lead.trello_card_url else ""
        wait = ""
        if waits and settings and now and lead.notified_at:
            wait = f" — ждёт {span(now - next_work_start(parse_ts(lead.notified_at), settings))}"
        lines.append(f"• <b>№{lead.id}</b> {escape(lead.name or 'Клиент')}" + (f": {facts}" if facts else "")
                     + status + wait + link)
    if len(leads) > LIST_MAX:
        lines.append(f"… и ещё {len(leads) - LIST_MAX} — полный список в Trello")
    return "\n".join(lines)


def take_button(lead: Lead, label: str = "✅ Взял в работу") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=f"take:{lead.id}")


def chat_button(lead: Lead) -> InlineKeyboardButton | None:
    # Ссылка tg://user?id= в кнопке ломает всю отправку, если клиент закрыл профиль, — только по username.
    if lead.username:
        return InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{lead.username}")
    return None


def reply_button(lead: Lead) -> InlineKeyboardButton:
    """Ответ клиенту через бота (app/bot/relay.py) — работает и без @username клиента."""
    return InlineKeyboardButton(text="💬 Ответить через бота", callback_data=f"reply:{lead.id}")


def lead_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    row = [take_button(lead)]
    if chat := chat_button(lead):
        row.append(chat)
    return InlineKeyboardMarkup(inline_keyboard=[row, [reply_button(lead)]])


def panel_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    """Панель заявки: кнопки этапов и «Ответить через бота»."""
    return InlineKeyboardMarkup(inline_keyboard=[*stage_keyboard(lead).inline_keyboard, [reply_button(lead)]])


def digest_keyboard(leads: list[Lead]) -> InlineKeyboardMarkup:
    buttons = [take_button(lead, f"✅ Взял №{lead.id}") for lead in leads[:LIST_MAX]]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + 3] for i in range(0, len(buttons), 3)])


# Панель взятой заявки: кнопка → действие в callback «st:<заявка>:<действие>».
# Замер и отказ открывают выбор даты / причины (app/bot/outcomes.py), остальное меняет этап сразу.
STAGE_BUTTONS = {
    MEASURE: ("📅 Замер назначен", "measure"),
    NO_ANSWER: ("📵 Не дозвонился", "no_answer"),
    REFUSED: ("❌ Отказ", "refuse"),
    THINKING: ("🤔 Думает", "thinking"),
    CONTRACT: ("✅ Договор", "contract"),
    REOPEN: ("↩️ Вернуть в работу", "reopen"),
}


# Что клиент ответил о назначенном замере — строкой на панели.
VISIT_MARKS = {"yes": "✅ подтвердил замер", "move": "🔁 просит перенести замер", "cancel": "❌ отменил замер"}


def stage_keyboard(lead: Lead) -> InlineKeyboardMarkup:
    buttons = []
    for stage in NEXT[lead.stage]:
        label, action = STAGE_BUTTONS[stage]
        if stage == MEASURE and lead.stage == MEASURE:
            label = "📅 Перенести замер"
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"st:{lead.id}:{action}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def panel_text(lead: Lead, zone: ZoneInfo) -> str:
    """Панель заявки: кто клиент, кто ведёт, на каком этапе. Телефон — чтобы звонить прямо отсюда."""
    who = escape(lead.name or "Клиент") + (f" · {escape(lead.phone)}" if lead.phone else "")
    lines = [
        f"📋 <b>№{lead.id}</b> · {who}",
        f"Ведёт: <b>{escape(lead.taken_by_name or '—')}</b>",
        f"Этап: {escape(stage_text(lead.stage, lead.measure_at, lead.refuse_reason, zone))}",
    ]
    if lead.stage == MEASURE and lead.visit_answer:
        lines.append(f"Клиент: {VISIT_MARKS[lead.visit_answer]}")
    if lead.stage is None:
        lines.append("Отметьте итог, когда он будет 👇")
    return "\n".join(lines)


# --- сервис ---


class Notifier:
    def __init__(
        self, bot: Bot, db: Database, settings: Settings, *, trello_enabled: bool, scan_interval: float = 20,
        assistant: LeadAssistant | None = None,
    ):
        self.bot, self.db, self.settings = bot, db, settings
        self.trello_enabled = trello_enabled
        self.scan_interval = scan_interval
        self.last_scan: datetime | None = None  # для сторожа
        self.assistant = assistant  # резюме лида от LLM; без него уведомления уходят без резюме
        # Другие проверки по расписанию (сообщения клиенту): идут в том же цикле, что и scan().
        self.extra_scans: list[Callable[[datetime], Awaitable[None]]] = []

    @property
    def handlers(self):
        return {
            TG_LEAD: self.send_lead,
            TG_REMIND: self.send_reminder,
            TG_CLIENT_MSG: self.send_client_messages,
            TG_DIGEST: self.send_digest,
            TG_DELETED: self.send_deleted,
            TG_PANEL: self.send_panel,
            TG_NUDGE: self.send_nudge,
            TG_ESCALATE: self.send_escalation,
            TG_VISIT: self.send_visit,
            TG_FEEDBACK: self.send_feedback,
        }

    async def switch_chat(self, old_id: int, new_id: int) -> None:
        """Группу менеджеров превратили в супергруппу — у неё новый id (служебное сообщение Telegram)."""
        if old_id != await self.chat_id():
            return
        await self.db.kv_set(KV_CHAT_ID, str(new_id))
        log.error("Чат менеджеров сменил id: %s → %s. Обновите MANAGER_CHAT_ID в env-файле.", old_id, new_id)

    async def chat_id(self) -> int | None:
        override = await self.db.kv_get(KV_CHAT_ID)
        return int(override) if override else self.settings.manager_chat_id

    def _silent(self) -> bool:
        """Ночью уведомления приходят без звука."""
        now = datetime.now(self.settings.zone)
        return not work_time(now, self.settings)

    async def _send(
        self, text: str, markup: InlineKeyboardMarkup | None = None, *, lead_ids: Sequence[int] = (), kind: str = "",
        reply_to: int | None = None, to: int | None = None,
    ) -> Message:
        """Сообщение в группу менеджеров (или в чат to). lead_ids — о каких заявках: номер сообщения запоминаем,
        чтобы удалить его, если клиент удалит заявку. reply_to — ответом на это сообщение (если оно ещё есть)."""
        chat_id = to or await self.chat_id()
        if chat_id is None:
            raise NotReady("MANAGER_CHAT_ID не задан")
        parse_mode: str | None = "HTML"
        if len(text) > TG_LIMIT:
            # Не должно случаться (поля обрезаны), но длинное сообщение Telegram не примет никогда —
            # задача застряла бы в очереди. Шлём урезанный простой текст: резать HTML опасно.
            log.error("Уведомление длиннее %s символов (%s) — отправляю урезанным", TG_LIMIT, len(text))
            text, parse_mode = clip(unescape(re.sub(r"<[^>]+>", "", text)), TG_LIMIT - 96), None
        kwargs = dict(
            text=text,
            parse_mode=parse_mode,
            reply_markup=markup,
            disable_notification=self._silent(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        if reply_to:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        try:
            sent = await self.bot.send_message(chat_id, **kwargs)
        except TelegramMigrateToChat as e:
            # Группу превратили в супергруппу — у неё новый id. Группу менеджеров запоминаем; шлём в новую.
            new_id = e.migrate_to_chat_id
            if to is None:
                await self.db.kv_set(KV_CHAT_ID, str(new_id))
            setting = "OWNER_CHAT_ID" if to else "MANAGER_CHAT_ID"
            log.error("Чат сменил id: %s → %s. Обновите %s в env-файле.", chat_id, new_id, setting)
            sent = await self.bot.send_message(new_id, **kwargs)
        if lead_ids:
            await self.db.add_tg_message(lead_ids, sent.chat.id, sent.message_id, kind)
        return sent

    async def to_owner(self, text: str, *, lead_ids: Sequence[int] = (), kind: str = "") -> Message:
        """Сообщение владельцу: в OWNER_CHAT_ID, а если он не задан — в группу менеджеров."""
        return await self._send(text, lead_ids=lead_ids, kind=kind, to=self.settings.owner_chat_id)

    async def _lead(self, task: OutboxTask) -> Lead:
        lead = await self.db.get_lead(task.lead_id)
        if lead is None:
            raise ValueError(f"lead {task.lead_id} not found")
        if self.trello_enabled and not lead.trello_card_url and task.attempts < CARD_WAIT_ATTEMPTS:
            raise NotReady("ждём карточку Trello, чтобы дать ссылку")
        return lead

    # --- задачи outbox ---

    async def _summarize(self, lead: Lead) -> Lead:
        """Резюме и «горячесть» для текущего статуса лида. Не вышло — уведомление уйдёт без резюме."""
        if self.assistant is None or lead.summary_status == lead.status:
            return lead
        day = datetime.now(self.settings.zone).date().isoformat()
        if not await self.db.spend_llm_day(self.settings.llm_calls_per_day, day):
            log.warning("Резюме лида %s не запрошено: дневной лимит обращений к LLM исчерпан", lead.id)
            return lead
        try:
            s = await asyncio.wait_for(
                self.assistant.summarize(lead, await self.db.get_messages(lead.id)), SUMMARY_BUDGET
            )
        except (LLMError, TimeoutError) as e:
            log.warning("Резюме лида %s не получено: %s", lead.id, str(e) or f"не уложились в {SUMMARY_BUDGET} с")
            return lead
        return await self.db.update_lead(
            lead.id, summary=s.summary, hotness=s.hotness, hotness_reason=s.reason, summary_model=s.model,
            summary_status=lead.status,
        )

    async def send_lead(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        if lead.taken_at:
            return
        lead = await self._summarize(lead)
        text = lead_text(lead, task.payload["reason"], previous=await self.db.previous_leads(lead),
                         zone=self.settings.zone)
        await self._send(text, lead_keyboard(lead), lead_ids=[lead.id], kind="lead")

    async def send_reminder(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        if lead.taken_at:
            return
        waiting = datetime.now(UTC) - next_work_start(datetime.fromisoformat(lead.notified_at), self.settings)
        text = lead_text(lead, "remind", max(1, int(waiting.total_seconds() // 60)),
                         previous=await self.db.previous_leads(lead), zone=self.settings.zone)
        await self._send(text, lead_keyboard(lead), lead_ids=[lead.id], kind="remind")

    async def send_panel(self, task: OutboxTask) -> None:
        """Панель взятой заявки — ответом на уведомление, под которым нажали «Взял»."""
        lead = await self.db.get_lead(task.lead_id)
        if lead is None or lead.status == "deleted" or not lead.taken_at:
            return
        await self._send(panel_text(lead, self.settings.zone), panel_keyboard(lead), lead_ids=[lead.id], kind="panel",
                         reply_to=task.payload.get("reply_to"))

    async def send_nudge(self, task: OutboxTask) -> None:
        """Напоминание тому, кто взял заявку: итога нет. С кнопками этапов — отметить можно прямо здесь."""
        lead = await self.db.get_lead(task.lead_id)
        if not in_work_on(lead, task.payload["stage"]):
            return  # итог отметили, пока напоминание ждало очереди
        at, zone = parse_ts(task.payload["at"]), self.settings.zone
        await self._send_mentioning(lead, lambda who: nudge_text(lead, who, at, zone), stage_keyboard(lead), "nudge")

    async def send_visit(self, task: OutboxTask) -> None:
        """Клиент ответил о замере: подтвердил — просто сообщаем; перенести или отменить — тому, кто ведёт,
        с кнопками этапов."""
        lead = await self.db.get_lead(task.lead_id)
        if lead is None or lead.status == "deleted":
            return
        answer, measure_at, zone = task.payload["answer"], task.payload["at"], self.settings.zone
        markup = stage_keyboard(lead) if answer != "yes" and in_work_on(lead, lead.stage) else None
        await self._send_mentioning(lead, lambda who: visit_text(lead, answer, measure_at, who, zone), markup, "visit")

    async def send_feedback(self, task: OutboxTask) -> None:
        """Клиент ответил на вопрос бота. Плохие новости («не связались», низкая оценка) — ещё и владельцу."""
        lead = await self.db.get_lead(task.lead_id)
        if lead is None or lead.status == "deleted":
            return
        about, answer, at = task.payload["about"], task.payload["answer"], parse_ts(task.payload["at"])
        bad = answer == "no" if about == "contact" else int(answer) <= LOW_RATING
        markup = stage_keyboard(lead) if about == "contact" and in_work_on(lead, lead.stage) else None

        def make_text(who: str | None) -> str:
            return feedback_text(lead, about, answer, who, at)

        await self._send_mentioning(lead, make_text, markup, "feedback")
        owner = self.settings.owner_chat_id
        if bad and owner and owner != await self.chat_id():
            await self._send(make_text(None), lead_ids=[lead.id], kind="feedback", to=owner)

    async def _send_mentioning(
        self, lead: Lead, make_text: Callable[[str], str], markup: InlineKeyboardMarkup | None, kind: str,
    ) -> None:
        """Сообщение с упоминанием того, кто ведёт заявку. Упоминание по id Telegram может не принять
        (настройки приватности менеджера) — тогда отправляем с именем без ссылки."""
        try:
            await self._send(make_text(mention(lead)), markup, lead_ids=[lead.id], kind=kind)
        except TelegramBadRequest as e:
            if lead.taken_by_username or not lead.taken_by_id:
                raise
            log.warning("Упоминание менеджера (заявка %s) не принято: %s — отправляю без ссылки", lead.id, e.message)
            await self._send(make_text(mention(lead, link=False)), markup, lead_ids=[lead.id], kind=kind)

    async def send_escalation(self, task: OutboxTask) -> None:
        """Владельцу: заявку никто не взял или по взятой нет итога."""
        lead = await self.db.get_lead(task.lead_id)
        reason = task.payload["reason"]
        if reason == "untaken":
            if lead is None or lead.taken_at or lead.status in ("cancelled", "deleted"):
                return
        elif not in_work_on(lead, task.payload["stage"]):
            return
        text = escalation_text(lead, reason, parse_ts(task.payload["at"]), self.settings.zone)
        await self.to_owner(text, lead_ids=[lead.id], kind="escalation")

    async def send_client_messages(self, task: OutboxTask) -> None:
        lead = await self._lead(task)
        msgs = await self.db.get_messages(lead.id, after_id=lead.client_msgs_notified, direction="in")
        msgs = [m for m in msgs if m.kind not in ANSWER_KINDS]  # они уже ушли отдельными уведомлениями
        if not msgs:
            return
        said = "ответил(а)" if talking_to_manager(lead) else "дописал(а) после анкеты"
        lines = [f"💬 <b>№{lead.id} · {escape(lead.name or 'Клиент')} {said}:</b>", ""]
        labels = {"voice": "🎤", "photo": "📷 фото", "document": "📎 файл", "video_note": "📹 видео", "contact": "📱"}
        total = 0
        for i, m in enumerate(msgs):
            if m.kind in ("text", "edit"):  # edit — правка заявки клиентом через /order
                text = m.text or ""
            elif m.kind == "voice" and not m.text:
                text = "🎤 голосовое (расшифровка — в карточке)"
            else:
                text = f"{labels.get(m.kind, m.kind)} {m.text or ''}".strip()
            text = clip(text, CLIENT_LINE_MAX)
            if total + len(text) > CLIENT_TOTAL_MAX:
                lines.append(f"… и ещё сообщений: {len(msgs) - i} — полностью в карточке")
                break
            total += len(text)
            lines.append(f"• {escape(text)}")
        if lead.taken_by_name:
            lines.append(f"\nВ работе у {escape(lead.taken_by_name)}")
        if lead.trello_card_url:
            lines.append(f'📋 <a href="{escape(lead.trello_card_url)}">Карточка в Trello</a>')
        markup = InlineKeyboardMarkup(inline_keyboard=[[reply_button(lead)]]) if lead.taken_at else lead_keyboard(lead)
        await self._send("\n".join(lines), markup, lead_ids=[lead.id], kind="client")
        await self.db.update_lead(lead.id, client_msgs_notified=msgs[-1].id)

    async def send_deleted(self, task: OutboxTask) -> None:
        """Клиент удалил заявку: убрать сообщения о ней из группы и сообщить менеджеру (без личных данных)."""
        await self._forget_group_messages(task.lead_id)
        await self._send(
            f"🗑 <b>Заявка №{task.lead_id} удалена клиентом</b>\n"
            "По его просьбе стёрты анкета, переписка и карточка в Trello."
        )

    async def _forget_group_messages(self, lead_id: int) -> None:
        """Удалить сообщения о заявке из группы. Telegram даёт удалять только 48 ч — более старые остаются.
        Сводку, где есть и другие заявки, не удаляем, а пересобираем без этой."""
        cutoff = datetime.now(UTC) - TG_DELETE_WINDOW
        for m in await self.db.tg_messages(lead_id):
            if datetime.fromisoformat(m.created_at) < cutoff:
                continue
            others = []
            if m.kind in LISTS:
                for i in await self.db.tg_message_leads(m.chat_id, m.message_id):
                    if i != lead_id and (lead := await self.db.get_lead(i)) and lead.status != "deleted":
                        others.append(lead)
            try:
                if others:
                    buttons = LISTS[m.kind][1]
                    await self.bot.edit_message_text(
                        digest_text(others, m.kind, self.settings, datetime.now(UTC)), chat_id=m.chat_id,
                        message_id=m.message_id, parse_mode="HTML",
                        reply_markup=digest_keyboard([x for x in others if not x.taken_at]) if buttons else None,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                else:
                    await self.bot.delete_message(m.chat_id, m.message_id)
            except (TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat) as e:
                # Уже удалено вручную, слишком старое, бота убрали из группы или группа стала супергруппой
                # (номера сообщений там могут быть другими — удалять «наугад» нельзя) — не повод для ретраев.
                log.warning("Сообщение %s о заявке %s не убрано из группы: %s", m.message_id, lead_id, e.message)
        await self.db.delete_tg_messages(lead_id)

    async def send_digest(self, task: OutboxTask) -> None:
        """Список заявок одним сообщением: утренняя сводка, напоминание или эскалация владельцу."""
        kind = task.payload.get("kind", "digest")
        leads = [
            lead for i in task.payload["lead_ids"]
            if (lead := await self.db.get_lead(i)) and not lead.taken_at and lead.status not in ("cancelled", "deleted")
        ]
        if not leads:
            return  # пока ждали очереди, заявки взяли
        text = digest_text(leads, kind, self.settings, datetime.now(UTC))
        ids = [x.id for x in leads]
        if kind == "untaken_list":
            await self.to_owner(text, lead_ids=ids, kind=kind)
        else:
            await self._send(text, digest_keyboard(leads), lead_ids=ids, kind=kind)

    # --- планировщик ---

    async def scan(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        s = self.settings

        for lead in await self.db.leads_to_abandon(now - timedelta(minutes=s.abandon_after_min)):
            await self.db.update_lead(lead.id, status="abandoned")

        for lead in await self.db.leads_to_notify():
            await self.db.update_lead(
                lead.id,
                notified_status=lead.status,
                notified_at=lead.notified_at or now_iso(now),
                events=[(TG_LEAD, {"reason": lead.status})],
            )

        waiting = await self.db.leads_waiting()
        work_now = work_time(now, s)
        if work_now:
            await self._remind([lead for lead in waiting if self._remind_due(lead, now)], now)
            for lead in await self.db.leads_in_work():
                await self._follow_up(lead, now)
            await self._escalate_untaken([lead for lead in waiting if self._untaken_due(lead, now)], now)

        today = now.astimezone(s.zone).date().isoformat()
        if work_now and await self.db.kv_get(KV_LAST_DIGEST) != today:
            await self.db.kv_set(KV_LAST_DIGEST, today)
            since = last_work_end(now, s)  # после выходных — с конца пятницы, а не «за сутки»
            night = [lead.id for lead in waiting if lead.is_night and parse_ts(lead.notified_at) >= since]
            if night:
                await self.db.enqueue(TG_DIGEST, None, {"lead_ids": night})

        for extra in self.extra_scans:
            await extra(now)

    async def _follow_up(self, lead: Lead, now: datetime) -> None:
        """Взятая заявка без итога: пора ли напомнить менеджеру и сообщить владельцу."""
        payload = {"stage": lead.stage, "at": now_iso(now)}
        if (due := nudge_due(lead, self.settings)) and now >= due:
            await self.db.update_lead(lead.id, nudges_sent=lead.nudges_sent + 1, last_nudge_at=now_iso(now),
                                      events=[(TG_NUDGE, payload)])
        if (due := escalate_due(lead, self.settings)) and now >= due:
            await self.db.update_lead(lead.id, escalated_at=now_iso(now),
                                      events=[(TG_ESCALATE, {**payload, "reason": "stuck"})])

    def _remind_due(self, lead: Lead, now: datetime) -> bool:
        s = self.settings
        if lead.reminders_sent >= s.remind_max:
            return False
        base = parse_ts(lead.last_reminder_at or lead.notified_at)
        return now >= next_work_start(base, s) + timedelta(minutes=s.remind_after_min)

    def _untaken_due(self, lead: Lead, now: datetime) -> bool:
        """Все напоминания о новой заявке ушли, а её так никто и не взял."""
        s = self.settings
        if lead.escalated_at or lead.reminders_sent < s.remind_max:
            return False
        last = parse_ts(lead.last_reminder_at or lead.notified_at)
        return now >= next_work_start(last, s) + timedelta(minutes=s.remind_after_min)

    async def _remind(self, due: list[Lead], now: datetime) -> None:
        """Одна заявка — личное напоминание; несколько сразу (утро после ночи) — одним списком.
        Счётчики и список — не одной транзакцией: при сбое между ними заявка получит напоминание в следующий раз."""
        if len(due) == 1:
            lead = due[0]
            await self.db.update_lead(lead.id, reminders_sent=lead.reminders_sent + 1, last_reminder_at=now_iso(now),
                                      events=[(TG_REMIND, {})])
            return
        for lead in due:
            await self.db.update_lead(lead.id, reminders_sent=lead.reminders_sent + 1, last_reminder_at=now_iso(now))
        if due:
            await self.db.enqueue(TG_DIGEST, None, {"lead_ids": [lead.id for lead in due], "kind": "remind_list"})

    async def _escalate_untaken(self, due: list[Lead], now: datetime) -> None:
        """Заявки, которые так никто и не взял, — владельцу: одна — отдельным сообщением, несколько — списком."""
        if len(due) == 1:
            payload = {"stage": None, "at": now_iso(now), "reason": "untaken"}
            await self.db.update_lead(due[0].id, escalated_at=now_iso(now), events=[(TG_ESCALATE, payload)])
            return
        for lead in due:
            await self.db.update_lead(lead.id, escalated_at=now_iso(now))
        if due:
            await self.db.enqueue(TG_DIGEST, None, {"lead_ids": [lead.id for lead in due], "kind": "untaken_list"})

    async def run(self) -> None:
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("notifier: сбой планировщика")
            self.last_scan = datetime.now(UTC)
            await asyncio.sleep(self.scan_interval)
