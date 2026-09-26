from datetime import UTC, datetime, time
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User, Voice

from app.config import Settings
from app.db import Database
from app.main import build_dispatcher

USER = User(id=42, is_bot=False, first_name="Анна", last_name="Петрова", username="anna")
CHAT = Chat(id=42, type="private")


class FakeSession(BaseSession):
    """Сессия без сети: запоминает вызовы API и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._msg_id = 1000

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._msg_id += 1
            return Message(message_id=self._msg_id, date=datetime.now(UTC), chat=CHAT, text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise NotImplementedError(type(method).__name__)

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def sent(self) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage)]


class Client:
    """Имитация клиента в чате с ботом."""

    def __init__(self, dp, bot: Bot, session: FakeSession):
        self.dp, self.bot, self.session = dp, bot, session
        self._update_id = 0
        self._msg_id = 0

    def _message(self, **kwargs: Any) -> Message:
        self._msg_id += 1
        return Message(message_id=self._msg_id, date=datetime.now(UTC), chat=CHAT, from_user=USER, **kwargs)

    async def _feed(self, **kwargs: Any) -> None:
        self._update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, **kwargs))

    async def text(self, text: str) -> None:
        await self._feed(message=self._message(text=text))

    async def voice(self, file_id: str = "voice-1") -> None:
        voice = Voice(file_id=file_id, file_unique_id=file_id, duration=3)
        await self._feed(message=self._message(voice=voice))

    async def contact(self, phone: str) -> None:
        contact = Contact(phone_number=phone, first_name=USER.first_name, user_id=USER.id)
        await self._feed(message=self._message(contact=contact))

    async def press(self, data: str) -> None:
        # Кнопка висит под последним сообщением бота.
        question = Message(message_id=999, date=datetime.now(UTC), chat=CHAT, text=self.session.sent()[-1].text)
        cb = CallbackQuery(id=str(self._update_id), from_user=USER, chat_instance="ci", message=question, data=data)
        await self._feed(callback_query=cb)

    def last_text(self) -> str:
        return self.session.sent()[-1].text


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def settings() -> Settings:
    # Рабочее время круглые сутки — если тесту не нужен ночной режим.
    return Settings(bot_token="123:TEST", work_start=time(0, 0), work_end=time(23, 59, 59))


@pytest.fixture
async def client(db: Database, settings: Settings) -> Client:
    session = FakeSession()
    bot = Bot("123:TEST", session=session)
    return Client(build_dispatcher(db, settings), bot, session)
