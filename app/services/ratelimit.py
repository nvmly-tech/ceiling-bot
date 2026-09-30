"""Темп отправки в группы: Telegram пускает бота в одну группу не чаще 20 сообщений в минуту.

Middleware сессии бота: каждое sendMessage в группу (id < 0) ждёт свободного места в минутном окне этого чата,
ожидающие проходят по очереди. Если Telegram всё-таки ответил 429 (Too Many Requests), чат закрывается ровно
на retry_after — для всех отправителей: очереди, сторожа, ответов на команды. Личные чаты с клиентами не
ограничиваются: бот отвечает в них по сообщению клиента.
"""

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage, TelegramMethod

GROUP_PER_MINUTE = 18  # с запасом от лимита Telegram (20)
WINDOW = 60.0  # с


class ChatRateLimiter:
    def __init__(
        self, per_minute: int = GROUP_PER_MINUTE, window: float = WINDOW,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.per_minute, self.window = per_minute, window
        self.clock, self.sleep = clock, sleep
        self._sent: dict[int, deque[float]] = defaultdict(deque)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._blocked_until: dict[int, float] = {}

    def _wait_needed(self, chat_id: int) -> float:
        now = self.clock()
        sent = self._sent[chat_id]
        while sent and now - sent[0] >= self.window:
            sent.popleft()
        blocked = self._blocked_until.get(chat_id, 0) - now
        if blocked > 0:
            return blocked
        if len(sent) >= self.per_minute:
            return self.window - (now - sent[0])
        return 0

    async def acquire(self, chat_id: int) -> None:
        """Дождаться права отправить сообщение в чат. Замок на чат — ожидающие проходят в порядке очереди."""
        async with self._locks[chat_id]:
            while (wait := self._wait_needed(chat_id)) > 0:
                await self.sleep(wait)
            self._sent[chat_id].append(self.clock())

    def block(self, chat_id: int, seconds: float) -> None:
        """Telegram попросил подождать: до этого момента в чат не шлём."""
        self._blocked_until[chat_id] = self.clock() + seconds

    async def middleware(
        self, make_request: Callable[..., Awaitable[Any]], bot: Bot, method: TelegramMethod
    ) -> Any:
        chat_id = getattr(method, "chat_id", None)
        if isinstance(method, SendMessage) and isinstance(chat_id, int) and chat_id < 0:
            await self.acquire(chat_id)
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as e:
            if isinstance(chat_id, int):
                self.block(chat_id, e.retry_after)
            raise
