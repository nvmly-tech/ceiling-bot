"""Источник заявки: метка из ссылки t.me/<бот>?start=<метка> — в заявке, уведомлении, карточке и отчёте."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
from app.services.report import report_text
from tests.conftest import USER
from tests.test_notifier import Env, make_env
from tests.test_report import SETTINGS


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db)


async def finish(env: Env) -> None:
    await env.client.press("obj:flat")
    await env.client.text("18,5")
    await env.client.press("ct:matte")
    await env.client.contact("79001234567")
    await env.client.text("в субботу")
    await env.tick()


async def test_source_from_start_link(env: Env):
    await env.client.text("/start avito")
    lead = await env.db.last_lead(USER.id)
    assert lead.source == "avito"
    await finish(env)
    assert "📣 Источник: avito" in env.group()[0].text
    assert "**Источник:** avito" in env.trello.cards["C1"]["desc"]


async def test_no_source_without_link(env: Env):
    await env.client.text("/start")
    assert (await env.db.last_lead(USER.id)).source is None
    await finish(env)
    assert "Источник" not in env.group()[0].text and "Источник" not in env.trello.cards["C1"]["desc"]


@pytest.mark.parametrize(("text", "source"), [
    ("/start Avito_2026-spring", "avito_2026-spring"),  # приводим к нижнему регистру — чтобы считать вместе
    ("/start <b>x</b>", None),
    ("/start два слова", None),
    ("/start " + "a" * 65, None),
    ("/start " + "a" * 64, "a" * 64),
])
async def test_source_is_validated(env: Env, text: str, source: str | None):
    await env.client.text(text)
    assert (await env.db.last_lead(USER.id)).source == source


async def test_source_kept_for_new_lead_after_restart_choice(env: Env):
    await env.client.text("/start")
    await env.client.press("obj:flat")
    await env.client.text("/start vk")  # анкета не закончена — бот спросит: продолжить или новая
    assert (await env.db.last_lead(USER.id)).source is None
    await env.client.press("restart:new:1")
    lead = await env.db.last_lead(USER.id)
    assert (lead.id, lead.source) == (2, "vk")


async def test_continue_keeps_old_lead_source(env: Env):
    await env.client.text("/start avito")
    await env.client.text("/start vk")
    await env.client.press("restart:continue:1")
    lead = await env.db.last_lead(USER.id)
    assert (lead.id, lead.source) == (1, "avito")


async def test_sources_in_report(db: Database):
    for n, source in enumerate(["avito", "avito", "vk", None]):
        await db.create_lead(tg_user_id=200 + n, chat_id=200 + n, name="К", username=None, is_night=False,
                             source=source)
    until = datetime.now(UTC) + timedelta(minutes=1)
    since = until - timedelta(days=7)
    text = report_text(await db.leads_created_between(since, until), since, until, SETTINGS)
    assert "• источники: avito 2 · vk 1 · без метки 1" in text
