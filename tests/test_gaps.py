"""Места, которые раньше не исполнялись ни одним тестом (по отчёту покрытия, сессия 002)."""

import asyncio
import json
import logging
from datetime import UTC, datetime, time

import httpx
import pytest
import respx
from aiogram import Bot

from app import redact
from app.config import Settings
from app.db import CARD_CREATE
from app.ops import alert
from app.services.health import Alerter, HealthMonitor
from app.services.llm import FormatError, LLMError, LLMProvider
from app.services.notifier import Notifier
from app.services.outbox import Outbox
from app.services.trello import TrelloClient
from tests.conftest import FakeSession, eventually

API = "https://api.trello.com/1"


# --- HTTP-клиент Trello: пути и методы (опечатку в пути раньше ловил только живой запуск) ---


@respx.mock
async def test_trello_client_paths():
    calls = {
        ("GET", "/boards/B/lists"): [],
        ("POST", "/boards/B/lists"): {"id": "L"},
        ("GET", "/boards/B/labels"): [],
        ("POST", "/boards/B/labels"): {"id": "LB"},
        ("POST", "/cards"): {"id": "C"},
        ("GET", "/cards/C"): {"id": "C"},
        ("PUT", "/cards/C"): {"id": "C"},
        ("POST", "/cards/C/actions/comments"): {"id": "A"},
        ("POST", "/cards/C/attachments"): {"id": "F"},
    }
    routes = {
        k: respx.request(k[0], API + k[1]).mock(return_value=httpx.Response(200, json=v)) for k, v in calls.items()
    }
    c = TrelloClient("K", "T")
    await c.lists("B")
    await c.create_list("B", "Новые")
    await c.labels("B")
    await c.create_label("B", "ночной", "purple")
    await c.create_card("L", "n", "d", [])
    await c.card("C")
    await c.update_card("C", idList="L2")
    await c.add_comment("C", "x" * 20000)
    await c.add_attachment("C", "голосовое_1.ogg", b"OGG", "audio/ogg")
    assert all(r.called for r in routes.values())
    comment = json.loads(routes[("POST", "/cards/C/actions/comments")].calls.last.request.read())
    assert len(comment["text"]) == 16384  # лимит Trello на комментарий
    upload = routes[("POST", "/cards/C/attachments")].calls.last.request
    assert b'filename="' in upload.read() and upload.url.params["key"] == "K"
    await c.close()


# --- LLM: бесплатная проверка /models и сетевые ошибки ---


@respx.mock
async def test_llm_check_available():
    route = respx.get("https://r.example/v1/models")
    p = LLMProvider("ds", "https://r.example/v1", "SECRET", "deepseek-v4.1-flash")
    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "deepseek-v4.1-flash"}]}))
    await p.check_available()
    route.mock(return_value=httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]}))
    with pytest.raises(LLMError, match="нет в списке"):
        await p.check_available()
    route.mock(return_value=httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError, match="401"):
        await p.check_available()
    route.mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(LLMError, match="ConnectError"):
        await p.check_available()
    route.mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(LLMError, match="неожиданный"):
        await p.check_available()
    await p.close()


@respx.mock
async def test_llm_chat_errors():
    route = respx.post("https://r.example/v1/chat/completions")
    p = LLMProvider("ds", "https://r.example/v1", "SECRET", "m")
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError, match="ReadTimeout") as e:
        await p.chat_json([])
    assert not isinstance(e.value, FormatError)  # сетевая ошибка — повод отключить модель
    route.mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMError, match="неожиданный ответ"):
        await p.chat_json([])
    route.mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "не json"}}]}))
    with pytest.raises(FormatError):
        await p.chat_json([])
    route.mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}))
    await p.ping()
    assert json.loads(route.calls.last.request.read())["max_tokens"] == 200  # запас для «рассуждающих» моделей
    await p.close()


# --- redact.install ---


