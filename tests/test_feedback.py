"""Обратная связь от клиента: «С вами связался менеджер?» и оценка замера."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import SendMessage

from app import stages
from app.bot import texts
from app.db import Database, now_iso
from tests.conftest import CHAT
from tests.test_notifier import GROUP, Env, make_env
from tests.test_trello import complete_dialog

OWNER = -8000


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db, owner_chat_id=OWNER)


async def take(env: Env) -> datetime:
    await complete_dialog(env.client)
    await env.tick()
    await env.client.press_in_group(env.group()[0], "take:1")
    await env.tick()
    return datetime.now(UTC)


def client_msgs(env: Env) -> list[SendMessage]:
    return env.session.sent(CHAT.id)


def buttons(msg: SendMessage) -> list[str]:
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row] if msg.reply_markup else []


# --- «С вами связался менеджер?» ---


async def test_client_asked_if_nobody_called(env: Env):
    t0 = await take(env)
    n = len(client_msgs(env))
    await env.tick(t0 + timedelta(minutes=170))
    assert len(client_msgs(env)) == n
    await env.tick(t0 + timedelta(minutes=185))
    ask = client_msgs(env)[-1]
    assert "№1" in ask.text and "связался" in ask.text
    assert buttons(ask) == ["contact:1:yes", "contact:1:no"]
    await env.tick(t0 + timedelta(hours=10))
    assert len(client_msgs(env)) == n + 1  # один раз на заявку


async def test_no_question_when_outcome_marked(env: Env):
    t0 = await take(env)
    await env.client.press_in_group(env.group()[0], "st:1:no_answer")  # менеджер уже звонил
    n = len(client_msgs(env))
    await env.tick(t0 + timedelta(hours=4))
    assert len(client_msgs(env)) == n


async def test_client_says_nobody_called(env: Env):
    t0 = await take(env)
    await env.tick(t0 + timedelta(minutes=185))
    await env.client.press("contact:1:no")
    assert env.client.last_text() == texts.CONTACT_NO_REPLY
    await env.tick(t0 + timedelta(minutes=186))
    [alert] = [m for m in env.session.sent(GROUP) if m.text.startswith("❗")]
    assert "ещё не связались" in alert.text and 'href="tg://user?id=7"' in alert.text
    assert "st:1:measure" in buttons(alert)
    [owner] = env.session.sent(OWNER)
    assert "№1" in owner.text and "ещё не связались" in owner.text
    # Владельцу — не просьба «позвоните» с кнопками, а кто взял заявку и когда.
    assert "Заявку взял(а) Иван Менеджеров" in owner.text and "👇" not in owner.text
    lead = await env.db.get_lead(1)
    assert lead.contact_answer == "no"
    assert (await env.db.get_messages(1, direction="in"))[-1].kind == "contact"


async def test_client_says_manager_called(env: Env):
    t0 = await take(env)
    await env.tick(t0 + timedelta(minutes=185))
    await env.client.press("contact:1:yes")
    assert env.client.last_text() == texts.CONTACT_YES_REPLY
    await env.tick(t0 + timedelta(minutes=186))
    note = env.group()[-1]
    assert "отметьте итог" in note.text and "st:1:no_answer" in buttons(note)
    assert env.session.sent(OWNER) == []
    assert (await env.db.get_lead(1)).contact_answer == "yes"


async def test_contact_answer_accepted_once_and_only_from_client(env: Env):
    t0 = await take(env)
    await env.tick(t0 + timedelta(minutes=185))
    await env.client.press("contact:1:no")
    await env.client.press("contact:1:yes")  # второе нажатие (кнопки уже убраны, но сообщение могло остаться)
    assert (await env.db.get_lead(1)).contact_answer == "no"
    await env.db.conn.execute("UPDATE leads SET tg_user_id = 777, contact_answer = NULL WHERE id = 1")
    await env.db.conn.commit()
    for data in ("contact:1:yes", "contact:x:yes", "contact:1:maybe", "contact:1"):
        await env.client.press(data)
    assert (await env.db.get_lead(1)).contact_answer is None


async def test_old_leads_not_asked_after_upgrade(env: Env):
    t0 = await take(env)
    for column in ("contact_asked_at", "rating_asked_at"):
        await env.db.conn.execute(f"ALTER TABLE leads DROP COLUMN {column}")
    await env.db._migrate()
    n = len(client_msgs(env))
    await env.tick(t0 + timedelta(hours=5))
    assert len(client_msgs(env)) == n


# --- оценка замера ---


async def measured(env: Env, hours_ago: float = 4, stage: str = stages.MEASURE) -> datetime:
    await take(env)
    at = datetime.now(UTC) - timedelta(hours=hours_ago)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван Менеджеров", measure_at=now_iso(at))
    if stage != stages.MEASURE:
        await env.db.set_stage(1, stage, by_name="Иван Менеджеров")
    return at


async def test_rating_asked_after_measure(env: Env):
    at = await measured(env, hours_ago=2)
    await env.tick()
    assert not [m for m in client_msgs(env) if "Оцените" in m.text]
    await env.tick(at + timedelta(hours=3, minutes=5))
    ask = client_msgs(env)[-1]
    assert "Оцените" in ask.text and "№1" in ask.text
    assert buttons(ask) == [f"rate:1:{i}" for i in range(1, 6)]
    await env.tick(at + timedelta(hours=8))
    assert len([m for m in client_msgs(env) if "Оцените" in m.text]) == 1


@pytest.mark.parametrize("stage", [stages.THINKING, stages.CONTRACT])
async def test_rating_asked_for_later_stages(env: Env, stage: str):
    await measured(env, stage=stage)
    await env.tick()
    assert "Оцените" in client_msgs(env)[-1].text


async def test_no_rating_if_refused_before_measure(env: Env):
    await take(env)
    soon = datetime.now(UTC) + timedelta(hours=1)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=now_iso(soon))
    await env.db.set_stage(1, stages.REFUSED, by_name="Иван", reason="changed")  # отменили до замера
    await env.tick(soon + timedelta(hours=5))
    assert not [m for m in client_msgs(env) if "Оцените" in m.text]


async def test_good_rating_to_group_only(env: Env):
    await measured(env)
    await env.tick()
    await env.client.press("rate:1:5")
    assert env.client.last_text() == texts.RATE_REPLY
    await env.tick()
    note = env.group()[-1]
    assert "5 из 5" in note.text and "№1" in note.text
    assert env.session.sent(OWNER) == []
    lead = await env.db.get_lead(1)
    assert lead.rating == 5
    await env.tick()
    assert "Оценка замера" in env.trello.cards["C1"]["desc"] and "5 из 5" in env.trello.cards["C1"]["desc"]


async def test_low_rating_also_to_owner(env: Env):
    await measured(env)
    await env.tick()
    await env.client.press("rate:1:2")
    await env.tick()
    [owner] = env.session.sent(OWNER)
    assert "2 из 5" in owner.text and "Иван Менеджеров" in owner.text


async def test_rating_accepted_once_and_validated(env: Env):
    await measured(env)
    await env.tick()
    for data in ("rate:1:9", "rate:1:x", "rate:1"):
        await env.client.press(data)
    assert (await env.db.get_lead(1)).rating is None
    await env.client.press("rate:1:4")
    await env.client.press("rate:1:1")
    assert (await env.db.get_lead(1)).rating == 4
    await env.tick()
    assert not [m for m in env.group() if "дописал" in m.text]  # ответы — не «клиент дописал»


# --- вопросы не к месту ---


async def test_no_contact_question_after_reopen(env: Env):
    """Заявку вернули в работу после итога — с клиентом уже общались, «связались ли с вами?» неуместно."""
    t0 = await take(env)
    await env.db.set_stage(1, stages.REFUSED, by_name="Иван", reason="other")
    await env.db.set_stage(1, None, by_name="Иван")
    await env.tick(t0 + timedelta(hours=5))
    assert not [m for m in client_msgs(env) if "связался" in m.text]


@pytest.mark.parametrize("answer", ["cancel", "move"])
async def test_no_rating_when_client_cancelled_or_moved_visit(env: Env, answer: str):
    """Клиент отменил замер кнопкой, а менеджер этап не обновил — оценивать нечего."""
    await take(env)
    at = datetime.now(UTC) + timedelta(days=1)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=now_iso(at))
    await env.tick()
    await env.client.press(f"visit:1:{answer}:{stages.stamp(now_iso(at))}")
    await env.tick(at + timedelta(hours=4))
    assert not [m for m in client_msgs(env) if "Оцените" in m.text]


async def test_rating_asked_when_client_confirmed_visit(env: Env):
    await take(env)
    at = datetime.now(UTC) + timedelta(days=1)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=now_iso(at))
    await env.tick()
    await env.client.press(f"visit:1:yes:{stages.stamp(now_iso(at))}")
    await env.tick(at + timedelta(hours=4))
    assert "Оцените" in client_msgs(env)[-1].text


async def test_panel_shows_client_answer_about_visit(env: Env):
    from app.services.notifier import panel_text

    await take(env)
    at = datetime.now(UTC) + timedelta(days=1)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=now_iso(at))
    await env.tick()
    await env.client.press(f"visit:1:cancel:{stages.stamp(now_iso(at))}")
    zone = env.notifier.settings.zone
    assert "Клиент: ❌ отменил замер" in panel_text(await env.db.get_lead(1), zone)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=now_iso(at + timedelta(days=1)))
    assert "Клиент:" not in panel_text(await env.db.get_lead(1), zone)  # новое время — ответ о старом не в счёт
