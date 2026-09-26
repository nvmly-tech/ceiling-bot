"""Воркер очереди outbox: выполняет внешние вызовы (Trello и др.) с ретраями.

У каждого лида своя очередь на каждый канал (trello:<лид>, tg:<лид>). Задачи одной очереди
выполняются строго по порядку: если задача упала, следующие ждут её успешного повтора.
Остальные очереди продолжают работать — упавший Trello не задерживает уведомления в Telegram.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.db import Database, OutboxTask

log = logging.getLogger(__name__)

Handler = Callable[[OutboxTask], Awaitable[None]]

BACKOFF_BASE = 5  # с
BACKOFF_MAX = 30 * 60  # с


def backoff(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой: 5 с, 10 с, 20 с … но не больше 30 мин."""
    return timedelta(seconds=min(BACKOFF_BASE * 2**attempts, BACKOFF_MAX))


class Outbox:
    def __init__(self, db: Database, handlers: dict[str, Handler], poll_interval: float = 5.0):
        self.db = db
        self.handlers = handlers
        self.poll_interval = poll_interval
        self._wake = asyncio.Event()
        self.last_run: datetime | None = None  # для сторожа (этап 6)
        db.on_enqueue = self._wake.set

    async def run_once(self, now: datetime | None = None) -> int:
        """Один проход по очереди. Возвращает число выполненных задач."""
        now = now or datetime.now(UTC)
        blocked: set[str] = set()
        done = 0
        for task in await self.db.outbox_pending():
            if task.queue is not None and task.queue in blocked:
                continue
            handler = self.handlers.get(task.kind)
            not_due = datetime.fromisoformat(task.next_attempt_at) > now
            if handler is None or not_due:
                # Нет обработчика (например, Trello не настроен) или ретрай ещё не наступил — ждём, сохраняя порядок.
                if task.queue is not None:
                    blocked.add(task.queue)
                continue
            try:
                await handler(task)
            except Exception as e:  # noqa: BLE001 — любая ошибка внешнего вызова уходит в ретрай
                delay = backoff(task.attempts)
                log.warning("outbox #%s %s (лид %s): %s; повтор через %s", task.id, task.kind, task.lead_id, e, delay)
                await self.db.outbox_retry(task.id, str(e), (now + delay).isoformat(timespec="seconds"))
                if task.queue is not None:
                    blocked.add(task.queue)
                continue
            await self.db.outbox_done(task.id)
            done += 1
        self.last_run = datetime.now(UTC)
        return done

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("outbox: сбой прохода очереди")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
            self._wake.clear()