def test_redact_install_registers_all_secrets(caplog):
    redact._secrets.clear()
    settings = Settings(bot_token="123456:BOT-SECRET", trello_api_key="trello-secret-key",
                        trello_token="trello-secret-token", groq_api_key="gsk_secret_value",
                        llm_primary_api_key="sk-secret-value")
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        redact.install(settings)
        redact.install(settings)  # повторный вызов не дублирует фильтр
        assert len([f for f in handler.filters if isinstance(f, redact.RedactingFilter)]) == 1
        assert redact.redact("x 123456:BOT-SECRET y sk-secret-value") == "x *** y ***"
        assert len(redact._secrets) == 5
    finally:
        root.removeHandler(handler)
        redact._secrets.clear()


# --- сторож: внешний healthcheck, ветки /status ---


async def monitor(db, **kw):
    settings = Settings(bot_token="123:TEST", manager_chat_id=-5000, work_start=time(0), work_end=time(23, 59, 59),
                        **kw)
    session = FakeSession()
    bot = Bot("123:TEST", session=session)
    notifier = Notifier(bot, db, settings, trello_enabled=False)
    outbox = Outbox(db, {})
    return HealthMonitor(bot, db, settings, outbox, notifier, Alerter(bot, settings, notifier)), session


@respx.mock
async def test_healthcheck_ping_only_when_healthy(db, monkeypatch):
    from app.services import systemd

    route = respx.get("https://hc.example/ping/abc").mock(return_value=httpx.Response(200))
    monkeypatch.setattr(systemd, "notify", lambda m: True)
    monkeypatch.setattr(systemd, "watchdog_interval", lambda: 0.01)
    m, _ = await monitor(db, healthcheck_url="https://hc.example/ping/abc")
    now = datetime.now(UTC)
    m.polling_attempt = m.outbox.last_run = m.notifier.last_scan = now
    m.clock = lambda: now
    task = asyncio.create_task(m.run_watchdog())
    await eventually(lambda: route.call_count >= 1)
    await asyncio.sleep(0.05)  # за это время тактов watchdog много — пинг всё равно один
    task.cancel()
    assert route.call_count == 1  # пинг раз в 5 минут, а не на каждый такт watchdog


async def test_status_without_llm_and_stt(db):
    m, _ = await monitor(db)
    text = await m.status_text()
    assert "LLM: не настроена — анкета по скрипту" in text and "Расшифровка голосовых: не настроена" in text


# --- очередь: цикл просыпается от новой задачи, а не ждёт таймера ---


async def test_outbox_loop_wakes_on_enqueue(db):
    done = asyncio.Event()

    async def handler(task):
        done.set()

    outbox = Outbox(db, {CARD_CREATE: handler}, poll_interval=60)
    loop = asyncio.create_task(outbox.run())
    await asyncio.sleep(0.01)
    await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    await asyncio.wait_for(done.wait(), 1)  # не 60 с
    loop.cancel()


# --- скрипт алерта: остальные ветки ---


@respx.mock
def test_alert_prefers_admin_and_survives_errors(tmp_path, monkeypatch, capsys):
    for k, v in {"BOT_TOKEN": "123:SECRET", "MANAGER_CHAT_ID": "-5000", "ADMIN_CHAT_ID": "777",
                 "DB_PATH": str(tmp_path / "none.sqlite3")}.items():
        monkeypatch.setenv(k, v)
    route = respx.post("https://api.telegram.org/bot123:SECRET/sendMessage").mock(
        return_value=httpx.Response(403, json={"ok": False})
    )
    assert alert.main(["alert", "stop"], {"SERVICE_RESULT": "oom-kill"}) == 0
    assert "chat_id=777" in route.calls.last.request.read().decode()
    assert "HTTP 403" in capsys.readouterr().err

    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("MANAGER_CHAT_ID", "")
    assert alert.main(["alert", "failed"], {}) == 0
    assert "нет ADMIN_CHAT_ID" in capsys.readouterr().err
