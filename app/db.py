"""SQLite: лиды, переписка, очередь исходящих вызовов (outbox) и состояние FSM.

Одно соединение на процесс, режим WAL. Время хранится в UTC (ISO 8601).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id    INTEGER NOT NULL,
    chat_id       INTEGER NOT NULL,
    name          TEXT,
    username      TEXT,
    object        TEXT,
    area_m2       REAL,
    area_text     TEXT,
    ceiling_type  TEXT,
    phone         TEXT,
    measure_time  TEXT,
    status        TEXT NOT NULL DEFAULT 'new',   -- new | qualified | abandoned
    is_night      INTEGER NOT NULL DEFAULT 0,
    trello_card_id TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS leads_user ON leads(tg_user_id, id);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    direction  TEXT NOT NULL,          -- in | out
    kind       TEXT NOT NULL,          -- text | voice | contact | button
    text       TEXT,
    file_id    TEXT,
    model      TEXT,                   -- кто сформировал ответ бота: script | <llm>
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id, id);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    lead_id         INTEGER,            -- задачи одного лида выполняются строго по порядку
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    done_at         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(done_at, id);

CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,
    state TEXT,
    data  TEXT NOT NULL DEFAULT '{}'
);
"""

LEAD_FIELDS = {
    "name", "username", "object", "area_m2", "area_text", "ceiling_type",
    "phone", "measure_time", "status", "is_night", "trello_card_id", "completed_at",
}


# Задачи outbox для синхронизации с Trello.
CARD_CREATE = "trello.card_create"
CARD_UPDATE = "trello.card_update"
CARD_COMMENT = "trello.comment"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Lead:
    id: int
    tg_user_id: int
    chat_id: int
    name: str | None
    username: str | None
    object: str | None
    area_m2: float | None
    area_text: str | None
    ceiling_type: str | None
    phone: str | None
    measure_time: str | None
    status: str
    is_night: bool
    trello_card_id: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass
class Message:
    id: int
    lead_id: int
    direction: str
    kind: str
    text: str | None
    file_id: str | None
    model: str | None
    created_at: str


@dataclass
class OutboxTask:
    id: int
    kind: str
    lead_id: int | None
    payload: dict[str, Any]
    attempts: int
    next_attempt_at: str
    last_error: str | None


