import asyncio
import gzip
import socket
import sqlite3
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
import respx
from aiogram import Bot
from aiogram.methods import GetUpdates, SendMessage

from app.config import Settings
from app.db import ALL_KINDS
from app.main import build_dispatcher
from app.ops import alert
from app.services import health, systemd
from app.services.health import KV_RUNNING, Alerter, HealthMonitor
from app.services.llm import FAIL_THRESHOLD, LLMError, LLMRouter
from app.services.notifier import Notifier
from app.services.outbox import Outbox
from deploy.backup import backup
from tests.conftest import Client, FakeSession
from tests.test_llm import FakeProvider

GROUP = -5000
ADMIN = -7000


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def tick(self, **kw):
        self.now += timedelta(**kw)


class CheckedProvider(FakeProvider):
    """Модель для сторожа: считает бесплатные проверки (/models) и платные (chat)."""

    def __init__(self, label):
        super().__init__(label)
        self.available_ok = True
        self.ping_ok = True
        self.free_checks = 0
        self.paid_pings = 0

    async def check_available(self):
        self.free_checks += 1
        if not self.available_ok:
            raise LLMError(f"{self.label}: HTTP 503")

    async def ping(self):
        self.paid_pings += 1
        if not self.ping_ok:
            raise LLMError(f"{self.label}: HTTP 503")


class FakeStt:
    def __init__(self):
        self.ok = True

    async def ping(self):
        if not self.ok:
            raise RuntimeError("Groq: HTTP 503")


async def noop(task):
    pass


ALL_HANDLED = {kind: noop for kind in ALL_KINDS}


async def make_monitor(db, *, providers=(), stt=None, admin=None, handlers=None):
    clock = Clock()
    settings = Settings(bot_token="123:TEST", manager_chat_id=GROUP, admin_chat_id=admin,
                        work_start=time(0), work_end=time(23, 59, 59))
    session = FakeSession()
    bot = Bot("123:TEST", session=session)
    notifier = Notifier(bot, db, settings, trello_enabled=False)
    outbox = Outbox(db, ALL_HANDLED if handlers is None else handlers)
    router = LLMRouter(list(providers)) if providers else None
    monitor = HealthMonitor(bot, db, settings, outbox, notifier, Alerter(bot, settings, notifier),
                            llm=router, stt=stt, clock=clock)
    return monitor, clock, session, router


def alerts(session, chat=GROUP):
    return [m.text for m in session.sent(chat)]


async def settle(monitor):
    await monitor.alerter.drain()  # алерты из синхронного колбэка маршрутизатора уходят фоновыми задачами


# --- systemd ---


def test_notify_writes_to_socket(tmp_path, monkeypatch):
    path = tmp_path / "notify"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    assert systemd.notify("WATCHDOG=1")
    assert server.recv(100) == b"WATCHDOG=1"
    server.close()
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert not systemd.notify("READY=1")


