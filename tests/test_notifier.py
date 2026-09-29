from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot

from app.config import Settings
from app.db import TG_DIGEST, TG_LEAD, TG_REMIND, Database
from app.main import build_dispatcher
from app.services.notifier import KV_CHAT_ID, Notifier, next_work_start
from app.services.outbox import Outbox
from tests.conftest import MANAGER, MANAGER2, MANAGER_CHAT, USER, Client, FakeSession
from tests.test_trello import FakeTrello, complete_dialog, make_sync

GROUP = -5000
ALL_DAY = dict(work_start=time(0, 0), work_end=time(23, 59, 59))
NIGHT_ONLY = dict(work_start=time(0, 0), work_end=time(0, 0))  # «сейчас» никогда не рабочее время


@dataclass
class Env:
    db: Database
    client: Client
    session: FakeSession
    notifier: Notifier
    outbox: Outbox
    trello: FakeTrello | None

    async def tick(self, now: datetime | None = None) -> None:
        """Один такт фоновых задач: планировщик + очередь."""
        await self.notifier.scan(now or datetime.now(UTC))
        # Время для очереди — после scan: задачи, поставленные scan'ом, иначе могли оказаться «из будущего»
        # (если между замерами сменилась секунда), и тест случайно падал.
        await self.outbox.run_once(now or datetime.now(UTC))

    def group(self):
        return self.session.sent(GROUP)


async def make_env(db: Database, *, trello: bool = True, **settings_kw) -> Env:
    settings = Settings(bot_token="123:TEST", manager_chat_id=GROUP, **{**ALL_DAY, **settings_kw})
    session = FakeSession()
    bot = Bot("123:TEST", session=session)
    fake = FakeTrello() if trello else None
    notifier = Notifier(bot, db, settings, trello_enabled=trello)
    handlers = {**(make_sync(db, fake).handlers if fake else {}), **notifier.handlers}
    outbox = Outbox(db, handlers)
    client = Client(build_dispatcher(db, settings, notifier), bot, session)
    return Env(db, client, session, notifier, outbox, fake)


@pytest.fixture
async def env(db) -> Env:
    return await make_env(db)


# --- новый лид ---


async def test_qualified_lead_notified_once(env: Env):
    await complete_dialog(env.client)
    await env.tick()
    [msg] = env.group()
    assert "Новая заявка №1" in msg.text
    assert "Анна Петрова (@anna)" in msg.text and "+79001234567" in msg.text
    assert 'href="https://trello.com/c/C1"' in msg.text
    buttons = msg.reply_markup.inline_keyboard[0]
    assert buttons[0].callback_data == "take:1"
    assert buttons[1].url == "https://t.me/anna"
    assert msg.disable_notification is False  # рабочее время — со звуком

    await env.tick()
    await env.tick()
    assert len(env.group()) == 1  # без дублей


async def test_notification_waits_for_trello_card_then_sends_without_link(db):
    env = await make_env(db)
    env.trello.fail["create_card"] = 10  # Trello лежит
    await complete_dialog(env.client)
    now = datetime.now(UTC)
    for i in range(6):
        await env.tick(now + timedelta(minutes=i))
    [msg] = env.group()
    assert "Новая заявка №1" in msg.text and "Trello" not in msg.text


async def test_without_trello_notifies_immediately(db):
    env = await make_env(db, trello=False)
    await complete_dialog(env.client)
    await env.tick()
    assert "Новая заявка №1" in env.group()[0].text


async def test_night_notification_is_silent(db):
    env = await make_env(db, **NIGHT_ONLY)
    await complete_dialog(env.client)
    await env.tick()
    [msg] = env.group()
    assert msg.disable_notification is True and "🌙 ночная" in msg.text


# --- брошенная анкета ---