class Database:
    def __init__(self, path: Path | str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        # Будит воркер outbox, когда появилась новая задача.
        self.on_enqueue: Callable[[], None] | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def ping(self) -> None:
        """Проверка для сторожа: база отвечает и пишется."""
        await self.conn.execute("CREATE TABLE IF NOT EXISTS _ping (at TEXT)")
        await self.conn.execute("DELETE FROM _ping")
        await self.conn.execute("INSERT INTO _ping VALUES (?)", (now_iso(),))
        await self.conn.commit()

    # --- лиды ---

    async def create_lead(
        self, *, tg_user_id: int, chat_id: int, name: str | None, username: str | None, is_night: bool
    ) -> Lead:
        ts = now_iso()
        cur = await self.conn.execute(
            "INSERT INTO leads (tg_user_id, chat_id, name, username, is_night, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_user_id, chat_id, name, username, int(is_night), ts, ts),
        )
        await self._enqueue(CARD_CREATE, cur.lastrowid)
        await self._commit()
        lead = await self.get_lead(cur.lastrowid)
        assert lead is not None
        return lead

    async def get_lead(self, lead_id: int) -> Lead | None:
        async with self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)) as cur:
            row = await cur.fetchone()
        return _lead(row) if row else None

    async def last_lead(self, tg_user_id: int) -> Lead | None:
        async with self.conn.execute(
            "SELECT * FROM leads WHERE tg_user_id = ? ORDER BY id DESC LIMIT 1", (tg_user_id,)
        ) as cur:
            row = await cur.fetchone()
        return _lead(row) if row else None

    async def update_lead(self, lead_id: int, **fields: Any) -> Lead:
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"Unknown lead fields: {unknown}")
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            await self.conn.execute(
                f"UPDATE leads SET {cols}, updated_at = ? WHERE id = ?",
                (*fields.values(), now_iso(), lead_id),
            )
            if fields.keys() - {"trello_card_id"}:
                await self._enqueue(CARD_UPDATE, lead_id, coalesce=True)
            await self._commit()
        lead = await self.get_lead(lead_id)
        assert lead is not None
        return lead

    # --- переписка ---

    async def add_message(
        self,
        lead_id: int,
        *,
        direction: str,
        kind: str,
        text: str | None = None,
        file_id: str | None = None,
        model: str | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO messages (lead_id, direction, kind, text, file_id, model, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lead_id, direction, kind, text, file_id, model, now_iso()),
        )
        await self._enqueue(CARD_COMMENT, lead_id, {"message_id": cur.lastrowid})
        await self._commit()
        return cur.lastrowid

    async def get_message(self, message_id: int) -> Message | None:
        async with self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)) as cur:
            row = await cur.fetchone()
        return Message(**dict(row)) if row else None

    async def get_messages(self, lead_id: int) -> list[Message]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [Message(**dict(r)) for r in rows]

    # --- outbox ---

    async def _enqueue(
        self, kind: str, lead_id: int | None, payload: dict[str, Any] | None = None, *, coalesce: bool = False
    ) -> None:
        """Поставить задачу в очередь. Без commit — вызывающий коммитит вместе со своей записью."""
        if coalesce:
            async with self.conn.execute(
                "SELECT 1 FROM outbox WHERE kind = ? AND lead_id IS ? AND done_at IS NULL AND attempts = 0",
                (kind, lead_id),
            ) as cur:
                if await cur.fetchone():
                    return  # такая же задача ещё ждёт и возьмёт актуальные данные на момент выполнения
        ts = now_iso()
        await self.conn.execute(
            "INSERT INTO outbox (kind, lead_id, payload, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, lead_id, json.dumps(payload or {}), ts, ts),
        )

    async def _commit(self) -> None:
        await self.conn.commit()
        if self.on_enqueue:
            self.on_enqueue()

    async def outbox_pending(self, limit: int = 200) -> list[OutboxTask]:
        """Невыполненные задачи в порядке постановки (включая ещё не наступившие ретраи)."""
        async with self.conn.execute(
            "SELECT id, kind, lead_id, payload, attempts, next_attempt_at, last_error"
            " FROM outbox WHERE done_at IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [OutboxTask(**{**dict(r), "payload": json.loads(r["payload"])}) for r in rows]

    async def outbox_done(self, task_id: int) -> None:
        await self.conn.execute("UPDATE outbox SET done_at = ? WHERE id = ?", (now_iso(), task_id))
        await self.conn.commit()

    async def outbox_retry(self, task_id: int, error: str, next_attempt_at: str) -> None:
        await self.conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? WHERE id = ?",
            (error[:1000], next_attempt_at, task_id),
        )
        await self.conn.commit()

    # --- FSM ---

    async def fsm_get(self, key: str) -> tuple[str | None, dict[str, Any]]:
        async with self.conn.execute("SELECT state, data FROM fsm WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None, {}
        return row["state"], json.loads(row["data"])

    async def fsm_set_state(self, key: str, state: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO fsm (key, state) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET state = excluded.state",
            (key, state),
        )
        await self.conn.commit()

    async def fsm_set_data(self, key: str, data: dict[str, Any]) -> None:
        await self.conn.execute(
            "INSERT INTO fsm (key, data) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET data = excluded.data",
            (key, json.dumps(data, ensure_ascii=False)),
        )
        await self.conn.commit()


def _lead(row: aiosqlite.Row) -> Lead:
    d = dict(row)
    d["is_night"] = bool(d["is_night"])
    return Lead(**d)
