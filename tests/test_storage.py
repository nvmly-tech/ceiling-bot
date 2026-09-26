from aiogram.fsm.storage.base import StorageKey

from app.bot.states import Lead
from app.bot.storage import SQLiteStorage
from app.db import Database


async def test_state_and_data_survive_reconnect(tmp_path):
    path = tmp_path / "bot.sqlite3"
    key = StorageKey(bot_id=1, chat_id=42, user_id=42)

    db = Database(path)
    await db.connect()
    storage = SQLiteStorage(db)
    await storage.set_state(key, Lead.area)
    await storage.set_data(key, {"lead_id": 7, "note": "кириллица"})
    await db.close()

    # «Рестарт» процесса: новое соединение к тому же файлу.
    db = Database(path)
    await db.connect()
    storage = SQLiteStorage(db)
    assert await storage.get_state(key) == Lead.area.state
    assert await storage.get_data(key) == {"lead_id": 7, "note": "кириллица"}
    assert await storage.get_state(StorageKey(bot_id=1, chat_id=1, user_id=1)) is None
    await db.close()
