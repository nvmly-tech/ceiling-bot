"""SQLite: лиды, переписка, очередь исходящих вызовов (outbox), состояние FSM и служебные значения.

Одно соединение на процесс, режим WAL. Время хранится в UTC (ISO 8601).
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    kind       TEXT NOT NULL,          -- text | voice | contact | button | photo | document | other
    text       TEXT,
    file_id    TEXT,
    model      TEXT,                   -- кто сформировал ответ бота: script | <llm>
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id, id);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    lead_id         INTEGER,
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

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Колонки, добавленные после первой версии схемы: дописываются в существующую базу при старте.
MIGRATIONS = [
    ("leads", "trello_card_url", "TEXT"),
    ("leads", "notified_status", "TEXT"),          # статус, о котором менеджер уже уведомлён
    ("leads", "notified_at", "TEXT"),
    ("leads", "taken_by_id", "INTEGER"),
    ("leads", "taken_by_name", "TEXT"),
    ("leads", "taken_at", "TEXT"),
    ("leads", "reminders_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "last_reminder_at", "TEXT"),
    ("leads", "client_msgs_notified", "INTEGER NOT NULL DEFAULT 0"),  # id последнего сообщения, о котором сказали
    ("outbox", "queue", "TEXT"),                   # задачи одной очереди выполняются строго по порядку
]

# Поля анкеты: их изменение обновляет карточку в Trello.
CARD_FIELDS = {
    "name", "username", "object", "area_m2", "area_text", "ceiling_type",
    "phone", "measure_time", "status", "is_night",
}
LEAD_FIELDS = CARD_FIELDS | {
    "trello_card_id", "trello_card_url", "completed_at", "notified_status", "notified_at",
    "reminders_sent", "last_reminder_at", "client_msgs_notified",
}

# Задачи outbox. Префикс до точки — канал: у каждого лида своя очередь на канал.
CARD_CREATE = "trello.card_create"
CARD_UPDATE = "trello.card_update"
CARD_COMMENT = "trello.comment"
CARD_TAKE = "trello.card_take"
TG_LEAD = "tg.lead"                # уведомление о новом / брошенном лиде
TG_REMIND = "tg.remind"            # напоминание: лид никто не взял
TG_CLIENT_MSG = "tg.client_msg"    # клиент дописал после анкеты
TG_DIGEST = "tg.digest"            # утренний дайджест ночных лидов

Event = tuple[str, dict[str, Any]]


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat(timespec="seconds")


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
    trello_card_url: str | None = None
    notified_status: str | None = None
    notified_at: str | None = None
    taken_by_id: int | None = None
    taken_by_name: str | None = None
    taken_at: str | None = None
    reminders_sent: int = 0
    last_reminder_at: str | None = None
    client_msgs_notified: int = 0


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
    queue: str | None
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
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        for table, column, ddl in MIGRATIONS:
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row["name"] for row in await cur.fetchall()}
            if column not in columns:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def ping(self) -> None:
        """Проверка для сторожа: база отвечает и пишется."""
        await self.kv_set("_ping", now_iso())

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

    async def update_lead(self, lead_id: int, events: Sequence[Event] = (), **fields: Any) -> Lead:
        """Обновить лид. events — задачи outbox, которые ставятся в той же транзакции."""
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"Unknown lead fields: {unknown}")
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            await self.conn.execute(
                f"UPDATE leads SET {cols}, updated_at = ? WHERE id = ?",
                (*fields.values(), now_iso(), lead_id),
            )
            if fields.keys() & CARD_FIELDS:
                await self._enqueue(CARD_UPDATE, lead_id, coalesce=True)
        for kind, payload in events:
            await self._enqueue(kind, lead_id, payload)
        if fields or events:
            await self._commit()
        lead = await self.get_lead(lead_id)
        assert lead is not None
        return lead

    async def take_lead(self, lead_id: int, *, by_id: int, by_name: str) -> bool:
        """Менеджер берёт лид. False — лид уже кто-то взял (два нажатия одновременно)."""
        cur = await self.conn.execute(
            "UPDATE leads SET taken_by_id = ?, taken_by_name = ?, taken_at = ?, updated_at = ?"
            " WHERE id = ? AND taken_at IS NULL",
            (by_id, by_name, now_iso(), now_iso(), lead_id),
        )
        if cur.rowcount == 0:
            await self.conn.rollback()
            return False
        await self._enqueue(CARD_TAKE, lead_id, {"by": by_name})
        await self._commit()
        return True

    async def _leads(self, where: str, params: Sequence[Any] = ()) -> list[Lead]:
        async with self.conn.execute(f"SELECT * FROM leads WHERE {where} ORDER BY id", params) as cur:
            rows = await cur.fetchall()
        return [_lead(r) for r in rows]

    async def leads_to_abandon(self, idle_since: datetime) -> list[Lead]:
        """Анкета не закончена, и клиент молчит с момента idle_since."""
        return await self._leads(
            "status = 'new' AND COALESCE("
            " (SELECT MAX(created_at) FROM messages WHERE lead_id = leads.id AND direction = 'in'),"
            " created_at) < ?",
            (now_iso(idle_since),),
        )

    async def leads_to_notify(self) -> list[Lead]:
        """Лиды, о текущем статусе которых менеджер ещё не знает."""
        return await self._leads(
            "status IN ('qualified', 'abandoned') AND notified_status IS NOT status AND taken_at IS NULL"
        )

    async def leads_waiting(self) -> list[Lead]:
        """Менеджер уведомлён, но лид никто не взял."""
        return await self._leads("notified_at IS NOT NULL AND taken_at IS NULL")

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

    async def get_messages(self, lead_id: int, *, after_id: int = 0, direction: str | None = None) -> list[Message]:
        sql = "SELECT * FROM messages WHERE lead_id = ? AND id > ?"
        params: list[Any] = [lead_id, after_id]
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        async with self.conn.execute(sql + " ORDER BY id", params) as cur:
            rows = await cur.fetchall()
        return [Message(**dict(r)) for r in rows]

    # --- outbox ---

    async def enqueue(
        self, kind: str, lead_id: int | None, payload: dict[str, Any] | None = None,
        *, coalesce: bool = False, delay: timedelta | None = None,
    ) -> None:
        await self._enqueue(kind, lead_id, payload, coalesce=coalesce, delay=delay)
        await self._commit()

    async def _enqueue(
        self, kind: str, lead_id: int | None, payload: dict[str, Any] | None = None,
        *, coalesce: bool = False, delay: timedelta | None = None,
    ) -> None:
        """Поставить задачу в очередь. Без commit — вызывающий коммитит вместе со своей записью."""
        if coalesce:
            async with self.conn.execute(
                "SELECT 1 FROM outbox WHERE kind = ? AND lead_id IS ? AND done_at IS NULL AND attempts = 0",
                (kind, lead_id),
            ) as cur:
                if await cur.fetchone():
                    return  # такая же задача ещё ждёт и возьмёт актуальные данные на момент выполнения
        now = datetime.now(UTC)
        queue = f"{kind.split('.')[0]}:{lead_id}" if lead_id is not None else None
        await self.conn.execute(
            "INSERT INTO outbox (kind, lead_id, queue, payload, next_attempt_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, lead_id, queue, json.dumps(payload or {}, ensure_ascii=False),
             now_iso(now + (delay or timedelta())), now_iso(now)),
        )

    async def _commit(self) -> None:
        await self.conn.commit()
        if self.on_enqueue:
            self.on_enqueue()

    async def outbox_pending(self, limit: int = 200) -> list[OutboxTask]:
        """Невыполненные задачи в порядке постановки (включая ещё не наступившие ретраи)."""
        async with self.conn.execute(
            "SELECT id, kind, lead_id, queue, payload, attempts, next_attempt_at, last_error"
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

    # --- служебные значения ---

    async def kv_get(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
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
