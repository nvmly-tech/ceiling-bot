"""Итоги заявки: кнопки этапов под панелью взятой заявки в группе менеджеров.

callback «st:<заявка>:<действие>[:<значение>]». Замер — выбор дня, потом часа; отказ — выбор причины.
Отметить этап может любой менеджер группы; кнопка со старой панели (этап уже сменился) ничего не меняет,
а панель обновляется до текущего этапа.
"""

from datetime import UTC, date, datetime, timedelta

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from app.config import Settings
from app.db import Database, Lead, now_iso
from app.services.notifier import Notifier, panel_keyboard, panel_text
from app.stages import (
    CONTRACT,
    MEASURE,
    NEXT,
    NO_ANSWER,
    REFUSE_REASONS,
    REFUSED,
    REOPEN,
    THINKING,
    WEEKDAYS,
    measure_hours,
    stage_text,
)

DAYS_AHEAD = 7  # на сколько дней вперёд предлагать дату замера
# Действие кнопки → этап, к которому оно ведёт (для проверки, что кнопка не со старой панели).
TARGETS = {
    "measure": MEASURE, "day": MEASURE, "at": MEASURE,
    "refuse": REFUSED, "rsn": REFUSED,
    "no_answer": NO_ANSWER, "thinking": THINKING, "contract": CONTRACT, "reopen": REOPEN,
}


def _button(text: str, lead_id: int, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"st:{lead_id}:{action}")


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def days_keyboard(lead_id: int, today: date) -> InlineKeyboardMarkup:
    days = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
    labels = ["Сегодня", "Завтра"] + [f"{WEEKDAYS[d.weekday()]} {d:%d.%m}" for d in days[2:]]
    buttons = [_button(label, lead_id, f"day:{d:%Y%m%d}") for label, d in zip(labels, days, strict=True)]
    return InlineKeyboardMarkup(inline_keyboard=[*_rows(buttons, 4), [_button("← Назад", lead_id, "back")]])


def hours_keyboard(lead_id: int, day: date, settings: Settings) -> InlineKeyboardMarkup:
    buttons = [_button(f"{h}:00", lead_id, f"at:{day:%Y%m%d}{h:02d}")
               for h in measure_hours(settings.work_start, settings.work_end)]
    return InlineKeyboardMarkup(inline_keyboard=[*_rows(buttons, 4), [_button("← Другой день", lead_id, "measure")]])


def reasons_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    buttons = [_button(label.capitalize(), lead_id, f"rsn:{code}") for code, label in REFUSE_REASONS.items()]
    return InlineKeyboardMarkup(inline_keyboard=[*_rows(buttons, 2), [_button("← Назад", lead_id, "back")]])


def parse(data: str) -> tuple[int, str, str] | None:
    parts = data.split(":")
    if len(parts) not in (3, 4) or not parts[1].isdigit() or parts[2] not in {*TARGETS, "back"}:
        return None
    return int(parts[1]), parts[2], parts[3] if len(parts) == 4 else ""


def parse_day(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def parse_moment(value: str, settings: Settings) -> datetime | None:
    """«2026100214» → 02.10.2026 14:00 по часовому поясу студии."""
    try:
        return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=settings.zone)
    except ValueError:
        return None


PICKER_HINTS = {"measure": "Выберите день замера", "day": "Выберите время", "refuse": "Выберите причину"}


def picker(action: str, value: str, lead: Lead, settings: Settings) -> InlineKeyboardMarkup | None:
    """Кнопки выбора (день, час, причина) или снова кнопки этапов («назад»); None — значение испорчено."""
    if action == "measure":
        return days_keyboard(lead.id, datetime.now(settings.zone).date())
    if action == "day":
        day = parse_day(value)
        return hours_keyboard(lead.id, day, settings) if day else None
    if action == "refuse":
        return reasons_keyboard(lead.id)
    return panel_keyboard(lead)


def stage_values(action: str, value: str, settings: Settings) -> dict | None:
    """Этап и его данные из нажатой кнопки; None — значение подделано или испорчено."""
    if action == "at":
        moment = parse_moment(value, settings)
        # В базе время — в UTC (строки сравниваются при выборке «замер уже прошёл»).
        return {"stage": MEASURE, "measure_at": now_iso(moment.astimezone(UTC))} if moment else None
    if action == "rsn":
        return {"stage": REFUSED, "reason": value} if value in REFUSE_REASONS else None
    return {"stage": None if action == "reopen" else TARGETS[action]}


async def _show_panel(message: Message, lead: Lead, settings: Settings) -> None:
    await message.edit_text(
        panel_text(lead, settings.zone), parse_mode="HTML", reply_markup=panel_keyboard(lead),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _usable(cb: CallbackQuery, lead: Lead, action: str, settings: Settings) -> bool:
    """Можно ли нажимать кнопки этой заявки сейчас; если нет — объясняем менеджеру."""
    if lead.status == "deleted":
        await cb.answer(f"Заявка №{lead.id} удалена клиентом", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return False
    if not lead.taken_at:
        await cb.answer("Сначала нажмите «✅ Взял в работу»", show_alert=True)
        return False
    if action != "back" and TARGETS[action] not in NEXT[lead.stage]:
        # Кнопка со старой панели: этап уже сменили — показываем актуальный.
        now = stage_text(lead.stage, lead.measure_at, lead.refuse_reason, settings.zone)
        await cb.answer(f"Уже отмечено: {now}", show_alert=True)
        await _show_panel(cb.message, lead, settings)
        return False
    return True


async def on_stage_button(cb: CallbackQuery, db: Database, settings: Settings, notifier: Notifier) -> None:
    if not isinstance(cb.message, Message) or cb.message.chat.id != await notifier.chat_id():
        await cb.answer()
        return
    parsed = parse(cb.data or "")
    if parsed is None:
        await cb.answer()  # подделанные данные кнопки
        return
    lead_id, action, value = parsed
    lead = await db.get_lead(lead_id)
    if lead is None:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if not await _usable(cb, lead, action, settings):
        return
    if action in {*PICKER_HINTS, "back"}:
        markup = picker(action, value, lead, settings)
        await cb.answer(PICKER_HINTS.get(action))
        if markup is not None:
            await cb.message.edit_reply_markup(reply_markup=markup)
        return
    values = stage_values(action, value, settings)
    if values is None:
        await cb.answer()
        return
    lead = await db.set_stage(lead_id, values.pop("stage"), by_name=cb.from_user.full_name, **values)
    await cb.answer(f"Отмечено: {stage_text(lead.stage, lead.measure_at, lead.refuse_reason, settings.zone)}")
    await _show_panel(cb.message, lead, settings)