async def test_abandoned_after_silence(env: Env):
    await env.client.text("/start")
    await env.client.press("obj:house")
    now = datetime.now(UTC)
    await env.tick(now + timedelta(minutes=29))
    assert env.group() == []

    await env.tick(now + timedelta(minutes=31))
    lead = await env.db.last_lead(USER.id)
    assert lead.status == "abandoned"
    [msg] = env.group()
    assert "анкета не завершена" in msg.text and "Дом" in msg.text
    assert env.trello.label_names("C1") == {"не завершил анкету"}

    # Клиент вернулся и дозаполнил — менеджер получает полную заявку.
    await env.client.text("40")
    await env.client.press("ct:glossy")
    await env.client.contact("+79001234567")
    await env.client.text("вечером")
    await env.tick(now + timedelta(minutes=32))
    assert "Новая заявка №1" in env.group()[-1].text
    assert env.trello.label_names("C1") == {"квалифицирован"}


# --- «Взял в работу» ---


async def test_take_moves_card_and_edits_message(env: Env):
    await complete_dialog(env.client)
    await env.tick()
    notification = env.group()[0]

    await env.client.press_in_group(notification, "take:1")
    lead = await env.db.last_lead(USER.id)
    assert lead.taken_by_name == "Иван Менеджеров" and lead.taken_at
    [edit] = env.session.edits()[-1:]
    assert "✅ №1 взял(а): <b>Иван Менеджеров</b>" in edit.text
    assert [b.url for row in edit.reply_markup.inline_keyboard for b in row] == ["https://t.me/anna"]

    await env.tick()
    card = env.trello.cards["C1"]
    assert card["idList"] == "L1"  # «В работе»
    assert card["comments"][-1] == "✅ Взял в работу: **Иван Менеджеров**"

    # Второй менеджер жмёт ту же кнопку — лид остаётся за первым.
    await env.client.press_in_group(notification, "take:1", user=MANAGER2)
    assert (await env.db.last_lead(USER.id)).taken_by_name == "Иван Менеджеров"
    answers = [c for c in env.session.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert answers[-1].show_alert and "Уже взял(а) Иван Менеджеров" in answers[-1].text


async def test_take_from_foreign_chat_ignored(env: Env):
    from aiogram.types import Chat

    await complete_dialog(env.client)
    await env.tick()
    await env.client.press_in_group(env.group()[0], "take:1", chat=Chat(id=-999, type="group"))
    assert (await env.db.last_lead(USER.id)).taken_at is None


async def test_group_messages_do_not_start_dialog(env: Env):
    await env.client.group_text("/start")
    await env.client.group_text("всем привет")
    assert await env.db.last_lead(MANAGER.id) is None
    assert env.session.sent() == []


# --- напоминания ---


async def test_reminders_every_interval_up_to_max(env: Env):
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)
    for minutes in (14, 16, 20, 31, 46, 61, 90):
        await env.tick(t0 + timedelta(minutes=minutes))
    reminders = [m for m in env.group() if "никто не взял" in m.text]
    assert len(reminders) == 3  # на 16, 31 и 46 минуте, дальше — лимит
    assert reminders[0].reply_markup.inline_keyboard[0][0].callback_data == "take:1"


async def test_no_reminders_after_take(env: Env):
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)
    await env.client.press_in_group(env.group()[0], "take:1")
    await env.tick(t0 + timedelta(minutes=20))
    assert not [m for m in env.group() if "никто не взял" in m.text]


# --- ночь и утренний дайджест ---


def test_next_work_start():
    s = Settings(bot_token="1:x", studio_tz="Europe/Moscow", work_start=time(9), work_end=time(21))
    msk = ZoneInfo("Europe/Moscow")
    assert next_work_start(datetime(2026, 9, 26, 2, 0, tzinfo=msk), s) == datetime(2026, 9, 26, 9, 0, tzinfo=msk)
    assert next_work_start(datetime(2026, 9, 26, 22, 0, tzinfo=msk), s) == datetime(2026, 9, 27, 9, 0, tzinfo=msk)
    noon = datetime(2026, 9, 26, 12, 0, tzinfo=msk)
    assert next_work_start(noon, s) == noon


