"""Одно соединение с базой на процесс, а обработчики aiogram и очередь работают параллельно:
транзакции не должны пересекаться — commit или rollback одной корутины не трогает запись другой."""

import asyncio

import pytest

from app.db import Database


async def test_second_take_does_not_erase_concurrent_client_message(db: Database):
    """Воспроизведение из разбора схемы: второе «Взял» (заявка уже взята) делало rollback и стирало сообщение
    клиента, пришедшее в ту же миллисекунду, — вместе с задачей для Trello или без неё."""
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    await db.take_lead(lead.id, by_id=7, by_name="Иван")
    for _ in range(50):
        msg_id, taken = await asyncio.gather(
            db.add_message(lead.id, direction="in", kind="text", text="мой номер 8 900 123-45-67"),
            db.take_lead(lead.id, by_id=8, by_name="Олег"),
        )
        assert taken is False
        assert await db.get_message(msg_id) is not None
    async with db.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'trello.comment'") as cur:
        assert (await cur.fetchone())[0] == 50


async def test_failed_transaction_does_not_leak_or_swallow_others(db: Database):
    """Транзакция, упавшая на середине, откатывается целиком, а чужая запись, начатая в это время, — сохраняется."""

    async def failing():
        async with db._tx():
            await db.conn.execute("INSERT INTO kv (key, value) VALUES ('half', '1')")
            await asyncio.sleep(0.01)  # другая корутина успевает начать свою запись
            raise RuntimeError("сбой посреди транзакции")

    results = await asyncio.gather(failing(), db.kv_set("other", "2"), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert await db.kv_get("half") is None  # не зафиксирована чужим commit
    assert await db.kv_get("other") == "2"  # и не стёрта чужим rollback


async def test_enqueue_wakes_outbox_only_after_commit(db: Database):
    woken = []
    db.on_enqueue = lambda: woken.append(True)
    await db.kv_set("x", "1")
    assert woken == []  # запись без задач очередь не будит
    await db.enqueue("tg.lead", None, {})
    assert woken == [True]


@pytest.mark.parametrize("method", ["kv_set", "fsm_set_state"])
async def test_simple_writes_are_committed(db: Database, method: str):
    await getattr(db, method)("k", "v")
    await db.conn.rollback()  # ничего незафиксированного не осталось
    value = await db.kv_get("k") if method == "kv_set" else (await db.fsm_get("k"))[0]
    assert value == "v"
