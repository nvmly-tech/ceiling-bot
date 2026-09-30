"""Сборка бота (build_app): ошибка в связке компонентов иначе всплыла бы только при запуске на сервере."""

import logging
from datetime import time

from aiogram import Bot

from app import db as dbmod
from app.bot import texts
from app.config import Settings
from app.main import build_app, build_llm
from tests.conftest import Client, FakeSession, eventually

GROUP = -5000
FULL = dict(
    bot_token="123:TEST", manager_chat_id=GROUP, work_start=time(0), work_end=time(23, 59, 59),
    trello_api_key="trello-key", trello_token="trello-token", trello_board_id="board",
    groq_api_key="gsk_test_key", gigaam_socket="/run/ceiling-bot-stt.sock", llm_primary_base_url="https://router.example/v1",
    llm_primary_api_key="sk-test-key", llm_primary_model="deepseek-v4.1-flash",
)
ALL_KINDS = {v for k, v in vars(dbmod).items() if k.isupper() and isinstance(v, str) and "." in v
             and v.split(".")[0] in ("trello", "tg", "stt", "shadow")}


async def make(db, **settings):
    session = FakeSession()
    app = await build_app(Settings(**settings), Bot("123:TEST", session=session), db)
    return app, session


async def test_full_configuration_is_wired(db):
    app, session = await make(db, **FULL)

    assert app.trello and app.stt and app.llm
    assert [t.label for t in app.stt.transcribers] == ["GigaAM", "Groq"] and app.stt.shadow
    labels = [p.label for p in app.llm.providers]
    assert labels == ["deepseek (router.cheap)", "groq: openai/gpt-oss-120b"]
    assert app.llm.providers[0].extra == {} and app.llm.providers[1].extra == {"reasoning_effort": "low"}
    assert app.llm.on_status_change is not None  # алерты сторожа о моделях подключены

    # У каждого вида задач очереди есть обработчик — иначе задачи молча копились бы вечно.
    assert set(app.outbox.handlers) == ALL_KINDS and len(ALL_KINDS) == 16

    # Сторож видит опрос Telegram через middleware сессии.
    assert app.monitor.polling_attempt is None
    await app.bot.get_updates()
    assert app.monitor.polling_attempt is not None

    # Диспетчер получил все зависимости: /status в группе менеджеров отвечает.
    await Client(app.dp, app.bot, session).group_text("/status")
    assert "Состояние бота" in session.sent(GROUP)[-1].text
    await app.close()


async def test_minimal_configuration(db, caplog):
    caplog.set_level(logging.WARNING)
    app, session = await make(db, bot_token="123:TEST")
    assert (app.trello, app.stt, app.llm) == (None, None, None)
    assert app.outbox.handlers == {}  # задачи копятся до появления настроек
    for part in ("Trello не настроен", "Расшифровка не настроена", "Ни одна LLM не настроена",
                 "MANAGER_CHAT_ID не задан"):
        assert part in caplog.text
    # Анкета по скрипту работает и без интеграций.
    client = Client(app.dp, app.bot, session)
    await client.text("/start")
    assert client.last_text() == texts.Q_OBJECT
    await app.close()


def test_llm_variants():
    only_groq = build_llm(Settings(bot_token="1:x", groq_api_key="gsk_x_key", llm_fallback_model="qwen/qwen3.8-27b"))
    assert [(p.label, p.extra) for p in only_groq.providers] == [("groq: qwen/qwen3.8-27b", {})]
    no_model = build_llm(Settings(bot_token="1:x", llm_primary_base_url="https://r/v1", llm_primary_api_key="sk-x-key"))
    assert no_model is None  # без имени модели основная не подключается, Groq не настроен


async def test_background_tasks_start_and_stop(db):
    app, _ = await make(db, **FULL)
    tasks = app.start_tasks()
    assert [t.get_name() for t in tasks] == ["outbox", "notifier", "watchdog", "checks"]
    await eventually(lambda: app.notifier.last_scan is not None and app.outbox.last_run is not None)
    await app.close(tasks)
    assert all(t.done() for t in tasks)


def test_bad_env_values_fail_at_startup():
    """Опечатка в .env должна ронять бот при запуске (systemd пришлёт алерт), а не на первом сообщении клиента."""
    import pytest
    from pydantic import ValidationError

    for bad in ({}, {"work_start": "9 утра"}, {"manager_chat_id": "группа"}, {"llm_timeout_sec": "долго"},
                {"studio_tz": "Moscow"}):
        kw = {"bot_token": "1:x", **bad} if bad else {}
        with pytest.raises(ValidationError):
            Settings(**kw)
    assert Settings(bot_token="1:x", studio_tz="Asia/Yekaterinburg").zone.key == "Asia/Yekaterinburg"


async def test_facts_file_from_settings(db, tmp_path):
    facts = tmp_path / "facts.md"
    facts.write_text("- Матовый — от 610 ₽/м².", encoding="utf-8")
    app, _ = await make(db, **FULL, facts_path=str(facts))
    assert app.dp["assistant"].facts.path == facts and app.dp["assistant"].facts.allowed_amounts == {610}
    await app.close()
