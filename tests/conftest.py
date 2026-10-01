import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    GetUpdates,
    SendChatAction,
    SendMessage,
    SetMessageReaction,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Contact, File, Message, MessageId, PhotoSize, Update, User, Voice

from app.config import Settings
from app.db import Database
from app.main import build_dispatcher

# Тесты не читают .env разработчика с боевыми ключами: всё, что нужно, задаётся явно в каждом тесте.
Settings.model_config["env_file"] = None

USER = User(id=42, is_bot=False, first_name="Анна", last_name="Петрова", username="anna")
CHAT = Chat(id=42, type="private")
MANAGER_CHAT = Chat(id=-5000, type="group", title="Менеджеры")
MANAGER = User(id=7, is_bot=False, first_name="Иван", last_name="Менеджеров")
MANAGER2 = User(id=8, is_bot=False, first_name="Олег")


class FakeSession(BaseSession):
    """Сессия без сети: запоминает вызовы API и возвращает правдоподобные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._msg_id = 1000
        self.migrate: dict[int, int] = {}  # chat_id → новый id: имитация превращения группы в супергруппу
        self.downloads: list[str] = []
        self.undeletable: set[int] = set()  # message_id, которые Telegram откажется удалять

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        if isinstance(method, SendMessage) and method.chat_id in self.migrate:
            new_id = self.migrate[method.chat_id]
            raise TelegramMigrateToChat(method=method, message="migrated", migrate_to_chat_id=new_id)
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._msg_id += 1
            chat_id = method.chat_id or CHAT.id
            chat = CHAT if chat_id == CHAT.id else Chat(id=chat_id, type="group")
            return Message(message_id=self._msg_id, date=datetime.now(UTC), chat=chat, text=method.text)
        if isinstance(method, EditMessageReplyMarkup | SetMessageReaction):
            return True
        if isinstance(method, CopyMessage):
            self._msg_id += 1
            return MessageId(message_id=self._msg_id)
        if isinstance(method, DeleteMessage):
            if method.chat_id in self.migrate:
                new_id = self.migrate[method.chat_id]
                raise TelegramMigrateToChat(method=method, message="migrated", migrate_to_chat_id=new_id)
            if method.message_id in self.undeletable:
                raise TelegramBadRequest(method=method, message="Bad Request: message can't be deleted")
            return True
        if isinstance(method, (AnswerCallbackQuery, SendChatAction)):
            return True
        if isinstance(method, GetUpdates):
            return []
        if isinstance(method, GetFile):
            ext = ".jpg" if method.file_id.startswith("photo") else ".oga"
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=f"files/{method.file_id}{ext}")
        raise NotImplementedError(type(method).__name__)

    async def stream_content(self, url: str, *args: Any, **kwargs: Any):
        self.downloads.append(url)
        yield b"FILE:" + url.rsplit("/", 1)[-1].encode()

    async def close(self) -> None:
        pass

    def sent(self, chat_id: int | None = None) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)]

    def deleted(self) -> list[int]:
        return [c.message_id for c in self.calls if isinstance(c, DeleteMessage)]

    def edits(self) -> list[EditMessageText]:
        return [c for c in self.calls if isinstance(c, EditMessageText)]

    def copies(self, chat_id: int | None = None) -> list[CopyMessage]:
        return [c for c in self.calls if isinstance(c, CopyMessage) and (chat_id is None or c.chat_id == chat_id)]


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

    async def photo(self, file_id: str = "photo-1", caption: str | None = None) -> None:
        size = PhotoSize(file_id=file_id, file_unique_id=file_id, width=100, height=100)
        await self._feed(message=self._message(photo=[size], caption=caption))

    async def contact(self, phone: str) -> None:
        contact = Contact(phone_number=phone, first_name=USER.first_name, user_id=USER.id)
        await self._feed(message=self._message(contact=contact))

    async def press(self, data: str) -> None:
        # Кнопка висит под последним сообщением бота.
        question = Message(message_id=999, date=datetime.now(UTC), chat=CHAT, text=self.session.sent(CHAT.id)[-1].text)
        cb = CallbackQuery(id=str(self._update_id), from_user=USER, chat_instance="ci", message=question, data=data)
        await self._feed(callback_query=cb)

    async def press_in_group(self, sent: SendMessage, data: str, user: User = MANAGER, chat: Chat = MANAGER_CHAT):
        """Менеджер нажимает кнопку под сообщением бота в группе."""
        msg = Message(message_id=500, date=datetime.now(UTC), chat=chat, text=sent.text, reply_markup=sent.reply_markup)
        self._update_id += 1
        cb = CallbackQuery(id=f"g{self._update_id}", from_user=user, chat_instance="g", message=msg, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, callback_query=cb))

    async def group_text(self, text: str, user: User = MANAGER) -> None:
        self._update_id += 1
        msg = Message(message_id=501, date=datetime.now(UTC), chat=MANAGER_CHAT, from_user=user, text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self._update_id, message=msg))

    def last_text(self) -> str:
        return self.session.sent(CHAT.id)[-1].text


async def eventually(check: Callable[[], bool], timeout: float = 2.0) -> None:
    """Дождаться условия от фоновой задачи. Фиксированная пауза (sleep 0.05) под нагрузкой — например,
    с замером покрытия в deploy.sh — иногда оказывалась короче, и тест случайно падал."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        if loop.time() > deadline:
            raise AssertionError("условие не выполнилось за отведённое время")
        await asyncio.sleep(0.005)


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
