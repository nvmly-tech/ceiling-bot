"""Воркер очереди outbox: выполняет внешние вызовы (Trello, Telegram, расшифровка) с ретраями.

Канал — префикс вида задачи до точки (trello, tg, stt, shadow). У каждого канала свой воркер: зависший Trello
не задерживает уведомления в Telegram, расшифровка голосового — ни того, ни другого. Внутри канала заявки
обрабатываются параллельно (не больше PARALLEL задач одновременно), а задачи одной заявки (очередь
«<канал>:<лид>») — строго по порядку: если задача упала, следующие ждут её успешного повтора.

Для сторожа — last_run: самый давний признак жизни среди каналов. Он обновляется после каждой выполненной
задачи, поэтому долгий разбор завала (сотни задач после простоя Trello) — не повод перезапускать бота.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.db import Database, OutboxTask
from app.redact import redact

log = logging.getLogger(__name__)

Handler = Callable[[OutboxTask], Awaitable[None]]

BACKOFF_BASE = 5  # с
BACKOFF_MAX = 30 * 60  # с
# Сколько заявок канала обрабатывать одновременно. Telegram: резюме от LLM (до 8 с) одной заявки не держит
# уведомления о других; темп отправки в группу держит ограничитель (ratelimit). Trello: в пределах его лимита
# 100 запросов за 10 с на токен. Расшифровка — по одной: GigaAM всё равно считает по одной.
PARALLEL = {"tg": 4, "trello": 2}
IDLE = "idle"  # воркер без каналов: ничего не делает, но отчитывается сторожу
# Порядок каналов в разовом проходе run_once: уведомлению нужна ссылка на карточку, а сообщению — расшифровка.
# В работе (run) каналы идут параллельно, и уведомление при медленном Trello просто подождёт карточку.
ONCE_ORDER = ("trello", "stt", "shadow")


def backoff(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой: 5 с, 10 с, 20 с … но не больше 30 мин."""
    return timedelta(seconds=min(BACKOFF_BASE * 2**attempts, BACKOFF_MAX))


def channel_of(kind: str) -> str:
    return kind.split(".")[0]


class Outbox:
    def __init__(
        self, db: Database, handlers: dict[str, Handler], poll_interval: float = 5.0,
        parallel: dict[str, int] | None = None,
    ):
        self.db = db
        self.handlers = handlers
        self.poll_interval = poll_interval
        self.parallel = PARALLEL if parallel is None else parallel
        self.channels = sorted({channel_of(k) for k in handlers}) or [IDLE]
        self._wake = {ch: asyncio.Event() for ch in self.channels}
        self.beats: dict[str, datetime] = {}  # канал → последний признак жизни
        db.on_enqueue = self._wake_all

    def _wake_all(self) -> None:
        for event in self._wake.values():
            event.set()

    def lagging(self) -> tuple[str, datetime] | None:
        """Канал, дольше всех не подававший признаков жизни; None — какой-то ещё ни разу не отчитался."""
        if any(ch not in self.beats for ch in self.channels):
            return None
        return min(((ch, self.beats[ch]) for ch in self.channels), key=lambda item: item[1])

    @property
    def last_run(self) -> datetime | None:
        """Для сторожа: признак жизни самого отстающего канала."""
        lag = self.lagging()
        return lag[1] if lag else None

    @last_run.setter
    def last_run(self, value: datetime) -> None:
        """Отметить все каналы разом (тесты сторожа)."""
        self.beats = dict.fromkeys(self.channels, value)

    async def run_once(self, now: datetime | None = None) -> int:
        """Один проход по всем каналам, по очереди (ONCE_ORDER, остальные — после). Для тестов и разового
        прогона; возвращает число выполненных задач."""
        rank = {ch: i for i, ch in enumerate(ONCE_ORDER)}
        done = 0
        for channel in sorted(self.channels, key=lambda ch: (rank.get(ch, len(rank)), ch)):
            done += await self.run_channel(channel, now)
        return done

    async def run_channel(self, channel: str, now: datetime | None = None) -> int:
        """Один проход по каналу: очереди заявок — параллельно, задачи внутри очереди — по порядку."""
        now = now or datetime.now(UTC)
        kinds = [k for k in self.handlers if channel_of(k) == channel]
        groups: dict[str, list[OutboxTask]] = {}
        for task in await self.db.outbox_pending(kinds):
            groups.setdefault(task.queue or f"#{task.id}", []).append(task)
        slots = asyncio.Semaphore(self.parallel.get(channel, 1))

        async def drain(tasks: list[OutboxTask]) -> int:
            done = 0
            async with slots:
                for task in tasks:
                    if datetime.fromisoformat(task.next_attempt_at) > now:
                        break  # повтор ещё не наступил — очередь ждёт, сохраняя порядок
                    if not await self._execute(task, now):
                        break
                    done += 1
                    self.beats[channel] = datetime.now(UTC)
            return done

        done = await asyncio.gather(*(drain(tasks) for tasks in groups.values()))
        self.beats[channel] = datetime.now(UTC)
        return sum(done)

    async def _execute(self, task: OutboxTask, now: datetime) -> bool:
        """Выполнить задачу. False — не вышло, повтор назначен; следующие задачи её очереди ждут."""
        try:
            await self.handlers[task.kind](task)
        except Exception as e:  # noqa: BLE001 — любая ошибка внешнего вызова уходит в ретрай
            delay = backoff(task.attempts)
            error = redact(str(e) or type(e).__name__)  # ошибка попадёт в базу и в /status
            log.warning("outbox #%s %s (лид %s): %s; повтор через %s", task.id, task.kind, task.lead_id, error, delay)
            await self.db.outbox_retry(task.id, error, (now + delay).isoformat(timespec="seconds"))
            return False
        await self.db.outbox_done(task.id)
        return True

    async def run(self) -> None:
        await asyncio.gather(*(self._run_channel(ch) for ch in self.channels))

    async def _run_channel(self, channel: str) -> None:
        wake = self._wake[channel]
        while True:
            wake.clear()  # до прохода: задача, поставленная во время него, разбудит следующий сразу
            try:
                await self.run_channel(channel)
            except Exception:
                log.exception("outbox: сбой прохода канала %s", channel)
            try:
                await asyncio.wait_for(wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
