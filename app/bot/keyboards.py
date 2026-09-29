from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot import texts


def _inline(prefix: str, options: dict[str, str], per_row: int = 2) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{code}") for code, label in options.items()]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + per_row] for i in range(0, len(buttons), per_row)])


def objects() -> InlineKeyboardMarkup:
    return _inline("obj", texts.OBJECT_OPTIONS)


def areas() -> InlineKeyboardMarkup:
    return _inline("area", texts.AREA_OPTIONS)


def ceilings() -> InlineKeyboardMarkup:
    return _inline("ct", texts.CEILING_OPTIONS)


def phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.SHARE_PHONE, request_contact=True)],
            [KeyboardButton(text=texts.NO_PHONE)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def restart(lead_id: int) -> InlineKeyboardMarkup:
    """/start посреди анкеты: продолжить её или закрыть и начать новую. В callback — номер заявки,
    чтобы старая кнопка (заявка уже сменилась) ничего не сделала."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.RESTART_CONTINUE, callback_data=f"restart:continue:{lead_id}"),
        InlineKeyboardButton(text=texts.RESTART_NEW, callback_data=f"restart:new:{lead_id}"),
    ]])


def order(lead_id: int) -> InlineKeyboardMarkup:
    """/order: что изменить в заявке. В callback — номер заявки: старая кнопка ничего не делает."""
    fields = [InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"edit:{field}:{lead_id}")
              for field, label in texts.FIELD_LABELS.items()]
    delete = InlineKeyboardButton(text=texts.ORDER_DELETE, callback_data=f"delete:ask:{lead_id}")
    ok = InlineKeyboardButton(text=texts.ORDER_OK, callback_data=f"edit:ok:{lead_id}")
    rows = [fields[i : i + 2] for i in range(0, len(fields), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows + [[delete], [ok]])


def delete_confirm(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.DELETE_YES, callback_data=f"delete:yes:{lead_id}"),
        InlineKeyboardButton(text=texts.DELETE_NO, callback_data=f"delete:no:{lead_id}"),
    ]])


def remove() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