def test_watchdog_interval(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert systemd.watchdog_interval() is None
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")
    assert systemd.watchdog_interval() == 20
    monkeypatch.setenv("WATCHDOG_PID", "1")  # сигнал предназначен другому процессу
    assert systemd.watchdog_interval() is None


# --- ядро ---


async def test_core_check(db):
    monitor, clock, _, _ = await make_monitor(db)
    assert (await monitor.check()).ok  # сразу после старта — льготный период

    clock.tick(seconds=130)
    problems = (await monitor.check()).problems
    assert problems == ["опрос Telegram не начался", "очередь не запустилась", "планировщик не запустилась"]

    monitor.polling_attempt = clock.now
    monitor.outbox.last_run = monitor.notifier.last_scan = clock.now
    assert (await monitor.check()).ok

    clock.tick(seconds=200)
    assert (await monitor.check()).problems == ["опрос Telegram завис (3 мин назад)"]
    clock.tick(seconds=120)
    assert len((await monitor.check()).problems) == 3


async def test_db_failure_is_a_problem(db):
    monitor, _, _, _ = await make_monitor(db)
    await db.close()
    assert any("база не пишется" in p for p in (await monitor.check()).problems)
    await db.connect()


async def test_session_middleware_tracks_polling(db):
    monitor, clock, _, _ = await make_monitor(db)

    async def ok(bot, method):
        return []

    async def fail(bot, method):
        raise RuntimeError("network")

    await monitor.session_middleware(ok, None, SendMessage(chat_id=1, text="x"))
    assert monitor.polling_attempt is None  # другие методы не считаются
    await monitor.session_middleware(ok, None, GetUpdates())
    assert monitor.polling_attempt == monitor.polling_ok == clock.now
    clock.tick(seconds=30)
    with pytest.raises(RuntimeError):
        await monitor.session_middleware(fail, None, GetUpdates())
    # Сеть лежит, но цикл опроса крутится — это не повод перезапускать бота.
    assert monitor.polling_attempt == clock.now and monitor.polling_ok == clock.now - timedelta(seconds=30)


async def test_watchdog_pings_only_while_healthy(db, monkeypatch):
    sent = []
    monkeypatch.setattr(systemd, "notify", lambda m: sent.append(m) or True)
    monkeypatch.setattr(systemd, "watchdog_interval", lambda: 0.01)
    monitor, clock, _, _ = await make_monitor(db)
    monitor.polling_attempt = monitor.outbox.last_run = monitor.notifier.last_scan = clock.now
    task = asyncio.create_task(monitor.run_watchdog())
    await asyncio.sleep(0.05)
    assert sent[0] == "READY=1" and "WATCHDOG=1" in sent

    sent.clear()
    clock.tick(seconds=400)  # всё зависло
    await asyncio.sleep(0.05)
    task.cancel()
    assert "WATCHDOG=1" not in sent and any(m.startswith("STATUS=нездоров") for m in sent)


# --- модели ---


async def test_model_checks_are_free_and_skip_recent(db):
    ds, groq = CheckedProvider("deepseek"), CheckedProvider("groq")
    monitor, clock, _, router = await make_monitor(db, providers=[ds, groq])
    router.record_ok("deepseek", clock.now)  # только что отвечала клиенту
    router.health["groq"].failures = 1       # ошибка живого запроса
    await monitor.check_models()
    assert (ds.free_checks, ds.paid_pings) == (0, 0)  # живой трафик уже всё показал
    assert (groq.free_checks, groq.paid_pings) == (1, 0)
    assert router.health["groq"].failures == 1  # /models не маскирует ошибки генерации


async def test_model_down_and_recovery_alerts(db):
    ds, groq = CheckedProvider("deepseek"), CheckedProvider("groq")
    monitor, clock, session, router = await make_monitor(db, providers=[ds, groq])
    ds.available_ok = False
    for _ in range(FAIL_THRESHOLD):
        await monitor.check_models()
        clock.tick(minutes=5)
    await settle(monitor)
    [down] = alerts(session)
    assert "Модель <b>deepseek</b> недоступна" in down and "HTTP 503" in down

    await monitor.check_models()  # отключённую проверяем настоящим запросом
    assert ds.paid_pings == 1
    await settle(monitor)
    assert alerts(session)[-1] == "✅ Модель <b>deepseek</b> снова работает"
    assert not router.health["deepseek"].is_down


async def test_all_models_down_alert(db):
    ds, groq = CheckedProvider("deepseek"), CheckedProvider("groq")
    monitor, clock, session, _ = await make_monitor(db, providers=[ds, groq], admin=777)
    ds.available_ok = groq.available_ok = False
    for _ in range(FAIL_THRESHOLD):
        await monitor.check_models()
    await settle(monitor)
    texts = alerts(session, 777)  # задан ADMIN_CHAT_ID — алерты туда, а не менеджерам
    assert len(texts) == 2 and "Недоступны все модели — клиентам отвечает скрипт" in texts[-1]
    assert alerts(session) == []


async def test_stt_alerts(db):
    stt = FakeStt()
    monitor, _, session, _ = await make_monitor(db, stt=stt)
    stt.ok = False
    await monitor.check_models()
    assert alerts(session) == []  # одна неудача — ещё не повод
    await monitor.check_models()
    await monitor.check_models()
    assert len(alerts(session)) == 1 and "Расшифровка голосовых (Groq) недоступна" in alerts(session)[0]
    stt.ok = True
    await monitor.check_models()
    assert alerts(session)[-1] == "✅ Расшифровка голосовых (Groq) снова работает"


# --- очередь ---


async def test_queue_stuck_alert(db):
    monitor, clock, session, _ = await make_monitor(db)
    clock.now = datetime.now(UTC)
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    [task] = await db.outbox_pending()
    await db.outbox_retry(task.id, "Trello: HTTP 503", clock.now.isoformat())
    await monitor.check_queue()
    assert alerts(session) == []

    clock.tick(minutes=31)
    await monitor.check_queue()
    await monitor.check_queue()
    [stuck] = alerts(session)
    assert "Очередь застряла: 1 задач" in stuck and "trello.card_create" in stuck and "HTTP 503" in stuck

    await db.outbox_done(task.id)
    await monitor.check_queue()
    assert alerts(session)[-1] == "✅ Очередь снова проходит"
    assert lead.id == 1


async def test_queue_alert_for_tasks_without_handler(db):
    # Канал не настроен (например, нет MANAGER_CHAT_ID) — его задачи некому выполнить. Ошибок у них нет
    # (attempts = 0), но сторож всё равно должен об этом сказать.
    monitor, clock, session, _ = await make_monitor(db, admin=ADMIN, handlers={})
    clock.now = datetime.now(UTC)
    await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    await monitor.check_queue()
    assert alerts(session, ADMIN) == []

    clock.tick(minutes=31)
    await monitor.check_queue()
    await monitor.check_queue()
    [alert] = alerts(session, ADMIN)
    assert "некому выполнить" in alert and "trello.card_create" in alert
    assert "некому выполнить" in await monitor.status_text()

    monitor.outbox.handlers = ALL_HANDLED
    await monitor.check_queue()
    assert alerts(session, ADMIN)[-1] == "✅ Задачи очереди снова есть кому выполнять"


# --- перезапуск после сбоя ---


async def test_restart_after_crash_is_reported(db):
    monitor, _, session, _ = await make_monitor(db)
    await monitor.on_start()
    assert alerts(session) == []  # первый запуск
    # Процесс умер, не дойдя до on_stop, — следующий старт сообщает о сбое.
    await monitor.on_start()
    assert alerts(session)[0].startswith("✅ Бот снова работает после сбоя")
    await monitor.on_stop()
    assert await db.kv_get(KV_RUNNING) == "0"
    await monitor.on_start()
    assert len(alerts(session)) == 1  # после штатной остановки — тишина


# --- /status ---


async def test_status_command(db):
    ds, groq = CheckedProvider("deepseek (router.cheap)"), CheckedProvider("groq: gpt-oss")
    monitor, clock, session, router = await make_monitor(db, providers=[ds, groq], stt=FakeStt())
    monitor.clock = lambda: datetime.now(UTC)
    monitor.started_at = datetime.now(UTC) - timedelta(minutes=5)
    monitor.polling_attempt = monitor.polling_ok = monitor.outbox.last_run = monitor.notifier.last_scan = \
        datetime.now(UTC)
    router.record_ok("deepseek (router.cheap)")
    for _ in range(FAIL_THRESHOLD):
        router.record_fail("groq: gpt-oss", "groq: HTTP 429 rate limit")
    await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)

    dp = build_dispatcher(db, monitor.settings, monitor.notifier, None, None, monitor)
    client = Client(dp, monitor.bot, session)
    await client.group_text("/status")
    text = session.sent(GROUP)[-1].text
    assert "🩺 <b>Состояние бота</b>" in text and "✅ Ядро в порядке" in text
    assert "✅ deepseek (router.cheap) — ответ" in text
    assert "⛔ groq: gpt-oss — отключена до" in text and "HTTP 429" in text
    assert "Заявок сегодня: 1" in text and "Очередь: ждут 1" in text

    n = len(session.sent())
    await client.text("/status")  # клиент в личке — статус не показываем
    assert all("Состояние бота" not in m.text for m in session.sent()[n:])


