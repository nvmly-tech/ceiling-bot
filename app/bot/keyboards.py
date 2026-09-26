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


def remove() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
