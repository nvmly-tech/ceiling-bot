"""Замер глазами клиента: подтверждение, напоминание накануне, ответы «жду / перенести / отменить»."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app import stages
from app.bot import texts
from app.db import Database, now_iso
from tests.conftest import CHAT, FakeSession
from tests.test_notifier import Env, make_env
from tests.test_trello import complete_dialog

ZONE = ZoneInfo("Europe/Moscow")


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db)


async def take(env: Env) -> None:
    await complete_dialog(env.client)
    await env.tick()
    await env.client.press_in_group(env.group()[0], "take:1")
    await env.tick()


def local(days_ahead: int, hour: int) -> datetime:
    day = datetime.now(ZONE).date() + timedelta(days=days_ahead)
    return datetime.combine(day, time(hour), tzinfo=ZONE)


async def schedule(env: Env, moment: datetime) -> str:
    """Менеджер назначил замер кнопкой на панели."""
    panel = [m for m in env.group() if m.text.startswith("📋")][-1]
    await env.client.press_in_group(panel, f"st:1:at:{moment:%Y%m%d%H}")
    await env.tick()
    return stages.stamp(now_iso(moment.astimezone(UTC)))


def client_msgs(env: Env) -> list[SendMessage]:
    return env.session.sent(CHAT.id)


def buttons(msg: SendMessage) -> list[str]:
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row] if msg.reply_markup else []


async def test_client_gets_measure_confirmation(env: Env):
    await take(env)
    stamp = await schedule(env, local(1, 14))
    msg = client_msgs(env)[-1]
    assert "Замер по заявке №1 назначен" in msg.text and "в 14:00" in msg.text
    assert buttons(msg) == [f"visit:1:move:{stamp}", f"visit:1:cancel:{stamp}"]
    await env.tick()
    assert any("Замер по заявке №1 назначен" in c for c in env.trello.cards["C1"]["comments"])  # и в переписке


async def test_past_measure_not_announced_to_client(env: Env):
    await take(env)
    n = len(client_msgs(env))
    past = now_iso(datetime.now(UTC) - timedelta(hours=1))
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=past)
    await env.tick()
    assert len(client_msgs(env)) == n  # менеджер отметил замер задним числом — клиенту писать нечего


async def test_reminder_day_before_at_noon(env: Env):
    await take(env)
    moment = local(3, 14)
    stamp = await schedule(env, moment)
    remind_at = datetime.combine(moment.date() - timedelta(days=1), time(12), tzinfo=ZONE).astimezone(UTC)
    n = len(client_msgs(env))
    await env.tick(remind_at - timedelta(minutes=1))
    assert len(client_msgs(env)) == n
    await env.tick(remind_at + timedelta(minutes=1))
    msg = client_msgs(env)[-1]
    assert msg.text.startswith("Напоминаю") and "завтра" in msg.text and "в 14:00" in msg.text
    assert buttons(msg) == [f"visit:1:{a}:{stamp}" for a in ("yes", "move", "cancel")]
    await env.tick(remind_at + timedelta(hours=1))
    assert len(client_msgs(env)) == n + 1  # один раз


async def test_no_reminder_when_scheduled_after_noon_before(env: Env):
    """Замер назначили меньше чем за сутки — подтверждение только что ушло, второе сообщение лишнее."""
    await take(env)
    soon = now_iso(datetime.now(UTC) + timedelta(hours=5))
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=soon)
    await env.tick()
    n = len(client_msgs(env))
    await env.tick(datetime.now(UTC) + timedelta(hours=1))
    assert len(client_msgs(env)) == n


async def test_rescheduled_measure_reminded_again(env: Env):
    await take(env)
    first = local(3, 14)
    await schedule(env, first)
    noon = datetime.combine(first.date() - timedelta(days=1), time(12), tzinfo=ZONE).astimezone(UTC)
    await env.tick(noon + timedelta(minutes=1))
    n = len(client_msgs(env))
    await schedule(env, local(5, 11))  # перенесли на другой день
    later_noon = datetime.combine(local(5, 11).date() - timedelta(days=1), time(12), tzinfo=ZONE).astimezone(UTC)
    await env.tick(later_noon + timedelta(minutes=1))
    assert client_msgs(env)[-1].text.startswith("Напоминаю")
    assert len(client_msgs(env)) == n + 2  # подтверждение нового времени + напоминание о нём


@pytest.mark.parametrize(("answer", "group_word", "reply"), [
    ("yes", "подтвердил", texts.VISIT_YES_REPLY),
    ("move", "перенести", texts.VISIT_MOVE_REPLY),
    ("cancel", "отменил", texts.VISIT_CANCEL_REPLY),
])
async def test_client_answers_reach_managers(env: Env, answer: str, group_word: str, reply: str):
    await take(env)
    stamp = await schedule(env, local(1, 14))
    await env.client.press(f"visit:1:{answer}:{stamp}")
    assert env.client.last_text() == reply
    assert env.session.edits()[-1].reply_markup is None  # кнопки под сообщением убраны
    await env.tick()
    note = env.group()[-1]
    assert "№1" in note.text and group_word in note.text and "в 14:00" in note.text
    if answer == "yes":
        assert note.reply_markup is None
    else:
        assert 'href="tg://user?id=7"' in note.text  # тому, кто ведёт заявку
        assert "st:1:measure" in buttons(note)
    msgs = await env.db.get_messages(1, direction="in")
    assert msgs[-1].kind == "visit"
    # Ответ уже передан отдельным уведомлением — в «клиент дописал» он не повторяется.
    await env.tick(datetime.now(UTC) + timedelta(minutes=5))
    assert not [m for m in env.group() if "дописал" in m.text]


async def test_stale_visit_button(env: Env):
    await take(env)
    old = await schedule(env, local(1, 14))
    await schedule(env, local(2, 16))
    n = len(env.group())
    await env.client.press(f"visit:1:cancel:{old}")
    answers = [c for c in env.session.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert answers[-1].show_alert and "изменилось" in answers[-1].text
    await env.tick()
    assert len(env.group()) == n and (await env.db.get_lead(1)).stage == stages.MEASURE


async def test_foreign_or_forged_visit_buttons_ignored(env: Env):
    await take(env)
    stamp = await schedule(env, local(1, 14))
    await env.db.conn.execute("UPDATE leads SET tg_user_id = 777 WHERE id = 1")  # чужая заявка
    await env.db.conn.commit()
    for data in (f"visit:1:yes:{stamp}", "visit:x:yes:1", f"visit:1:fly:{stamp}", "visit:1"):
        await env.client.press(data)
    await env.tick()
    assert not [m for m in env.group() if "подтвердил" in m.text]


async def test_client_blocked_bot(env: Env, monkeypatch):
    await take(env)
    original = FakeSession.make_request

    async def blocked(self, bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == CHAT.id:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        return await original(self, bot, method, timeout)

    monkeypatch.setattr(FakeSession, "make_request", blocked)
    await env.db.set_stage(1, stages.MEASURE, by_name="Иван", measure_at=now_iso(datetime.now(UTC) + timedelta(days=1)))
    await env.tick()
    assert await env.db.outbox_pending() == []  # не повторяем бесконечно и не держим очередь заявки
