"""FSM-хранилище aiogram поверх SQLite: после рестарта клиент продолжает с того же вопроса."""

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from app.db import Database


class SQLiteStorage(BaseStorage):
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _key(key: StorageKey) -> str:
        parts = [
            key.bot_id, key.chat_id, key.user_id,
            key.thread_id or "", key.business_connection_id or "", key.destiny,
        ]
        return ":".join(map(str, parts))

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self.db.fsm_set_state(self._key(key), value)

    async def get_state(self, key: StorageKey) -> str | None:
        state, _ = await self.db.fsm_get(self._key(key))
        return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self.db.fsm_set_data(self._key(key), dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        _, data = await self.db.fsm_get(self._key(key))
        return data

    async def close(self) -> None:
        # Соединением владеет Database, закрывается в main.
        pass
