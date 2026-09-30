"""Отчёт владельцу по заявкам: недельный (сам) и по команде /report."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import stages
from app.config import Settings
from app.db import Database, now_iso
from app.services.report import plural, report_text
from tests.conftest import CHAT
from tests.test_notifier import GROUP, make_env
from tests.test_trello import complete_dialog

ZONE = ZoneInfo("Europe/Moscow")
OWNER = -8000
SETTINGS = Settings(bot_token="123:TEST", work_start=time(0), work_end=time(23, 59, 59))


def test_plural():
    forms = ("заявка", "заявки", "заявок")
    assert [plural(n, forms) for n in (1, 2, 5, 11, 21, 22, 25, 111)] == [
        "1 заявка", "2 заявки", "5 заявок", "11 заявок", "21 заявка", "22 заявки", "25 заявок", "111 заявок",
    ]


async def lead(db: Database, n: int, **fields) -> int:
    row = await db.create_lead(tg_user_id=100 + n, chat_id=100 + n, name=f"Клиент {n}", username=None, is_night=False)
    if fields:
        await db.update_lead(row.id, **fields)
    return row.id


async def notified(db: Database, lead_id: int, minutes_ago: int) -> None:
    at = now_iso(datetime.now(UTC) - timedelta(minutes=minutes_ago))
    await db.update_lead(lead_id, status="qualified", notified_status="qualified", notified_at=at)


async def test_report_counts(db: Database):
    a = await lead(db, 1, hotness="горячий")
    await notified(db, a, 6)
    await db.take_lead(a, by_id=7, by_name="Иван")
    await db.set_stage(a, stages.CONTRACT, by_name="Иван")
    await db.set_once(a, "rating", 5)

    b = await lead(db, 2, hotness="тёплый", is_night=True)
    await notified(db, b, 20)
    await db.take_lead(b, by_id=8, by_name="Олег")
    await db.set_stage(b, stages.REFUSED, by_name="Олег", reason="price")
    await db.set_once(b, "rating", 2)

    c = await lead(db, 3, hotness="тёплый")
    await notified(db, c, 10)
    await db.take_lead(c, by_id=7, by_name="Иван")
    await db.set_once(c, "contact_answer", "no")

    d = await lead(db, 4)
    await notified(db, d, 90)  # так никто и не взял
    await lead(db, 5, status="abandoned")
    await lead(db, 6, status="cancelled")

    until = datetime.now(UTC) + timedelta(minutes=1)
    since = until - timedelta(days=7)
    text = report_text(await db.leads_created_between(since, until), since, until, SETTINGS)

    assert text.startswith("📊 <b>Отчёт по заявкам</b>")
    assert "<b>Заявок:</b> 6" in text
    assert "анкета заполнена: 4" in text and "не завершили: 1" in text and "закрыты клиентом: 1" in text
    assert "🔥 горячих: 1" in text and "🌤 тёплых: 2" in text and "🌙 ночных: 1" in text
    assert "взяли: 3 из 4" in text and "обычно через 10 мин" in text  # медиана из 6, 10 и 20
    assert f"не взяли: 1 (№{d})" in text
    assert "Иван — 2 заявки, обычно через 8 мин" in text and "Олег — 1 заявка, обычно через 20 мин" in text
    assert "✅ договор: 1" in text and "🛠 без итога: 1" in text
    assert "❌ отказ: 1 — дорого 1" in text
    assert f"«с нами не связались»: 1 (№{c})" in text
    assert "оценка замера: 3.5 из 5 (2 оценки), низких: 1" in text


async def test_report_for_empty_period(db: Database):
    until = datetime.now(UTC)
    text = report_text([], until - timedelta(days=7), until, SETTINGS)
    assert "Заявок за период не было" in text


def next_monday(hour: int = 10) -> datetime:
    today = datetime.now(ZONE).date()
    monday = today + timedelta(days=7 - today.weekday())
    return datetime.combine(monday, time(hour), tzinfo=ZONE).astimezone(UTC)


def reports(env, chat: int):
    return [m for m in env.session.sent(chat) if m.text.startswith("📊")]


async def test_weekly_report_on_new_week(db: Database):
    env = await make_env(db, owner_chat_id=OWNER, weekly_report=True)
    await complete_dialog(env.client)
    await env.tick()
    assert reports(env, OWNER) == []  # первый запуск: за неделю, когда отчётов ещё не было, не шлём

    await env.tick(next_monday())
    [report] = reports(env, OWNER)
    monday = next_monday().astimezone(ZONE).date()
    period = f"{monday - timedelta(days=7):%d.%m}–{monday - timedelta(days=1):%d.%m}"
    assert period in report.text and "<b>Заявок:</b> 1" in report.text

    await env.tick(next_monday(15))
    await env.tick(next_monday() + timedelta(days=2))
    assert len(reports(env, OWNER)) == 1  # раз в неделю


async def test_weekly_report_waits_for_work_time(db: Database):
    env = await make_env(db, owner_chat_id=OWNER, weekly_report=True, work_start=time(9), work_end=time(21))
    await env.tick(next_monday(12) - timedelta(days=7))
    await env.tick(next_monday(3))  # ночь понедельника
    assert reports(env, OWNER) == []
    await env.tick(next_monday(9))
    assert len(reports(env, OWNER)) == 1


async def test_weekly_report_can_be_disabled(db: Database):
    env = await make_env(db, owner_chat_id=OWNER, weekly_report=False)
    await env.tick()
    await env.tick(next_monday())
    assert reports(env, OWNER) == []


async def test_weekly_report_goes_to_group_without_owner_chat(db: Database):
    env = await make_env(db, weekly_report=True)
    await env.tick()
    await env.tick(next_monday())
    assert len(reports(env, GROUP)) == 1


async def test_report_command(db: Database):
    env = await make_env(db, owner_chat_id=OWNER)
    await complete_dialog(env.client)
    await env.client.group_text("/report")
    [report] = reports(env, GROUP)
    assert "<b>Заявок:</b> 1" in report.text and "за 7 дней" in report.text

    await env.client.group_text("/report 30")
    assert "за 30 дней" in reports(env, GROUP)[-1].text
    await env.client.group_text("/report много")  # непонятный срок — обычные 7 дней
    assert "за 7 дней" in reports(env, GROUP)[-1].text

    await env.client.text("/report")  # клиент в личке — отчёт не показываем
    assert reports(env, CHAT.id) == []
