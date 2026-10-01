"""Рабочие дни и праздники: в нерабочий день — как ночью (без звука, без напоминаний, «ответим в понедельник»)."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import Database, now_iso
from app.worktime import last_work_end, manager_eta, next_work_start, work_day, work_time
from tests.conftest import USER
from tests.test_notifier import GROUP, make_env

ZONE = ZoneInfo("Europe/Moscow")
FRI = date(2026, 10, 2)  # пятница
SAT, SUN, MON = FRI + timedelta(days=1), FRI + timedelta(days=2), FRI + timedelta(days=3)


def s(**kw) -> Settings:
    return Settings(bot_token="1:T", work_start=time(9), work_end=time(21), **kw)


def local(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(d, time(hour, minute), tzinfo=ZONE)


@pytest.mark.parametrize(("value", "days"), [
    ("1-7", {1, 2, 3, 4, 5, 6, 7}), ("1-5", {1, 2, 3, 4, 5}), ("1,3,5-7", {1, 3, 5, 6, 7}),
    (" 1 - 6 ", {1, 2, 3, 4, 5, 6}),
])
def test_work_days_parsed(value, days):
    assert s(work_days=value).workdays == frozenset(days)


@pytest.mark.parametrize("value", ["0-5", "8", "5-1", "пн-пт", ",", "1-"])
def test_bad_work_days_fail_at_start(value):
    with pytest.raises(ValidationError):
        s(work_days=value)


def test_days_off_full_dates_and_yearly():
    cfg = s(days_off="2026-12-31, 01-01,01-07")
    assert not work_day(date(2026, 12, 31), cfg) and not work_day(date(2027, 1, 1), cfg)
    assert not work_day(date(2030, 1, 7), cfg)  # «01-07» — каждый год
    assert work_day(date(2026, 12, 30), cfg) and work_day(date(2027, 12, 31), cfg)


@pytest.mark.parametrize("value", ["2026-13-01", "31-12", "завтра", "2026-02-30"])
def test_bad_days_off_fail_at_start(value):
    with pytest.raises(ValidationError):
        s(days_off=value)


def test_work_time_respects_days():
    cfg = s(work_days="1-5")
    assert work_time(local(FRI, 12), cfg) and not work_time(local(SAT, 12), cfg) and not work_time(local(FRI, 22), cfg)
    assert work_time(local(SAT, 12), s())  # по умолчанию все дни рабочие — как раньше


def test_next_work_start_skips_weekend_and_holidays():
    cfg = s(work_days="1-5")
    assert next_work_start(local(FRI, 22), cfg) == local(MON, 9)
    assert next_work_start(local(SAT, 12), cfg) == local(MON, 9)
    assert next_work_start(local(FRI, 8), cfg) == local(FRI, 9)
    assert next_work_start(local(FRI, 12), cfg) == local(FRI, 12)
    holiday = s(work_days="1-5", days_off=MON.isoformat())
    assert next_work_start(local(FRI, 22), holiday) == local(MON + timedelta(days=1), 9)


def test_last_work_end():
    cfg = s(work_days="1-5")
    assert last_work_end(local(MON, 9), cfg) == local(FRI, 21)  # с конца пятницы
    assert last_work_end(local(FRI, 9), cfg) == local(FRI - timedelta(days=1), 21)


@pytest.mark.parametrize(("now", "days_off", "eta"), [
    (local(FRI - timedelta(days=1), 3), "", "сегодня в 9:00"),
    (local(FRI - timedelta(days=1), 22), "", "завтра в 9:00"),
    (local(FRI, 22), "", "в понедельник в 9:00"),
    (local(SAT, 12), "", "в понедельник в 9:00"),
    (local(FRI, 22), f"{MON.isoformat()},{(MON + timedelta(days=1)).isoformat()}", "в среду в 9:00"),
    # Новогодние до 8-го включительно; 9 января 2027 — суббота, при пятидневке — понедельник 11-го.
    (local(date(2026, 12, 30), 22), "12-31,01-01,01-02,01-03,01-04,01-05,01-06,01-07,01-08", "11 января в 9:00"),
])
def test_manager_eta(now, days_off, eta):
    assert manager_eta(now, s(work_days="1-5", days_off=days_off)) == eta


def test_no_work_day_within_a_year_is_an_error_not_a_hang():
    every_day = ",".join(f"{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 32)
                         if not ((m == 2 and d > 29) or (m in (4, 6, 9, 11) and d == 31)))
    with pytest.raises(RuntimeError, match="WORK_DAYS"):
        next_work_start(local(FRI, 22), s(days_off=every_day))


# --- в работе бота ---


async def test_client_on_day_off_hears_when_manager_answers(db: Database):
    today = datetime.now(ZONE).isoweekday()
    others = ",".join(str(d) for d in range(1, 8) if d != today)  # сегодня — выходной
    env = await make_env(db, work_days=others, trello=False)
    await env.client.text("/start")
    [greeting] = [m.text for m in env.session.sent(USER.id) if "нерабочее время" in m.text]
    assert "завтра в" in greeting
    assert (await db.last_lead(USER.id)).is_night


async def test_no_reminders_on_day_off_and_digest_covers_weekend(db: Database):
    env = await make_env(db, work_days="1-5", trello=False, work_start=time(9), work_end=time(21))
    saturday = local(date.today() + timedelta(days=(5 - date.today().weekday()) % 7 + 7), 12)  # суббота впереди
    friday_evening = saturday - timedelta(hours=14)
    sunday, monday = saturday + timedelta(days=1), saturday + timedelta(days=2)
    thursday = saturday - timedelta(days=2)
    ids = []
    for i, at in enumerate((thursday.replace(hour=23), friday_evening, saturday, sunday)):
        lead = await db.create_lead(tg_user_id=300 + i, chat_id=300 + i, name=f"К{i}", username=None, is_night=True)
        await db.update_lead(lead.id, status="qualified", notified_status="qualified", notified_at=now_iso(at))
        ids.append(lead.id)
    await env.tick((sunday + timedelta(hours=3)).astimezone(UTC))
    assert env.session.sent(GROUP) == []  # выходной: ни напоминаний, ни сводки
    await env.tick(monday.replace(hour=9, minute=1).astimezone(UTC))
    [digest] = [m for m in env.session.sent(GROUP) if m.text.startswith("☀️")]
    # С конца пятницы (21:00): пятничный вечер, суббота, воскресенье — да; четверговая ночь была в пятничной сводке.
    assert all(f"№{i}<" in digest.text for i in ids[1:]) and f"№{ids[0]}<" not in digest.text
