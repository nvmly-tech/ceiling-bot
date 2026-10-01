"""SQLite: лиды, переписка, очередь исходящих вызовов (outbox), состояние FSM и служебные значения.

Одно соединение на процесс, режим WAL. Время хранится в UTC (ISO 8601). Запись — только через _tx():
обработчики aiogram и очередь работают параллельно, а транзакция у соединения одна на всех.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

from app.schema import BACKFILL, MIGRATIONS, SCHEMA
from app.stages import MEASURE

T = TypeVar("T")
LLM_DAY_KEY = "llm_calls:{day}"  # kv: обращений к LLM за день (по часовому поясу студии)


class _NoBudget(Exception):
    """Обращение к LLM не списано (лимит заявки или дня исчерпан) — откатить транзакцию."""
HISTORY_LIMIT = 50  # сколько прошлых заявок клиента читаем (показываем — несколько последних)

# Поля анкеты: их изменение обновляет карточку в Trello.
CARD_FIELDS = {
    "name", "username", "object", "area_m2", "area_text", "ceiling_type",
    "phone", "measure_time", "status", "is_night", "summary", "hotness", "hotness_reason", "summary_model",
    "stage", "measure_at", "refuse_reason", "rating",
}
LEAD_FIELDS = CARD_FIELDS | {
    "trello_card_id", "trello_card_url", "completed_at", "notified_status", "notified_at",
    "reminders_sent", "last_reminder_at", "client_msgs_notified", "summary_status", "stage_at", "stage_by_name",
    "nudges_sent", "last_nudge_at", "escalated_at", "measure_reminded_for", "contact_asked_at", "contact_answer",
    "rating_asked_at", "visit_answer",
}
ONCE_FIELDS = {"contact_answer", "rating"}  # ответы клиента: принимается только первый

# Задачи outbox. Префикс до точки — канал: у каждого лида своя очередь на канал.
CARD_CREATE = "trello.card_create"
CARD_UPDATE = "trello.card_update"
CARD_COMMENT = "trello.comment"
CARD_TAKE = "trello.card_take"
CARD_ATTACH = "trello.attach"          # приложить файл сообщения (голосовое, фото, документ)
CARD_TRANSCRIPT = "trello.transcript"  # комментарий с отложенной расшифровкой голосового
CARD_DELETE = "trello.card_delete"     # клиент удалил заявку — удалить карточку
CARD_STAGE = "trello.card_stage"       # менеджер отметил этап — карточку в нужный список и комментарий
STT_TRANSCRIBE = "stt.transcribe"      # отложенная расшифровка голосового
STT_SHADOW = "shadow.stt"              # теневая расшифровка другой моделью — только для сравнения, клиенту не видна
TG_LEAD = "tg.lead"                # уведомление о новом / брошенном лиде
TG_REMIND = "tg.remind"            # напоминание: лид никто не взял
TG_CLIENT_MSG = "tg.client_msg"    # клиент дописал после анкеты
TG_DIGEST = "tg.digest"            # утренний дайджест ночных лидов
TG_DELETED = "tg.deleted"          # клиент удалил заявку, о которой менеджер уже знал
TG_PANEL = "tg.panel"              # панель взятой заявки: кнопки этапов (замер, отказ, договор…)
TG_NUDGE = "tg.nudge"              # напоминание менеджеру: взятая заявка без итога
TG_ESCALATE = "tg.escalate"        # владельцу: заявку никто не взял / нет итога
TG_VISIT = "tg.visit"              # менеджерам: клиент ответил о замере (жду / перенести / отменить)
TG_TO_CLIENT = "tg.to_client"      # клиенту: замер назначен, напоминание накануне, вопросы о качестве
TG_FEEDBACK = "tg.feedback"        # менеджерам и владельцу: клиент ответил «связались ли» / оценил замер
TG_REPORT = "tg.report"            # владельцу: недельный отчёт по заявкам
ALL_KINDS = (CARD_CREATE, CARD_UPDATE, CARD_COMMENT, CARD_TAKE, CARD_ATTACH, CARD_TRANSCRIPT, CARD_DELETE, CARD_STAGE,
             STT_TRANSCRIBE, STT_SHADOW, TG_LEAD, TG_REMIND, TG_CLIENT_MSG, TG_DIGEST, TG_DELETED, TG_PANEL, TG_NUDGE,
             TG_ESCALATE, TG_VISIT, TG_TO_CLIENT, TG_FEEDBACK, TG_REPORT)

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
    summary: str | None = None
    hotness: str | None = None
    hotness_reason: str | None = None
    summary_model: str | None = None
    summary_status: str | None = None
    llm_calls: int = 0
    stage: str | None = None
    stage_at: str | None = None
    stage_by_name: str | None = None
    measure_at: str | None = None
    refuse_reason: str | None = None
    taken_by_username: str | None = None
    nudges_sent: int = 0
    last_nudge_at: str | None = None
    escalated_at: str | None = None
    measure_reminded_for: str | None = None
    contact_asked_at: str | None = None
    contact_answer: str | None = None
    rating_asked_at: str | None = None
    rating: int | None = None
    source: str | None = None
    visit_answer: str | None = None


@dataclass
class Message:
    id: int
    lead_id: int
    direction: str
    kind: str
    text: str | None
    file_id: str | None
    model: str | None  # ответ бота — кто его сформировал; голосовое клиента — какая модель расшифровала
    created_at: str
    text_alt: str | None = None
    text_alt_model: str | None = None


@dataclass
class TgMessage:
    id: int
    lead_id: int
    chat_id: int
    message_id: int
    kind: str
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
        self._write = asyncio.Lock()
        self._enqueued = False  # в текущей транзакции поставлены задачи — после commit разбудить воркер

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
                if backfill := BACKFILL.get((table, column)):
                    await self.conn.execute(backfill)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[None]:
        """Одна транзакция за раз. Без этого commit одной корутины фиксировал чужую незаконченную запись,
        а rollback — стирал её (второе «Взял» стирало сообщение клиента, пришедшее в ту же миллисекунду)."""
        async with self._write:
            self._enqueued = False
            try:
                yield
            except BaseException:
                await self.conn.rollback()
                raise
            await self.conn.commit()
            if self._enqueued and self.on_enqueue:
                self.on_enqueue()

    async def ping(self) -> None:
        """Проверка для сторожа: база отвечает и пишется."""
        await self.kv_set("_ping", now_iso())

    # --- лиды ---

    async def create_lead(
        self, *, tg_user_id: int, chat_id: int, name: str | None, username: str | None, is_night: bool,
        source: str | None = None,
    ) -> Lead:
        ts = now_iso()
        async with self._tx():
            cur = await self.conn.execute(
                "INSERT INTO leads (tg_user_id, chat_id, name, username, is_night, source, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tg_user_id, chat_id, name, username, int(is_night), source, ts, ts),
            )
            await self._enqueue(CARD_CREATE, cur.lastrowid)
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
        if fields or events:
            async with self._tx():
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
        lead = await self.get_lead(lead_id)
        assert lead is not None
        return lead

    async def take_lead(
        self, lead_id: int, *, by_id: int, by_name: str, by_username: str | None = None, reply_to: int | None = None,
    ) -> bool:
        """Менеджер берёт лид. False — лид уже кто-то взял (два нажатия одновременно).
        reply_to — сообщение, под которым нажали «Взял»: панель заявки придёт ответом на него.
        Счётчики напоминаний обнуляются: владельцу могли сообщить, что заявку никто не берёт, — теперь новый отсчёт."""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE leads SET taken_by_id = ?, taken_by_name = ?, taken_by_username = ?, taken_at = ?,"
                " updated_at = ?, nudges_sent = 0, last_nudge_at = NULL, escalated_at = NULL"
                " WHERE id = ? AND taken_at IS NULL",
                (by_id, by_name, by_username, now_iso(), now_iso(), lead_id),
            )
            if cur.rowcount == 0:
                return False  # уже взята: запрос ничего не изменил — откатывать нечего
            await self._enqueue(CARD_TAKE, lead_id, {"by": by_name})
            await self._enqueue(TG_PANEL, lead_id, {"reply_to": reply_to})
        return True

    async def set_stage(
        self, lead_id: int, stage: str | None, *, by_name: str, measure_at: str | None = None,
        reason: str | None = None,
    ) -> Lead:
        """Менеджер отметил этап. Дата замера сохраняется и после него (для истории и вопросов клиенту)."""
        fields: dict[str, Any] = {
            "stage": stage, "stage_at": now_iso(), "stage_by_name": by_name, "refuse_reason": reason,
            # Новый этап — новый отсчёт напоминаний и эскалации; ответ клиента о прежнем замере уже не в счёт.
            "nudges_sent": 0, "last_nudge_at": None, "escalated_at": None, "visit_answer": None,
        }
        if measure_at is not None:
            fields["measure_at"] = measure_at
        lead = await self.get_lead(lead_id)
        # В задаче — всё для комментария: к её выполнению этап может смениться ещё раз.
        payload = {"stage": stage, "by": by_name, "reason": reason,
                   "measure_at": measure_at or (lead.measure_at if lead else None)}
        events: list[Event] = [(CARD_STAGE, payload)]
        if stage == MEASURE and measure_at:
            events.append((TG_TO_CLIENT, {"what": "measure_set", "at": measure_at}))  # клиенту — когда замер
        return await self.update_lead(lead_id, events=events, **fields)

    async def count_leads_since(self, tg_user_id: int, since: datetime) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE tg_user_id = ? AND created_at >= ?", (tg_user_id, now_iso(since))
        ) as cur:
            return (await cur.fetchone())[0]

    async def count_incoming_since(self, lead_id: int, since: datetime) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE lead_id = ? AND direction = 'in' AND created_at >= ?",
            (lead_id, now_iso(since)),
        ) as cur:
            return (await cur.fetchone())[0]

    async def spend_llm_call(self, lead_id: int, limit: int, *, day_limit: int = 0, day: str = "") -> bool:
        """Списать одно обращение к LLM: из бюджета заявки (limit) и общего бюджета дня (day_limit, 0 — без
        лимита; day — дата по часовому поясу студии). False — один из них исчерпан, не списано ничего.
        Счётчик заявки в базе, а не в FSM: сброс состояния диалога (/start) его не обнуляет."""
        try:
            async with self._tx():
                cur = await self.conn.execute(
                    "UPDATE leads SET llm_calls = llm_calls + 1 WHERE id = ? AND llm_calls < ?", (lead_id, limit)
                )
                if cur.rowcount == 0 or (day and not await self._spend_day(day_limit, day)):
                    raise _NoBudget
        except _NoBudget:
            return False
        return True

    async def spend_llm_day(self, limit: int, day: str) -> bool:
        """Списать обращение к LLM только из бюджета дня (резюме лида). False — день исчерпан."""
        async with self._tx():
            return await self._spend_day(limit, day)

    async def _spend_day(self, limit: int, day: str) -> bool:
        cur = await self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE"
            " SET value = CAST(value AS INTEGER) + 1 WHERE ? = 0 OR CAST(kv.value AS INTEGER) < ?",
            (LLM_DAY_KEY.format(day=day), limit, limit),
        )
        return cur.rowcount == 1

    async def llm_calls_on(self, day: str) -> int:
        return int(await self.kv_get(LLM_DAY_KEY.format(day=day)) or 0)

    async def _leads(self, where: str, params: Sequence[Any] = ()) -> list[Lead]:
        async with self.conn.execute(f"SELECT * FROM leads WHERE {where} ORDER BY id", params) as cur:
            rows = await cur.fetchall()
        return [_lead(r) for r in rows]

    async def previous_leads(self, lead: Lead) -> list[Lead]:
        """Прошлые заявки того же клиента — с того же Telegram-аккаунта или с тем же телефоном; новые — первыми.
        Удалённые и закрытые клиентом не в счёт."""
        same = "tg_user_id = ?"
        params: list[Any] = [lead.id, lead.tg_user_id]
        if lead.phone and lead.phone.startswith("+"):  # «не оставил номер» — не признак одного человека
            same += " OR phone = ?"
            params.append(lead.phone)
        async with self.conn.execute(
            f"SELECT * FROM leads WHERE id < ? AND status NOT IN ('cancelled', 'deleted') AND ({same})"
            f" ORDER BY id DESC LIMIT {HISTORY_LIMIT}",
            params,
        ) as cur:
            return [_lead(r) for r in await cur.fetchall()]

    async def leads_created_between(self, since: datetime, until: datetime) -> list[Lead]:
        """Заявки, созданные за период, — для отчёта владельцу."""
        return await self._leads("created_at >= ? AND created_at < ?", (now_iso(since), now_iso(until)))

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
        """Менеджер уведомлён, но лид никто не взял (закрытые клиентом — не в счёт: о них не напоминаем)."""
        return await self._leads(
            "notified_at IS NOT NULL AND taken_at IS NULL AND status NOT IN ('cancelled', 'deleted')"
        )

    async def leads_in_work(self) -> list[Lead]:
        """Взятые заявки без итога (договор или отказ): о них напоминаем менеджеру."""
        return await self._leads(
            "taken_at IS NOT NULL AND status NOT IN ('cancelled', 'deleted')"
            " AND (stage IS NULL OR stage NOT IN ('contract', 'refused'))"
        )

    async def set_once(self, lead_id: int, field: str, value: Any) -> bool:
        """Записать ответ клиента, если его ещё нет. False — уже отвечал (или кнопку нажали дважды)."""
        if field not in ONCE_FIELDS:
            raise ValueError(f"Not a once-field: {field}")
        async with self._tx():
            cur = await self.conn.execute(
                f"UPDATE leads SET {field} = ?, updated_at = ? WHERE id = ? AND {field} IS NULL",
                (value, now_iso(), lead_id),
            )
            if cur.rowcount and field in CARD_FIELDS:
                await self._enqueue(CARD_UPDATE, lead_id, coalesce=True)
        return cur.rowcount == 1

    async def leads_to_ask_contact(self) -> list[Lead]:
        """Взятые заявки, у которых этап не отмечали ни разу (stage_at пуст: вернули в работу после итога —
        с клиентом уже общались), а клиента ещё не спрашивали, связались ли с ним."""
        return await self._leads(
            "taken_at IS NOT NULL AND stage IS NULL AND stage_at IS NULL AND contact_asked_at IS NULL"
            " AND status NOT IN ('cancelled', 'deleted')"
        )

    async def leads_to_ask_rating(self) -> list[Lead]:
        """Заявки с замером, оценку которого у клиента ещё не спрашивали."""
        return await self._leads(
            "measure_at IS NOT NULL AND rating_asked_at IS NULL AND status NOT IN ('cancelled', 'deleted')"
            " AND stage IN ('measure', 'thinking', 'contract', 'refused')"
        )

    async def leads_to_remind_measure(self) -> list[Lead]:
        """Назначенные замеры, о которых клиенту ещё не напоминали (перенесённый — напомнить заново)."""
        return await self._leads(
            "stage = 'measure' AND measure_at IS NOT NULL AND status NOT IN ('cancelled', 'deleted')"
            " AND measure_reminded_for IS NOT measure_at"
        )

    async def delete_lead_data(self, lead_id: int) -> None:
        """Клиент удалил заявку: стереть его данные в одной транзакции. Остаётся обезличенная строка (номер и
        status='deleted') — по ней очередь удалит карточку Trello, если она есть или создаётся прямо сейчас."""
        async with self._tx():
            lead = await self.get_lead(lead_id)
            if lead is None or lead.status == "deleted":
                return
            # Карточка есть, создаётся прямо сейчас или ждёт настройки Trello — её надо будет удалить.
            async with self.conn.execute(
                "SELECT 1 FROM outbox WHERE lead_id = ? AND kind = ?", (lead_id, CARD_CREATE)
            ) as cur:
                card_planned = await cur.fetchone() is not None
            # Невыполненные задачи (карточка, комментарии, уведомления, расшифровки) больше не нужны.
            await self.conn.execute("DELETE FROM outbox WHERE lead_id = ? AND done_at IS NULL", (lead_id,))
            await self.conn.execute("DELETE FROM messages WHERE lead_id = ?", (lead_id,))
            await self.conn.execute(
                "UPDATE leads SET status = 'deleted', tg_user_id = 0, chat_id = 0, name = NULL, username = NULL,"
                " object = NULL, area_m2 = NULL, area_text = NULL, ceiling_type = NULL, phone = NULL,"
                " measure_time = NULL, summary = NULL, hotness = NULL, hotness_reason = NULL, summary_model = NULL,"
                " summary_status = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), lead_id),
            )
            if lead.trello_card_id or card_planned:
                await self._enqueue(CARD_DELETE, lead_id)
            if lead.notified_at or await self.tg_messages(lead_id):
                await self._enqueue(TG_DELETED, lead_id)  # сообщить менеджеру и убрать сообщения о заявке из группы

    # --- сообщения бота в группе менеджеров ---

    async def add_tg_message(self, lead_ids: Sequence[int], chat_id: int, message_id: int, kind: str) -> None:
        async with self._tx():
            await self.conn.executemany(
                "INSERT INTO tg_messages (lead_id, chat_id, message_id, kind, created_at) VALUES (?, ?, ?, ?, ?)",
                [(lead_id, chat_id, message_id, kind, now_iso()) for lead_id in lead_ids],
            )

    async def tg_messages(self, lead_id: int) -> list[TgMessage]:
        async with self.conn.execute("SELECT * FROM tg_messages WHERE lead_id = ? ORDER BY id", (lead_id,)) as cur:
            return [_row(TgMessage, r) for r in await cur.fetchall()]

    async def tg_message_leads(self, chat_id: int, message_id: int) -> list[int]:
        """Заявки, о которых одно сообщение (сводка)."""
        async with self.conn.execute(
            "SELECT lead_id FROM tg_messages WHERE chat_id = ? AND message_id = ? ORDER BY id", (chat_id, message_id)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def leads_by_phones(self, phones: Sequence[str]) -> list[int]:
        """Живые заявки с этими номерами (+7XXXXXXXXXX) — для сообщений менеджеров, где упомянут телефон клиента."""
        if not phones:
            return []
        marks = ", ".join("?" * len(phones))
        async with self.conn.execute(
            f"SELECT id FROM leads WHERE phone IN ({marks}) AND status != 'deleted' ORDER BY id", list(phones)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def delete_tg_messages(self, lead_id: int) -> None:
        async with self._tx():
            await self.conn.execute("DELETE FROM tg_messages WHERE lead_id = ?", (lead_id,))

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
        async with self._tx():
            cur = await self.conn.execute(
                "INSERT INTO messages (lead_id, direction, kind, text, file_id, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, direction, kind, text, file_id, model, now_iso()),
            )
            await self._enqueue(CARD_COMMENT, lead_id, {"message_id": cur.lastrowid})
            if file_id:
                await self._enqueue(CARD_ATTACH, lead_id, {"message_id": cur.lastrowid})
        return cur.lastrowid

    async def set_message_text(self, message_id: int, text: str, model: str | None = None) -> None:
        async with self._tx():
            await self.conn.execute(
                "UPDATE messages SET text = ?, model = COALESCE(?, model) WHERE id = ?", (text, model, message_id)
            )

    async def set_message_alt(self, message_id: int, text: str, model: str) -> None:
        async with self._tx():
            await self.conn.execute(
                "UPDATE messages SET text_alt = ?, text_alt_model = ? WHERE id = ?", (text, model, message_id)
            )

    async def stt_pairs(self, since: datetime) -> list[Message]:
        """Голосовые, расшифрованные обеими моделями (основной и теневой), — для сравнения."""
        async with self.conn.execute(
            "SELECT * FROM messages WHERE kind = 'voice' AND text IS NOT NULL AND text_alt IS NOT NULL"
            " AND created_at >= ? ORDER BY id",
            (now_iso(since),),
        ) as cur:
            return [_row(Message, r) for r in await cur.fetchall()]

    async def get_message(self, message_id: int) -> Message | None:
        async with self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)) as cur:
            row = await cur.fetchone()
        return _row(Message, row) if row else None

    async def get_messages(self, lead_id: int, *, after_id: int = 0, direction: str | None = None) -> list[Message]:
        sql = "SELECT * FROM messages WHERE lead_id = ? AND id > ?"
        params: list[Any] = [lead_id, after_id]
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        async with self.conn.execute(sql + " ORDER BY id", params) as cur:
            rows = await cur.fetchall()
        return [_row(Message, r) for r in rows]

    # --- outbox ---

    async def enqueue(
        self, kind: str, lead_id: int | None, payload: dict[str, Any] | None = None,
        *, coalesce: bool = False, delay: timedelta | None = None,
    ) -> None:
        async with self._tx():
            await self._enqueue(kind, lead_id, payload, coalesce=coalesce, delay=delay)

    async def _enqueue(
        self, kind: str, lead_id: int | None, payload: dict[str, Any] | None = None,
        *, coalesce: bool = False, delay: timedelta | None = None,
    ) -> None:
        """Поставить задачу в очередь. Только внутри _tx() — вместе с записью, ради которой она ставится."""
        self._enqueued = True
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

    async def outbox_pending(
        self, kinds: Collection[str] | None = None, *, per_queue: int = 20, limit: int = 1000
    ) -> list[OutboxTask]:
        """Невыполненные задачи в порядке постановки (включая ещё не наступившие ретраи).
        kinds — только задачи с обработчиком: задачи ненастроенного канала не должны занимать выборку.
        Из каждой очереди — не больше per_queue первых: одна длинная очередь не вытесняет остальные."""
        where, params = "done_at IS NULL", []
        if kinds is not None:
            # Очереди однородны по каналу (trello:<id>, tg:<id>, stt:<id>), поэтому фильтр по виду не ломает порядок.
            where += f" AND kind IN ({', '.join('?' * len(kinds))})"
            params = list(kinds)
        async with self.conn.execute(
            "SELECT id, kind, lead_id, queue, payload, attempts, next_attempt_at, last_error FROM ("
            "  SELECT *, ROW_NUMBER() OVER (PARTITION BY COALESCE(queue, 'id:' || id) ORDER BY id) AS pos"
            f"  FROM outbox WHERE {where}"
            ") WHERE pos <= ? ORDER BY id LIMIT ?",
            (*params, per_queue, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [OutboxTask(**{**dict(r), "payload": json.loads(r["payload"])}) for r in rows]

    async def outbox_done(self, task_id: int) -> None:
        async with self._tx():
            await self.conn.execute("UPDATE outbox SET done_at = ? WHERE id = ?", (now_iso(), task_id))

    async def outbox_retry(self, task_id: int, error: str, next_attempt_at: str) -> None:
        async with self._tx():
            await self.conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? WHERE id = ?",
                (error[:1000], next_attempt_at, task_id),
            )

    async def outbox_stats(self, handled: Collection[str] | None = None) -> dict[str, Any]:
        """Для сторожа и /status: сколько задач ждёт, сколько с ошибками, самая старая ошибочная;
        если передан handled — ещё и задачи, которые некому выполнить (канал не настроен)."""
        async with self.conn.execute(
            "SELECT COUNT(*) AS pending, SUM(attempts > 0) AS failing,"
            " MIN(CASE WHEN attempts > 0 THEN created_at END) AS oldest_failing_at"
            " FROM outbox WHERE done_at IS NULL"
        ) as cur:
            row = dict(await cur.fetchone())
        async with self.conn.execute(
            "SELECT kind, last_error FROM outbox WHERE done_at IS NULL AND attempts > 0 ORDER BY id DESC LIMIT 1"
        ) as cur:
            last = await cur.fetchone()
        unhandled = {"unhandled": 0, "unhandled_kinds": [], "oldest_unhandled_at": None}
        if handled is not None:
            async with self.conn.execute(
                "SELECT kind, COUNT(*) AS n, MIN(created_at) AS oldest FROM outbox"
                f" WHERE done_at IS NULL AND kind NOT IN ({', '.join('?' * len(handled))})"  # SQLite допускает IN ()
                " GROUP BY kind ORDER BY kind",
                list(handled),
            ) as cur:
                rows = await cur.fetchall()
            if rows:
                unhandled = {
                    "unhandled": sum(r["n"] for r in rows),
                    "unhandled_kinds": [r["kind"] for r in rows],
                    "oldest_unhandled_at": min(r["oldest"] for r in rows),
                }
        return {
            "pending": row["pending"] or 0,
            "failing": row["failing"] or 0,
            "oldest_failing_at": row["oldest_failing_at"],
            "failing_kind": last["kind"] if last else None,
            "last_error": last["last_error"] if last else None,
            **unhandled,
        }

    async def leads_today(self, zone: Any) -> dict[str, int]:
        """Заявки с начала суток по часовому поясу студии."""
        start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(status = 'qualified') AS qualified, SUM(taken_at IS NOT NULL) AS taken"
            " FROM leads WHERE created_at >= ?",
            (now_iso(start.astimezone(UTC)),),
        ) as cur:
            row = dict(await cur.fetchone())
        return {k: row[k] or 0 for k in ("total", "qualified", "taken")}

    # --- служебные значения ---

    async def kv_get(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str | None) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- FSM ---

    async def fsm_get(self, key: str) -> tuple[str | None, dict[str, Any]]:
        async with self.conn.execute("SELECT state, data FROM fsm WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None, {}
        return row["state"], json.loads(row["data"])

    async def fsm_set_state(self, key: str, state: str | None) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO fsm (key, state) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET state = excluded.state",
                (key, state),
            )

    async def fsm_set_data(self, key: str, data: dict[str, Any]) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO fsm (key, data) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET data = excluded.data",
                (key, json.dumps(data, ensure_ascii=False)),
            )


def _row(cls: type[T], row: aiosqlite.Row) -> T:
    """Строка таблицы → dataclass. Колонки, которых класс не знает, пропускаются: после отката кода
    на прошлую версию в базе остаются колонки, добавленные более новой."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in dict(row).items() if k in known})


def _lead(row: aiosqlite.Row) -> Lead:
    lead = _row(Lead, row)
    lead.is_night = bool(lead.is_night)
    return lead
