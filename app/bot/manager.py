"""Чат менеджеров (и админа): кнопка «Взял в работу», команда /status."""

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

from app.config import Settings
from app.db import Database
from app.services.health import HealthMonitor
from app.services.notifier import Notifier


def _without_take(markup: InlineKeyboardMarkup | None, lead_id: int) -> InlineKeyboardMarkup | None:
    """Убрать кнопку «Взял» этого лида (в дайджесте остальные кнопки остаются)."""
    if markup is None:
        return None
    rows = [[b for b in row if b.callback_data != f"take:{lead_id}"] for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def on_take(cb: CallbackQuery, db: Database, settings: Settings, notifier: Notifier) -> None:
    if not isinstance(cb.message, Message) or cb.message.chat.id != await notifier.chat_id():
        await cb.answer()
        return
    try:
        lead_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer()  # подделанные данные кнопки
        return
    lead = await db.get_lead(lead_id)
    if lead is None:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if lead.status == "deleted":
        # Сообщение старше 48 ч осталось в группе (Telegram не дал удалить) — взять заявку уже нельзя.
        await cb.answer(f"Заявка №{lead_id} удалена клиентом", show_alert=True)
        # Убираем «Взял» этой заявки и ссылку на клиента; кнопки других заявок (в сводке) остаются.
        rest = _without_take(cb.message.reply_markup, lead_id)
        rows = [[b for b in row if not b.url] for row in rest.inline_keyboard] if rest else []
        rows = [row for row in rows if row]
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
        return
    by = cb.from_user.full_name
    if await db.take_lead(lead_id, by_id=cb.from_user.id, by_name=by):
        await cb.answer(f"Заявка №{lead_id} ваша 👍")
        when = datetime.now(settings.zone).strftime("%H:%M")
        mark = f"\n\n✅ №{lead_id} взял(а): <b>{escape(by)}</b> · {when}"
    else:
        await cb.answer(f"Уже взял(а) {lead.taken_by_name}", show_alert=True)
        mark = ""
    await cb.message.edit_text(
        cb.message.html_text + mark,
        parse_mode="HTML",
        reply_markup=_without_take(cb.message.reply_markup, lead_id),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def on_status(
    message: Message, settings: Settings, notifier: Notifier, monitor: HealthMonitor | None = None
) -> None:
    allowed = {settings.admin_chat_id, await notifier.chat_id()} - {None}
    if monitor is None or message.chat.id not in allowed:
        raise SkipHandler  # не наш чат — пусть обработает диалог с клиентом
    await message.answer(await monitor.status_text(), parse_mode="HTML")


async def on_migrate(message: Message, notifier: Notifier) -> None:
    """Служебное сообщение «группа стала супергруппой»: переходим на новый id сразу, не дожидаясь отправки —
    иначе «Взял» и /status из новой группы не принимались бы до следующего уведомления."""
    await notifier.switch_chat(message.chat.id, message.migrate_to_chat_id)


def create_manager_router() -> Router:
    r = Router(name="manager")
    r.message.register(on_migrate, F.migrate_to_chat_id)
    r.callback_query.register(on_take, F.data.startswith("take:"))
    r.message.register(on_status, Command("status"))
    return r
