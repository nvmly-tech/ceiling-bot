"""Очередь outbox: у каждого канала свой воркер, заявки внутри канала — параллельно, порядок внутри заявки —
строгий, сторож видит канал, который встал."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
from app.services.outbox import Outbox
from tests.conftest import eventually
from tests.test_trello import FakeTrello, make_sync


async def leads(db: Database, n: int) -> list[int]:
    return [(await db.create_lead(tg_user_id=i, chat_id=i, name="К", username=None, is_night=False)).id
            for i in range(n)]


@pytest.fixture
async def running():
    """Запустить outbox.run() в фоне и остановить после теста."""
    tasks = []

    def start(outbox: Outbox) -> None:
        tasks.append(asyncio.create_task(outbox.run()))

    yield start
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_slow_trello_does_not_delay_telegram(db: Database, running):
    """Раньше воркер был один: уведомление о новой заявке ждало, пока пройдут все задачи Trello."""
    trello_gate = asyncio.Event()
    sent = []

    async def trello(task):
        await trello_gate.wait()  # Trello «висит»

    async def tg(task):
        sent.append(task.lead_id)

    a, b = await leads(db, 2)
    outbox = Outbox(db, {"trello.card_create": trello, "trello.comment": trello, "tg.lead": tg}, poll_interval=0.05)
    running(outbox)
    await db.enqueue("tg.lead", b, {})
    await eventually(lambda: sent == [b])
    assert not trello_gate.is_set()  # уведомление ушло, пока Trello ещё не ответил
    trello_gate.set()


async def test_leads_in_parallel_order_within_lead(db: Database):
    log: list[str] = []
    a_gate = asyncio.Event()

    async def tg(task):
        name = task.payload["name"]
        log.append(f"start {name}")
        if name == "a1":
            await a_gate.wait()
        log.append(f"end {name}")

    a, b = await leads(db, 2)
    for lead, name in ((a, "a1"), (a, "a2"), (b, "b1")):
        await db.enqueue("tg.lead", lead, {"name": name})
    outbox = Outbox(db, {"tg.lead": tg})
    run = asyncio.create_task(outbox.run_once())
    await eventually(lambda: "end b1" in log)  # другая заявка не ждёт медленную
    assert "start a2" not in log  # а своя — ждёт: порядок внутри заявки строгий
    a_gate.set()
    assert await run == 3
    assert log.index("end a1") < log.index("start a2")


async def test_parallelism_is_limited_per_channel(db: Database):
    active = peak = 0

    async def slow(task):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    for lead in await leads(db, 10):
        await db.enqueue("trello.comment", lead, {})
        await db.enqueue("tg.lead", lead, {})
    outbox = Outbox(db, {"trello.comment": slow, "trello.card_create": slow, "tg.lead": slow},
                    parallel={"tg": 3, "trello": 1})
    assert await outbox.run_once() == 30
    assert peak <= 3 + 1


async def test_failed_task_blocks_only_its_lead(db: Database):
    calls = []

    async def tg(task):
        calls.append(task.payload["name"])
        if task.payload["name"] == "a1":
            raise RuntimeError("Telegram не ответил")

    a, b = await leads(db, 2)
    for lead, name in ((a, "a1"), (a, "a2"), (b, "b1")):
        await db.enqueue("tg.lead", lead, {"name": name})
    outbox = Outbox(db, {"tg.lead": tg})
    now = datetime.now(UTC)
    assert await outbox.run_once(now) == 1
    assert sorted(calls) == ["a1", "b1"]  # a2 ждёт повтора a1
    calls.clear()
    assert await outbox.run_once(now + timedelta(seconds=6)) == 0  # повтор a1 — снова неудача, a2 всё ещё ждёт
    assert calls == ["a1"]


async def test_heartbeat_after_each_task(db: Database):
    """Сторож смотрит на last_run: длинный разбор завала (сотни задач Trello после простоя) — не повод
    считать очередь вставшей, пока задачи выполняются."""
    beats = []
    outbox: Outbox | None = None

    async def trello(task):
        beats.append(outbox.last_run)
        await asyncio.sleep(0.002)

    (lead,) = await leads(db, 1)
    for _ in range(3):
        await db.enqueue("trello.comment", lead, {})
    outbox = Outbox(db, {"trello.card_create": trello, "trello.comment": trello})
    await outbox.run_once()
    assert beats[0] is None and beats[1] is not None and beats[2] > beats[1]


async def test_last_run_is_the_most_lagging_channel(db: Database):
    async def noop(task):
        pass

    outbox = Outbox(db, {"tg.lead": noop, "trello.comment": noop})
    assert outbox.last_run is None  # ни один канал ещё не отчитался
    await outbox.run_once()
    t = datetime.now(UTC) - timedelta(minutes=10)
    outbox.beats["trello"] = t
    assert outbox.last_run == t and outbox.lagging() == ("trello", t)


async def test_no_handlers_still_reports_alive(db: Database):
    outbox = Outbox(db, {})
    await outbox.run_once()
    assert outbox.last_run is not None  # сторож не должен убивать бота без настроенных каналов


class NetworkTrello(FakeTrello):
    """Как настоящий клиент: на каждом запросе отдаёт управление — параллельные задачи пересекаются."""

    async def lists(self, board_id):
        await asyncio.sleep(0)
        return await super().lists(board_id)

    async def create_list(self, board_id, name):
        await asyncio.sleep(0)
        return await super().create_list(board_id, name)


async def test_trello_board_prepared_once_under_parallel_tasks(db: Database):
    fake = NetworkTrello(lists=())
    sync = make_sync(db, fake)
    await asyncio.gather(*(sync.board() for _ in range(5)))
    assert fake.calls.count("create_list") == 5  # по разу на каждый из пяти списков, а не по пять
