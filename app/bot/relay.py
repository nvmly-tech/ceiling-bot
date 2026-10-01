"""Ответ клиенту через бота: «💬 Ответить через бота» → подсказка в группе → ответ на неё уходит клиенту.

Нужно, потому что «Написать клиенту» работает только с @username, а клиент без него (например, выбравший
«напишите мне в Telegram») для менеджера иначе недостижим. Клиенту уходят только ответы на подсказку:
переписка менеджеров между собой в группе к нему не попадёт. Ответ менеджера — в переписке и карточке Trello.
Пока идёт разговор с человеком (inwork.HUMAN_TALK после ответа менеджера), бот сам клиенту не отвечает —
сообщения клиента уходят в группу (handlers.on_human_talk).
"""

import logging
from html import escape

from aiogram import Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, ForceReply, Message, ReactionTypeEmoji

from app.bot.dialog import incoming
from app.db import Database, Lead, now_iso
from app.services.notifier import Notifier

log = logging.getLogger(__name__)

PROMPT_KIND = "reply_prompt"  # вид сообщения в tg_messages: подсказка «ответьте клиенту»
MEDIA_LABELS = {"voice": "🎤 голосовое", "photo": "📷 фото", "document": "📎 файл", "video_note": "📹 видео"}


def prompt_text(lead: Lead, who: str) -> str:
    return (f"✍️ Ответ клиенту по заявке №{lead.id} · {escape(lead.name or 'Клиент')} — {who}, напишите его "
            "ответом на это сообщение (текст, фото, голосовое). Клиенту уходят только ответы на это сообщение.")


def who_html(user) -> str:
    """Упоминание менеджера, чтобы поле ответа открылось именно у него (ForceReply selective)."""
    if user.username:
        return f"@{escape(user.username)}"
    return f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'


async def on_reply_button(cb: CallbackQuery, db: Database, notifier: Notifier) -> None:
    if not isinstance(cb.message, Message) or cb.message.chat.id != await notifier.chat_id():
        await cb.answer()
        return
    raw = (cb.data or "").split(":", 1)[1]
    lead = await db.get_lead(int(raw)) if raw.isdigit() else None
    if lead is None or lead.status == "deleted" or not lead.chat_id:
        await cb.answer("Заявка не найдена или удалена клиентом" if raw.isdigit() else None, show_alert=raw.isdigit())
        return
    await cb.answer()
    markup = ForceReply(selective=True, input_field_placeholder="Ответ клиенту…")
    try:
        sent = await cb.message.answer(prompt_text(lead, who_html(cb.from_user)), parse_mode="HTML",
                                       reply_markup=markup)
    except TelegramBadRequest:
        # Упоминание по id Telegram может не принять (приватность) — тогда по имени.
        sent = await cb.message.answer(prompt_text(lead, escape(cb.from_user.full_name)), parse_mode="HTML",
                                       reply_markup=markup)
    await db.add_tg_message([lead.id], sent.chat.id, sent.message_id, PROMPT_KIND)


async def _deliver(bot: Bot, lead: Lead, message: Message, header: str, kind: str, text: str | None) -> None:
    if kind == "text":
        await bot.send_message(lead.chat_id, f"{header}\n{text}")
        return
    await bot.send_message(lead.chat_id, header)
    await bot.copy_message(lead.chat_id, message.chat.id, message.message_id)


async def on_manager_reply(message: Message, bot: Bot, db: Database, notifier: Notifier) -> None:
    """Ответ менеджера на подсказку — клиенту. Остальные сообщения группы — дальше (on_group_message)."""
    if message.chat.id != await notifier.chat_id() or message.reply_to_message is None:
        raise SkipHandler
    prompt = await db.tg_message(message.chat.id, message.reply_to_message.message_id)
    if prompt is None or prompt.kind != PROMPT_KIND:
        raise SkipHandler
    lead = await db.get_lead(prompt.lead_id)
    if lead is None or lead.status == "deleted" or not lead.chat_id:
        await message.reply(f"⚠️ Заявка №{prompt.lead_id} удалена клиентом — сообщение не отправлено")
        return
    item = incoming(message)
    manager = message.from_user
    try:
        await _deliver(bot, lead, message, f"{manager.first_name}, менеджер студии:", item.kind, item.text)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("Ответ менеджера по заявке %s не доставлен: %s", lead.id, e.message)
        await message.reply(f"⚠️ Не доставлено: {'клиент заблокировал бота' if 'blocked' in e.message else e.message}")
        return
    await _remember(db, lead, message, item, manager.full_name)
    try:
        await bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji="👍")])
    except TelegramBadRequest as e:  # реакции в группе выключены — не беда, ответ уже у клиента
        log.info("Реакция на ответ менеджера не поставлена: %s", e.message)


async def _remember(db: Database, lead: Lead, message: Message, item, manager_name: str) -> None:
    """В переписку (и карточку — вложением, если это файл), и отметка «идёт разговор с человеком»."""
    text = item.text if item.kind == "text" else f"{MEDIA_LABELS.get(item.kind, item.kind)} {item.text or ''}".strip()
    await db.add_message(lead.id, direction="out", kind="manager", text=text, file_id=item.file_id,
                         model=manager_name)
    fields: dict = {"manager_reply_at": now_iso()}
    if not lead.manager_reply_at:
        # Разговор начался: в «клиент ответил» — только то, что клиент напишет дальше, а не его прежние ответы.
        incoming_msgs = await db.get_messages(lead.id, direction="in")
        if incoming_msgs and incoming_msgs[-1].id > lead.client_msgs_notified:
            fields["client_msgs_notified"] = incoming_msgs[-1].id
    await db.update_lead(lead.id, **fields)
    await db.add_tg_message([lead.id], message.chat.id, message.message_id, "manager")  # удалится вместе с заявкой
