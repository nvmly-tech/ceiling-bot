"""Зависшие заявки: напоминания менеджеру, который взял заявку, и эскалация владельцу."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import User

from app import stages
from app.db import Database, now_iso
from app.schema import NUDGES_OFF
from tests.conftest import FakeSession
from tests.test_notifier import GROUP, Env, make_env
from tests.test_trello import complete_dialog

OWNER = -8000


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db, owner_chat_id=OWNER)


async def take(env: Env, user: User | None = None) -> datetime:
    await complete_dialog(env.client)
    await env.tick()
    await env.client.press_in_group(env.group()[0], "take:1", **({"user": user} if user else {}))
    await env.tick()
    return datetime.now(UTC)


def nudges(env: Env) -> list[SendMessage]:
    return [m for m in env.group() if m.text.startswith(("⏳", "📵", "📅", "🤔"))]


def owner(env: Env) -> list[SendMessage]:
    return env.session.sent(OWNER)


async def test_taken_without_outcome_nudges_manager_then_owner(env: Env):
    t0 = await take(env)
    await env.tick(t0 + timedelta(minutes=110))
    assert nudges(env) == []
    await env.tick(t0 + timedelta(minutes=125))
    [nudge] = nudges(env)
    assert "№1" in nudge.text and "без итога" in nudge.text
    assert 'href="tg://user?id=7"' in nudge.text  # упоминание менеджера, взявшего заявку
    assert [b.callback_data for row in nudge.reply_markup.inline_keyboard for b in row][0] == "st:1:measure"

    for minutes in (250, 380, 500):
        await env.tick(t0 + timedelta(minutes=minutes))
    assert len(nudges(env)) == 2  # не больше двух напоминаний на этап
    assert owner(env) == []

    await env.tick(t0 + timedelta(hours=24, minutes=5))
    [escalation] = owner(env)
    assert escalation.text.startswith("⚠️") and "№1" in escalation.text and "Иван Менеджеров" in escalation.text
    await env.tick(t0 + timedelta(hours=30))
    assert len(owner(env)) == 1  # владельцу — один раз на этап
    assert "nudge" in [m.kind for m in await env.db.tg_messages(1)]  # удалятся, если клиент удалит заявку


async def test_nudge_buttons_work_like_panel(env: Env):
    t0 = await take(env)
    await env.tick(t0 + timedelta(minutes=125))
    await env.client.press_in_group(nudges(env)[0], "st:1:no_answer")
    assert (await env.db.get_lead(1)).stage == stages.NO_ANSWER


async def test_stage_change_restarts_nudges(env: Env):
    t0 = await take(env)
    await env.tick(t0 + timedelta(minutes=125))
    await env.client.press_in_group(nudges(env)[0], "st:1:no_answer")
    lead = await env.db.get_lead(1)
    assert (lead.nudges_sent, lead.escalated_at) == (0, None)
    t1 = datetime.now(UTC)
    await env.tick(t1 + timedelta(minutes=125))
    last = nudges(env)[-1]
    assert last.text.startswith("📵") and "+79001234567" in last.text and "ещё раз" in last.text


async def test_measure_passed_asks_result(env: Env):
    await take(env)
    past = datetime.now(UTC) - timedelta(hours=3)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван Менеджеров", measure_at=now_iso(past))
    await env.tick()
    [nudge] = nudges(env)
    assert nudge.text.startswith("📅") and "чем закончился" in nudge.text
    assert "st:1:contract" in [b.callback_data for row in nudge.reply_markup.inline_keyboard for b in row]
    await env.tick(past + timedelta(hours=24, minutes=5))
    assert len(owner(env)) == 1


async def test_future_measure_not_nudged(env: Env):
    await take(env)
    soon = datetime.now(UTC) + timedelta(days=2)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван Менеджеров", measure_at=now_iso(soon))
    await env.tick(datetime.now(UTC) + timedelta(days=1))
    assert nudges(env) == [] and owner(env) == []


async def test_thinking_reminder_after_days(env: Env):
    await take(env)
    await env.db.set_stage(1, stages.THINKING, by_name="Иван Менеджеров")
    t1 = datetime.now(UTC)
    await env.tick(t1 + timedelta(days=2))
    assert nudges(env) == []
    await env.tick(t1 + timedelta(days=3, minutes=5))
    [nudge] = nudges(env)
    assert nudge.text.startswith("🤔") and "3 дн" in nudge.text
    await env.tick(t1 + timedelta(days=10))
    assert owner(env) == []  # «думает» — не повод жаловаться владельцу


async def test_final_stages_not_nudged(env: Env):
    await take(env)
    await env.db.set_stage(1, stages.CONTRACT, by_name="Иван Менеджеров")
    await env.tick(datetime.now(UTC) + timedelta(days=5))
    assert nudges(env) == [] and owner(env) == []


async def test_nudge_dropped_if_stage_changed_before_sending(env: Env):
    t0 = await take(env)
    await env.notifier.scan(t0 + timedelta(minutes=125))  # напоминание поставлено в очередь…
    await env.db.set_stage(1, stages.NO_ANSWER, by_name="Иван Менеджеров")  # …но итог отметили раньше отправки
    await env.outbox.run_once(t0 + timedelta(minutes=125))
    assert nudges(env) == []


async def test_untaken_lead_escalated_after_reminders(env: Env):
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)
    for minutes in (16, 31, 46, 55):
        await env.tick(t0 + timedelta(minutes=minutes))
    assert owner(env) == []
    await env.tick(t0 + timedelta(minutes=62))
    [escalation] = owner(env)
    assert "№1" in escalation.text and "никто не взял" in escalation.text
    await env.tick(t0 + timedelta(minutes=120))
    assert len(owner(env)) == 1


async def test_escalation_goes_to_group_without_owner_chat(db: Database):
    env = await make_env(db)
    t0 = await take(env)
    await env.tick(t0 + timedelta(hours=24, minutes=5))
    assert [m for m in env.session.sent(GROUP) if m.text.startswith("⚠️")]


async def test_mention_by_username(env: Env):
    t0 = await take(env, User(id=9, is_bot=False, first_name="Пётр", username="petr_m"))
    await env.tick(t0 + timedelta(minutes=125))
    assert "@petr_m" in nudges(env)[0].text


async def test_mention_dropped_if_telegram_rejects_it(env: Env, monkeypatch):
    t0 = await take(env)
    original = FakeSession.make_request

    async def picky(self, bot, method, timeout=None):
        if isinstance(method, SendMessage) and "tg://user" in (method.text or ""):
            raise TelegramBadRequest(method=method, message="Bad Request: can't parse entities")
        return await original(self, bot, method, timeout)

    monkeypatch.setattr(FakeSession, "make_request", picky)
    await env.tick(t0 + timedelta(minutes=125))
    [nudge] = nudges(env)
    assert "Иван Менеджеров" in nudge.text and "tg://user" not in nudge.text


async def test_old_leads_not_nudged_after_upgrade(db: Database):
    """Заявки, взятые до появления напоминаний, не должны разом получить их после обновления бота."""
    env = await make_env(db, owner_chat_id=OWNER)
    await take(env)
    for column in ("nudges_sent", "escalated_at"):
        await db.conn.execute(f"ALTER TABLE leads DROP COLUMN {column}")
    await db._migrate()
    lead = await db.get_lead(1)
    assert lead.nudges_sent == NUDGES_OFF and lead.escalated_at
    await env.tick(datetime.now(UTC) + timedelta(days=3))
    assert nudges(env) == [] and owner(env) == []

    await env.client.press_in_group(env.group()[0], "st:1:no_answer")  # новый этап — напоминания снова работают
    await env.tick(datetime.now(UTC) + timedelta(minutes=125))
    assert len(nudges(env)) == 1
