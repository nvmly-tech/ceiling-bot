"""Повторные клиенты: прошлые заявки того же человека (по Telegram или телефону) — в уведомлении, карточке, отчёте."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app import stages
from app.bot import texts
from app.db import Database
from tests.conftest import USER
from tests.test_notifier import Env, make_env
from tests.test_trello import complete_dialog

ZONE = ZoneInfo("Europe/Moscow")


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db)


def notification(env: Env, lead_id: int):
    return next(m for m in env.group() if f"Новая заявка №{lead_id}" in m.text)


async def other_client(db: Database, *, phone: str | None = "+79001234567", **fields) -> int:
    """Заявка с другого Telegram-аккаунта."""
    lead = await db.create_lead(tg_user_id=555, chat_id=555, name="Другой", username=None, is_night=False)
    await db.update_lead(lead.id, **{"phone": phone, "status": "qualified", **fields})
    return lead.id


def test_history_text():
    from app.db import Lead

    base = dict(id=5, tg_user_id=1, chat_id=1, name="А", username=None, object=None, area_m2=None, area_text=None,
                ceiling_type=None, phone=None, measure_time=None, is_night=False, trello_card_id=None,
                created_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC).isoformat(), updated_at="", completed_at=None)
    taken = Lead(**base, status="qualified", taken_at="2026-09-12T10:05:00+00:00", taken_by_name="Иван",
                 stage=stages.CONTRACT)
    assert stages.history_text(taken, ZONE) == "№5 от 12.09.26 — ✅ договор (Иван)"
    assert stages.history_text(Lead(**base, status="qualified"), ZONE) == "№5 от 12.09.26 — в работу не взята"
    assert stages.history_text(Lead(**base, status="abandoned"), ZONE) == "№5 от 12.09.26 — анкета не завершена"


async def test_first_lead_has_no_history(env: Env):
    await complete_dialog(env.client)
    await env.tick()
    assert "🔁" not in notification(env, 1).text and "Уже обращался" not in env.trello.cards["C1"]["desc"]


async def test_repeat_by_telegram_account(env: Env):
    await complete_dialog(env.client)
    await env.tick()
    await env.client.press_in_group(env.group()[0], "take:1")
    await env.db.set_stage(1, stages.CONTRACT, by_name="Иван Менеджеров")

    await complete_dialog(env.client)  # тот же клиент, новая заявка
    await env.tick()
    note = notification(env, 2)
    assert "🔁 <b>Уже обращался:</b>" in note.text
    assert "№1 от" in note.text and "договор" in note.text and "Иван Менеджеров" in note.text
    desc = env.trello.cards["C2"]["desc"]
    assert "**Уже обращался:**" in desc and "№1 от" in desc


async def test_repeat_by_phone_from_another_account(env: Env):
    await other_client(env.db)
    await complete_dialog(env.client)  # тот же номер +79001234567
    await env.tick()
    assert "№1 от" in notification(env, 2).text and "в работу не взята" in notification(env, 2).text


async def test_deleted_and_cancelled_leads_are_not_history(env: Env):
    first = await other_client(env.db)
    await env.db.delete_lead_data(first)
    await other_client(env.db, status="cancelled")
    await complete_dialog(env.client)
    await env.tick()
    assert "🔁" not in notification(env, 3).text


async def test_missing_phone_does_not_link_strangers(env: Env):
    await other_client(env.db, phone=texts.NO_PHONE_VALUE)
    await env.client.text("/start")
    await env.client.press("obj:flat")
    await env.client.text("20")
    await env.client.press("ct:matte")
    await env.client.text(texts.NO_PHONE)
    await env.client.text("в субботу")
    await env.tick()
    assert (await env.db.last_lead(USER.id)).phone == texts.NO_PHONE_VALUE
    assert "🔁" not in notification(env, 2).text


async def test_only_recent_history_shown(env: Env):
    for _ in range(5):
        await other_client(env.db)
    await complete_dialog(env.client)
    await env.tick()
    text = notification(env, 6).text
    assert [f"№{i} от" in text for i in (1, 2, 3, 4, 5)] == [False, False, True, True, True]
    assert "и ещё 2" in text


async def test_repeats_in_report(env: Env):
    await other_client(env.db)
    await complete_dialog(env.client)
    await env.client.group_text("/report")
    assert "🔁 повторных: 1" in env.group()[-1].text
