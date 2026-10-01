"""Утро после ночи с заявками: не залп из десятков сообщений в группу, а списки."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import Database, now_iso
from app.services.notifier import LIST_MAX
from tests.test_notifier import GROUP, Env, make_env

ZONE = ZoneInfo("Europe/Moscow")
OWNER = -8000
WORK = dict(work_start=time(9), work_end=time(21))


def at(days: int, hour: int, minute: int = 0) -> datetime:
    """Момент в часовом поясе студии. Утро в тестах — завтрашнее: задачи очереди ставятся по настоящим часам,
    и «сегодня 9:15», уже прошедшее к запуску тестов, сделало бы их «запланированными на потом»."""
    day = datetime.now(ZONE).date() + timedelta(days=days)
    return datetime.combine(day, time(hour, minute), tzinfo=ZONE).astimezone(UTC)


async def night_leads(db: Database, n: int, *, night_of: int = 0) -> list[int]:
    """n заявок, о которых менеджеров уведомили ночью (в 23:00 дня night_of), никто не взял."""
    ids = []
    for i in range(n):
        lead = await db.create_lead(tg_user_id=100 + i, chat_id=100 + i, name=f"Клиент {i}", username=None,
                                    is_night=True)
        await db.update_lead(lead.id, status="qualified", notified_status="qualified",
                             notified_at=now_iso(at(night_of, 23)), object="Квартира", phone="+79001234567")
        ids.append(lead.id)
    return ids


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db, owner_chat_id=OWNER, trello=False, **WORK)


def buttons(msg) -> list[str]:
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row] if msg.reply_markup else []


async def test_morning_reminders_go_as_one_list(env: Env):
    ids = await night_leads(env.db, 40)
    for moment in (at(1, 9, 0), at(1, 9, 15, ), at(1, 9, 30), at(1, 9, 45), at(1, 10, 5)):
        await env.tick(moment + timedelta(seconds=30))
    group = env.session.sent(GROUP)
    lists = [m for m in group if m.text.startswith("⏰")]
    assert len(lists) == 3  # 9:15, 9:30, 9:45 — по одному сообщению, а не по 40
    assert "Заявки ждут менеджера: 40" in lists[0].text
    assert len(buttons(lists[0])) == LIST_MAX and f"и ещё {40 - LIST_MAX}" in lists[0].text
    assert all(len(m.text) <= 4096 for m in group)
    assert len(group) == 1 + 3  # сводка + три списка
    lead = await env.db.get_lead(ids[0])
    assert lead.reminders_sent == 3  # счётчики напоминаний идут как раньше
    assert "ждёт" in lists[0].text


async def test_single_due_reminder_stays_personal(env: Env):
    await night_leads(env.db, 1)
    await env.tick(at(1, 9, 0))
    await env.tick(at(1, 9, 15, ) + timedelta(seconds=30))
    [reminder] = [m for m in env.session.sent(GROUP) if m.text.startswith("⏰")]
    assert "Заявку №1 никто не взял" in reminder.text


async def test_digest_lists_only_last_night_and_is_capped(env: Env):
    old = await night_leads(env.db, 3, night_of=-2)  # трое суток назад: уже были в прошлых сводках
    fresh = await night_leads(env.db, LIST_MAX + 5)
    await env.tick(at(1, 9, 0) + timedelta(seconds=30))
    [digest] = [m for m in env.session.sent(GROUP) if m.text.startswith("☀️")]
    assert f"Ночных заявок ждут менеджера: {len(fresh)}" in digest.text
    assert all(f"№{i} " not in digest.text for i in old)
    assert len(buttons(digest)) == LIST_MAX and "и ещё 5" in digest.text and len(digest.text) <= 4096


async def test_list_rebuilt_without_deleted_lead(env: Env):
    a, b = await night_leads(env.db, 2)
    await env.tick(at(1, 9, 0))
    await env.tick(at(1, 9, 15) + timedelta(seconds=30))
    await env.db.delete_lead_data(a)
    await env.tick(at(1, 9, 16))
    rebuilt = [e for e in env.session.edits() if e.text.startswith("⏰")]
    assert rebuilt and "Заявки ждут менеджера: 1" in rebuilt[-1].text and f"№{b}" in rebuilt[-1].text
    assert buttons(rebuilt[-1]) == [f"take:{b}"]


async def test_untaken_escalations_batched_to_owner(env: Env):
    await night_leads(env.db, 5)
    for moment in (at(1, 9, 0), at(1, 9, 15), at(1, 9, 30), at(1, 9, 45), at(1, 10, 0), at(1, 10, 30)):
        await env.tick(moment + timedelta(seconds=30))
    owner = env.session.sent(OWNER)
    assert len(owner) == 1 and "Заявки так никто и не взял: 5" in owner[0].text


async def test_single_untaken_escalation_stays_personal(env: Env):
    await night_leads(env.db, 1)
    for moment in (at(1, 9, 0), at(1, 9, 15), at(1, 9, 30), at(1, 9, 45), at(1, 10, 0)):
        await env.tick(moment + timedelta(seconds=30))
    [owner] = env.session.sent(OWNER)
    assert "Заявка №1" in owner.text and "никто не взял" in owner.text
