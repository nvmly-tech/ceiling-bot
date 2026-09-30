"""Темп отправки в группы: Telegram пускает бота в одну группу не чаще 20 сообщений в минуту."""

import asyncio
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from app.db import Database
from app.services.outbox import Outbox
from app.services.ratelimit import GROUP_PER_MINUTE, ChatRateLimiter
from tests.conftest import FakeSession

GROUP = -5000


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(t: FakeTime, **kw) -> ChatRateLimiter:
    return ChatRateLimiter(clock=t.clock, sleep=t.sleep, **kw)


async def test_group_messages_are_paced():
    t = FakeTime()
    lim = limiter(t)
    for _ in range(GROUP_PER_MINUTE):
        await lim.acquire(GROUP)
    assert t.slept == []  # в пределах лимита — без ожидания
    await lim.acquire(GROUP)
    assert t.slept == [60.0]  # следующее — когда самое старое выйдет из минутного окна
    t.now += 1  # первые 18 уже вышли из окна — снова без ожидания
    await lim.acquire(GROUP)
    assert t.slept == [60.0]


async def test_chats_are_limited_separately():
    t = FakeTime()
    lim = limiter(t, per_minute=2)
    for chat in (GROUP, GROUP, -6000, -6000):
        await lim.acquire(chat)
    assert t.slept == []


async def test_retry_after_blocks_the_chat():
    t = FakeTime()
    lim = limiter(t)
    lim.block(GROUP, 45)
    await lim.acquire(GROUP)
    assert t.slept == [45]


async def test_middleware_limits_only_group_messages_and_honours_retry_after():
    t = FakeTime()
    lim = limiter(t, per_minute=1)
    session = FakeSession()
    bot = Bot("123:TEST", session=session)
    session.middleware(lim.middleware)

    await bot.send_message(42, "клиенту")  # личный чат — не ограничиваем
    await bot.send_message(42, "клиенту")
    await bot.send_message(GROUP, "в группу")
    assert t.slept == []
    await bot.send_message(GROUP, "в группу ещё")
    assert t.slept == [60.0]

    original = FakeSession.make_request

    async def flood(self, bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.text == "лишнее":
            raise TelegramRetryAfter(method=method, message="Too Many Requests: retry after 30", retry_after=30)
        return await original(self, bot, method, timeout)

    FakeSession.make_request = flood
    try:
        try:
            await bot.send_message(-7000, "лишнее")
        except TelegramRetryAfter:
            pass
        await bot.send_message(-7000, "после паузы")
        assert t.slept[-1] == 30  # следующее сообщение в этот чат — только после паузы, которую просил Telegram
    finally:
        FakeSession.make_request = original


async def test_outbox_waits_as_long_as_telegram_asks(db: Database):
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)

    async def flooded(task):
        raise TelegramRetryAfter(method=SendMessage(chat_id=GROUP, text="x"), message="Too Many Requests",
                                 retry_after=120)

    await db.enqueue("tg.lead", lead.id, {})
    now = datetime.now(UTC)
    await Outbox(db, {"tg.lead": flooded}).run_once(now)
    [task] = await db.outbox_pending(["tg.lead"])
    assert datetime.fromisoformat(task.next_attempt_at) >= now + timedelta(seconds=119)  # а не наши 5 с


async def test_waiters_keep_order():
    t = FakeTime()
    lim = limiter(t, per_minute=1)
    order = []

    async def send(n):
        await lim.acquire(GROUP)
        order.append(n)

    await asyncio.gather(*(send(n) for n in range(4)))
    assert order == [0, 1, 2, 3]
