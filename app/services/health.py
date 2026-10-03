"""Сторож: watchdog для systemd, проверки моделей и очереди, алерты админу, /status.

Watchdog (WATCHDOG=1) шлётся, только если живо ядро бота: цикл опроса Telegram крутится, база пишется,
очередь и планировщик работают. Иначе systemd через WatchdogSec убьёт процесс и перезапустит.
Внешние сервисы (LLM, Groq, Trello) на watchdog не влияют — перезапуск их не чинит; об их падении
и восстановлении сторож пишет алертом.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot
from aiogram.methods import GetUpdates, TelegramMethod

from app.config import Settings
from app.db import Database
from app.redact import redact
from app.services import systemd
from app.services.llm import FormatError, Health, LLMRouter
from app.services.notifier import Notifier
from app.services.outbox import Outbox
from app.services.stt import Transcriber, compare_summary, model_label

log = logging.getLogger(__name__)

POLLING_STALE = 180        # с: ни одна попытка getUpdates не завершилась — цикл опроса завис
LOOP_STALE = 300           # с: очередь или планировщик не делали проход
DB_TIMEOUT = 10            # с
STARTUP_GRACE = 120        # с: первые минуты после старта опрос ещё не обязан отчитаться
MODEL_CHECK_INTERVAL = 300  # с
STT_FAIL_THRESHOLD = 2
QUEUE_STUCK_AFTER = timedelta(minutes=30)
STT_COMPARE_WINDOW = timedelta(days=14)  # за сколько дней /status сравнивает основную и теневую расшифровку
KV_RUNNING = "running"     # 1 — процесс работает; остался 1 при старте — прошлый запуск умер аварийно
KV_LLM_ALERTED = "llm_day_alerted"  # дата, за которую уже сообщили об исчерпанном дневном лимите LLM

REVISION_FILE = Path(__file__).resolve().parents[2] / "REVISION"


def revision() -> str:
    try:
        return REVISION_FILE.read_text().strip()
    except OSError:
        return "dev"


def ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "никогда"
    sec = int((now - moment).total_seconds())
    if sec < 90:
        return f"{sec} с назад"
    if sec < 5400:
        return f"{sec // 60} мин назад"
    return f"{sec // 3600} ч {sec % 3600 // 60} мин назад"


def llm_activity(h: Health, now: datetime) -> str:
    """Для /status: когда модель последний раз отвечала клиенту и когда сторож проверил её доступность.
    Время ответа живёт только в памяти, поэтому после перезапуска его нет — это не поломка."""
    checked = f"проверка ок {ago(h.last_check, now)}" if h.last_check else None
    if h.last_ok is None:
        return "с запуска ещё не отвечала, " + (
            checked or f"первая проверка — в течение {MODEL_CHECK_INTERVAL // 60} мин")
    answered = f"ответ {ago(h.last_ok, now)}"
    return f"{answered}, {checked}" if checked and h.last_check > h.last_ok else answered


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class Alerter:
    """Алерты в чат админа (ADMIN_CHAT_ID), а если он не задан — в чат менеджеров."""

    def __init__(self, bot: Bot, settings: Settings, notifier: Notifier):
        self.bot, self.settings, self.notifier = bot, settings, notifier
        self._tasks: set[asyncio.Task] = set()

    async def chat_id(self) -> int | None:
        return self.settings.admin_chat_id or await self.notifier.chat_id()

    async def send(self, text: str) -> None:
        text = redact(text)  # в алертах бывают тексты ошибок внешних API
        chat_id = await self.chat_id()
        if chat_id is None:
            log.warning("Алерт не отправлен (нет ADMIN_CHAT_ID и MANAGER_CHAT_ID): %s", text)
            return
        try:
            await self.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:  # noqa: BLE001 — алерт не должен ронять сторожа
            log.error("Алерт не отправлен: %s (%s)", e, text)

    def fire(self, text: str) -> None:
        """Отправить из синхронного кода (колбэк маршрутизатора LLM)."""
        task = asyncio.get_running_loop().create_task(self.send(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Дождаться отправки алертов, запущенных через fire (при остановке бота и в тестах)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class HealthMonitor:
    def __init__(
        self, bot: Bot, db: Database, settings: Settings, outbox: Outbox, notifier: Notifier, alerter: Alerter, *,
        llm: LLMRouter | None = None, stt: Transcriber | Sequence[Transcriber] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.bot, self.db, self.settings = bot, db, settings
        self.outbox, self.notifier, self.alerter = outbox, notifier, alerter
        self.llm = llm
        # Модели расшифровки по порядку: основная, резервная. У каждой — своё здоровье и свои алерты.
        self.stt: list[Transcriber] = list(stt) if isinstance(stt, (list, tuple)) else ([stt] if stt else [])
        self.clock = clock
        self.started_at = clock()
        self.polling_attempt: datetime | None = None  # последняя завершённая попытка getUpdates
        self.polling_ok: datetime | None = None       # последняя успешная
        self.stt_health = {model_label(t): Health() for t in self.stt}
        self.queue_stuck = False
        self.queue_unhandled = False  # есть задачи, которые некому выполнить (канал не настроен)
        if llm is not None:
            llm.on_status_change = self._on_llm_change

    # --- наблюдение за опросом Telegram ---

    async def session_middleware(
        self, make_request: Callable[..., Awaitable[Any]], bot: Bot, method: TelegramMethod
    ) -> Any:
        """Middleware сессии бота: отмечает каждую завершённую попытку getUpdates."""
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except Exception:
            self.polling_attempt = self.clock()
            raise
        self.polling_attempt = self.polling_ok = self.clock()
        return result

    # --- проверка ядра для watchdog ---

    async def check(self) -> Report:
        now = self.clock()
        report = Report()
        uptime = (now - self.started_at).total_seconds()
        if self.polling_attempt is None:
            if uptime > STARTUP_GRACE:
                report.problems.append("опрос Telegram не начался")
        elif (now - self.polling_attempt).total_seconds() > POLLING_STALE:
            report.problems.append(f"опрос Telegram завис ({ago(self.polling_attempt, now)})")
        try:
            await asyncio.wait_for(self.db.ping(), DB_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            report.problems.append(f"база не пишется: {e or type(e).__name__}")
        lag = self.outbox.lagging()
        queue = f"очередь ({lag[0]})" if lag else "очередь"
        for name, last in ((queue, self.outbox.last_run), ("планировщик", self.notifier.last_scan)):
            if last is None:
                if uptime > STARTUP_GRACE:
                    report.problems.append(f"{name} не запустилась")
            elif (now - last).total_seconds() > LOOP_STALE:
                report.problems.append(f"{name} стоит ({ago(last, now)})")
        return report

    async def run_watchdog(self) -> None:
        systemd.notify("READY=1")  # Type=notify: без него systemd считает, что бот не запустился
        interval = systemd.watchdog_interval()
        if interval is None:
            log.info("Watchdog systemd не включён (запуск не под systemd)")
            return
        last_ping: datetime | None = None  # пинга ещё не было — первый сразу, как ядро здорово
        while True:
            report = await self.check()
            if report.ok:
                systemd.notify("WATCHDOG=1")
                # Не time.monotonic(): он считает от загрузки машины, и сразу после перезагрузки сервера
                # «0 — давно» оказывалось меньше интервала — первый пинг откладывался (поймал CI на свежем раннере).
                now = self.clock()
                due = last_ping is None or (now - last_ping).total_seconds() > MODEL_CHECK_INTERVAL
                if self.settings.healthcheck_url and due:
                    last_ping = now
                    await self._ping_healthcheck()
            else:
                # Не пингуем — systemd перезапустит процесс, когда истечёт WatchdogSec.
                log.error("Сторож: ядро бота нездорово: %s", "; ".join(report.problems))
                systemd.notify(f"STATUS=нездоров: {'; '.join(report.problems)}")
            await asyncio.sleep(interval)

    async def _ping_healthcheck(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.get(self.settings.healthcheck_url)
        except httpx.HTTPError as e:
            log.warning("healthcheck: %s", type(e).__name__)

    # --- внешние сервисы: модели, расшифровка, очередь ---

    def _on_llm_change(self, label: str, up: bool, error: str | None) -> None:
        if up:
            self.alerter.fire(f"✅ Модель <b>{escape(label)}</b> снова работает")
            return
        text = (
            f"⚠️ Модель <b>{escape(label)}</b> недоступна, отключена на 5 мин.\n"
            f"<code>{escape(error or '')[:300]}</code>"
        )
        if self.llm and all(h.is_down for h in self.llm.health.values()):
            text += "\n\n🛑 Недоступны все модели — клиентам отвечает скрипт."
        self.alerter.fire(text)

    async def check_models(self) -> None:
        """Проверка моделей без лишних токенов:
        - модель недавно ответила клиенту — не проверяем, живой трафик уже всё показал;
        - иначе GET /models (0 токенов): API доступен, ключ рабочий, модель на месте;
        - настоящий запрос к модели — только пока она помечена упавшей, чтобы заметить восстановление."""
        if self.llm:
            now = self.clock()
            for p in self.llm.providers:
                h = self.llm.health[p.label]
                if not h.is_down and h.last_ok and (now - h.last_ok).total_seconds() < MODEL_CHECK_INTERVAL:
                    continue
                try:
                    if h.is_down:
                        await asyncio.wait_for(p.ping(), p.timeout)
                    else:
                        await asyncio.wait_for(p.check_available(), p.timeout)
                except FormatError:
                    self.llm.record_ok(p.label)  # ответила не по формату, но API работает
                except Exception as e:  # noqa: BLE001
                    self.llm.record_fail(p.label, str(e) or type(e).__name__)
                else:
                    # Успешный /models не обнуляет ошибки живых запросов: API может отвечать, а генерация — нет.
                    h.last_check = now
                    if h.is_down:
                        self.llm.record_ok(p.label)
        for t in self.stt:
            await self._check_stt(t)
        await self._check_llm_budget()

    async def _check_llm_budget(self) -> None:
        """Дневной лимит обращений к LLM исчерпан — один алерт за день: дальше анкету ведёт скрипт."""
        limit = self.settings.llm_calls_per_day
        if not self.llm or not limit:
            return
        day = self.clock().astimezone(self.settings.zone).date().isoformat()
        if await self.db.llm_calls_on(day) < limit or await self.db.kv_get(KV_LLM_ALERTED) == day:
            return
        await self.db.kv_set(KV_LLM_ALERTED, day)
        await self.alerter.send(
            f"🛑 Дневной лимит обращений к LLM ({limit}) исчерпан — до полуночи анкету ведёт скрипт.\n"
            "Если заявок столько не было — похоже на поток фейковых аккаунтов: проверьте группу и Trello. "
            "Лимит — LLM_CALLS_PER_DAY."
        )

    async def _check_stt(self, t: Transcriber) -> None:
        label = model_label(t)
        h = self.stt_health[label]
        try:
            await asyncio.wait_for(t.ping(), 20)
        except Exception as e:  # noqa: BLE001
            h.failures += 1
            h.last_error = str(e) or type(e).__name__
            if h.failures >= STT_FAIL_THRESHOLD and not h.is_down:
                h.down_until = self.clock()
                others_ok = any(not x.is_down for name, x in self.stt_health.items() if name != label)
                tail = "работает запасная модель" if others_ok else "голосовые принимаются, расшифровка ждёт в очереди"
                await self.alerter.send(
                    f"⚠️ Расшифровка голосовых ({escape(label)}) недоступна — {tail}."
                    f"\n<code>{escape(h.last_error)[:300]}</code>"
                )
        else:
            if h.is_down:
                await self.alerter.send(f"✅ Расшифровка голосовых ({escape(label)}) снова работает")
            h.failures, h.down_until, h.last_ok = 0, None, self.clock()

    async def check_queue(self) -> None:
        stats = await self.db.outbox_stats(self.outbox.handlers.keys())
        oldest = stats["oldest_failing_at"]
        stuck = oldest is not None and self.clock() - datetime.fromisoformat(oldest) > QUEUE_STUCK_AFTER
        if stuck and not self.queue_stuck:
            await self.alerter.send(
                f"⚠️ Очередь застряла: {stats['failing']} задач не проходят больше "
                f"{int(QUEUE_STUCK_AFTER.total_seconds() // 60)} мин ({escape(stats['failing_kind'] or '')}).\n"
                f"<code>{escape(stats['last_error'] or '')[:300]}</code>"
            )
        elif not stuck and self.queue_stuck:
            await self.alerter.send("✅ Очередь снова проходит")
        self.queue_stuck = stuck
        await self._check_unhandled(stats)

    async def _check_unhandled(self, stats: dict) -> None:
        """Задачи без обработчика не падают с ошибкой (attempts = 0) — их ловим по возрасту."""
        oldest = stats["oldest_unhandled_at"]
        unhandled = oldest is not None and self.clock() - datetime.fromisoformat(oldest) > QUEUE_STUCK_AFTER
        if unhandled and not self.queue_unhandled:
            await self.alerter.send(f"⚠️ {unhandled_line(stats)}")
        elif not unhandled and self.queue_unhandled:
            await self.alerter.send("✅ Задачи очереди снова есть кому выполнять")
        self.queue_unhandled = unhandled

    async def run_checks(self) -> None:
        while True:
            await asyncio.sleep(MODEL_CHECK_INTERVAL)
            for check in (self.check_models, self.check_queue):
                try:
                    await check()
                except Exception:
                    log.exception("Сторож: сбой проверки %s", check.__name__)

    # --- старт / остановка ---

    async def on_start(self) -> None:
        crashed = await self.db.kv_get(KV_RUNNING) == "1"
        await self.db.kv_set(KV_RUNNING, "1")
        if crashed:
            await self.alerter.send(f"✅ Бот снова работает после сбоя (версия {escape(revision())})")

    async def on_stop(self) -> None:
        await self.alerter.drain()
        await self.db.kv_set(KV_RUNNING, "0")

    # --- /status ---

    async def status_text(self) -> str:
        now = self.clock()
        report = await self.check()
        uptime = now - self.started_at
        lines = [
            "🩺 <b>Состояние бота</b>",
            f"Версия {escape(revision())}, работает {ago(self.started_at, now).removesuffix(' назад')}"
            if uptime.total_seconds() >= 1 else f"Версия {escape(revision())}",
            "",
            ("✅ Ядро в порядке" if report.ok else "⛔ " + escape("; ".join(report.problems))),
            f"Telegram: последний успешный опрос {ago(self.polling_ok, now)}",
        ]
        stats = await self.db.outbox_stats(self.outbox.handlers.keys())
        if stats["pending"] == 0:
            lines.append("Очередь: ✅ пусто")
        else:
            q = f"Очередь: ждут {stats['pending']}"
            if stats["failing"]:
                q = ("⚠️ " + q + f", с ошибками {stats['failing']} ({escape(stats['failing_kind'] or '')}): "
                     f"<code>{escape(stats['last_error'] or '')[:150]}</code>")
            lines.append(q)
            if stats["unhandled"]:
                lines.append(f"⚠️ {unhandled_line(stats)}")
        if self.llm:
            lines.append("")
            lines.append("<b>LLM</b> (по порядку, дальше — скрипт):")
            for p in self.llm.providers:
                h = self.llm.health[p.label]
                if h.is_down:
                    until = h.down_until.astimezone(self.settings.zone).strftime("%H:%M")
                    error = escape(redact(h.last_error or ""))[:150]  # /status видит вся группа — без секретов
                    lines.append(f"⛔ {escape(p.label)} — отключена до {until}: <code>{error}</code>")
                elif h.failures:
                    lines.append(f"⚠️ {escape(p.label)} — ошибок подряд: {h.failures}")
                else:
                    lines.append(f"✅ {escape(p.label)} — {llm_activity(h, now)}")
            used = await self.db.llm_calls_on(now.astimezone(self.settings.zone).date().isoformat())
            limit = self.settings.llm_calls_per_day
            lines.append(f"LLM за сегодня: {used} из {limit} обращений" if limit else f"LLM за сегодня: {used}")
        else:
            lines.append("LLM: не настроена — анкета по скрипту")
        if self.stt:
            states = [f"{escape(model_label(t))} {'⛔ недоступна' if self.stt_health[model_label(t)].is_down else '✅'}"
                      for t in self.stt]
            lines.append("Расшифровка голосовых: " + ", ".join(states))
            count, avg = compare_summary(await self.db.stt_pairs(now - STT_COMPARE_WINDOW))
            if count:
                lines.append(f"Сравнение расшифровок за {STT_COMPARE_WINDOW.days} дн.: {count} голосовых, "
                             f"совпадение ~{avg:.0%}")
        else:
            lines.append("Расшифровка голосовых: не настроена")
        today = await self.db.leads_today(self.settings.zone)
        lines.append("")
        lines.append(
            f"Заявок сегодня: {today['total']} (анкета заполнена: {today['qualified']}, взято: {today['taken']})"
        )
        return "\n".join(lines)


def unhandled_line(stats: dict) -> str:
    kinds = ", ".join(stats["unhandled_kinds"])
    return (
        f"В очереди {stats['unhandled']} задач, которые некому выполнить ({escape(kinds)}): канал не настроен — "
        "проверьте MANAGER_CHAT_ID, ключи Trello и Groq в env-файле"
    )