async def test_night_lead_digest_then_reminder(db):
    msk = ZoneInfo("Europe/Moscow")
    env = await make_env(db, studio_tz="Europe/Moscow", work_start=time(9), work_end=time(21))
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="Ночной", username=None, is_night=True)
    await db.update_lead(lead.id, status="qualified", object="Квартира", phone="+79001234567")

    night = datetime(2026, 9, 26, 2, 0, tzinfo=msk).astimezone(UTC)
    await env.notifier.scan(night)
    await env.notifier.scan(night + timedelta(hours=3))  # 05:00 — ни напоминаний, ни дайджеста
    kinds = [t.kind for t in await db.outbox_pending()]
    assert kinds.count(TG_LEAD) == 1 and TG_REMIND not in kinds and TG_DIGEST not in kinds

    morning = datetime(2026, 9, 26, 9, 0, 30, tzinfo=msk).astimezone(UTC)
    await env.notifier.scan(morning)
    await env.notifier.scan(morning + timedelta(minutes=1))  # дайджест — раз в день
    digests = [t for t in await db.outbox_pending() if t.kind == TG_DIGEST]
    assert len(digests) == 1 and digests[0].payload == {"lead_ids": [lead.id]}

    await env.notifier.scan(morning + timedelta(minutes=16))
    assert [t.kind for t in await db.outbox_pending()].count(TG_REMIND) == 1


async def test_digest_message_and_take_from_it(env: Env):
    a = await env.db.create_lead(tg_user_id=1, chat_id=1, name="Первый", username=None, is_night=True)
    b = await env.db.create_lead(tg_user_id=2, chat_id=2, name="Второй", username=None, is_night=True)
    await env.db.enqueue(TG_DIGEST, None, {"lead_ids": [a.id, b.id]})
    await env.outbox.run_once()
    digest = [m for m in env.group() if "Доброе утро" in m.text][0]
    assert "№1</b> Первый" in digest.text and "№2</b> Второй" in digest.text

    await env.client.press_in_group(digest, f"take:{a.id}")
    edit = env.session.edits()[-1]
    left = [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row]
    assert left == [f"take:{b.id}"]


# --- клиент дописал после анкеты ---


async def test_client_messages_after_done_batched(env: Env):
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)
    await env.client.text("ещё хочу подсветку")
    await env.client.text("и карниз")
    await env.tick(t0 + timedelta(seconds=30))
    assert not [m for m in env.group() if "дописал" in m.text]  # ждём, вдруг напишет ещё

    await env.tick(t0 + timedelta(seconds=61))
    [msg] = [m for m in env.group() if "дописал" in m.text]
    assert "• ещё хочу подсветку\n• и карниз" in msg.text
    assert "/start" not in msg.text and "Квартира" not in msg.text  # ответы анкеты менеджер уже видел

    await env.client.text("и побыстрее")
    await env.tick(t0 + timedelta(minutes=5))
    last = [m for m in env.group() if "дописал" in m.text][-1]
    assert "побыстрее" in last.text and "подсветку" not in last.text


# --- супергруппа ---


async def test_group_migration_switches_chat(env: Env):
    env.session.migrate[GROUP] = -100777
    await complete_dialog(env.client)
    await env.tick()
    assert await env.db.kv_get(KV_CHAT_ID) == "-100777"
    assert "Новая заявка" in env.session.sent(-100777)[0].text
    assert await env.notifier.chat_id() == -100777


async def test_group_migration_service_message_switches_chat_at_once(env: Env):
    # Telegram присылает в старую группу служебное сообщение о переходе в супергруппу — переходим сразу,
    # не дожидаясь следующей отправки (иначе «Взял» и /status из новой группы не принимались бы).
    from aiogram.types import Message, Update

    migrated = Message(message_id=900, date=datetime.now(UTC), chat=MANAGER_CHAT, migrate_to_chat_id=-100777)
    await env.client.dp.feed_update(env.client.bot, Update(update_id=9000, message=migrated))
    assert await env.notifier.chat_id() == -100777
    assert await env.db.kv_get(KV_CHAT_ID) == "-100777"