# --- алерт из systemd ---


def test_alert_messages():
    assert alert.message("stop", {"SERVICE_RESULT": "success"}) is None  # штатная остановка
    watchdog = alert.message("stop", {"SERVICE_RESULT": "watchdog"})
    assert "завис" in watchdog and "перезапустит" in watchdog
    assert "код 1" in alert.message("stop", {"SERVICE_RESULT": "exit-code", "EXIT_STATUS": "1"})
    assert "сигналом KILL" in alert.message("stop", {"SERVICE_RESULT": "signal", "EXIT_STATUS": "KILL"})
    assert "больше не перезапускает" in alert.message("failed", {})
    assert "бэкап" in alert.message("backup", {})


@respx.mock
def test_alert_sends_to_migrated_group(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO kv VALUES ('manager_chat_id', '-100777')")
    for key, value in {"BOT_TOKEN": "123:SECRET", "MANAGER_CHAT_ID": "-5000", "DB_PATH": str(db_path),
                       "ADMIN_CHAT_ID": ""}.items():
        monkeypatch.setenv(key, value)
    route = respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert alert.main(["alert", "stop"], {"SERVICE_RESULT": "watchdog"}) == 0
    assert "chat_id=-100777" in route.calls.last.request.read().decode()

    assert alert.main(["alert", "stop"], {"SERVICE_RESULT": "success"}) == 0
    assert route.call_count == 1

    route.mock(side_effect=httpx.ConnectError("boom"))
    assert alert.main(["alert", "failed"], {}) == 0  # сбой алерта не роняет systemd


# --- бэкап ---


def test_backup_consistent_and_rotated(tmp_path):
    db_path = tmp_path / "bot.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO leads (name) VALUES ('Анна')")
    conn.commit()  # соединение открыто — данные в WAL, как у работающего бота
    dest = tmp_path / "backups"
    for day in (1, 2, 3):
        path = backup(db_path, dest, keep=2, now=datetime(2026, 9, day, 3, 30))
    conn.close()
    assert sorted(p.name for p in dest.iterdir()) == ["ceiling-bot-20260902-0330.sqlite3.gz",
                                                      "ceiling-bot-20260903-0330.sqlite3.gz"]
    assert oct(path.stat().st_mode)[-3:] == "600" and oct(dest.stat().st_mode)[-3:] == "700"
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(gzip.decompress(path.read_bytes()))
    with sqlite3.connect(restored) as r:
        assert r.execute("SELECT name FROM leads").fetchall() == [("Анна",)]


def test_revision_file(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "REVISION_FILE", tmp_path / "REVISION")
    assert health.revision() == "dev"
    (tmp_path / "REVISION").write_text("abc123\n")
    assert health.revision() == "abc123"


def test_backup_refuses_missing_db(tmp_path):
    # Нет файла базы — ошибка, а не «успешная» копия пустой базы, которая вытеснит настоящие.
    dest = tmp_path / "backups"
    with pytest.raises(sqlite3.OperationalError):
        backup(tmp_path / "missing.sqlite3", dest)
    assert not (tmp_path / "missing.sqlite3").exists()
    assert not list(dest.glob("*.gz"))


def test_backup_refuses_db_without_leads(tmp_path):
    db_path = tmp_path / "bot.sqlite3"
    sqlite3.connect(db_path).close()  # пустая база: таблиц бота нет
    dest = tmp_path / "backups"
    with pytest.raises(RuntimeError, match="leads"):
        backup(db_path, dest)
    assert not list(dest.glob("*.gz"))
