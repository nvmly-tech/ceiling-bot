"""Общий дневной лимит обращений к LLM: тысяча фейковых аккаунтов не должна стоить студии тысячи рублей."""

from datetime import UTC, datetime, time

from aiogram import Bot

from app.bot.assistant import LeadAssistant
from app.config import Settings
from app.db import Database
from app.main import build_dispatcher
from app.services.llm import LLMRouter
from tests.conftest import USER, Client, FakeSession
from tests.test_health import ADMIN, CheckedProvider, make_monitor
from tests.test_llm import SUMMARY, FakeProvider, notify_env, qualified_lead, run_all, turn

TODAY, TOMORROW = "2026-10-01", "2026-10-02"


async def lead(db: Database, n: int = 1) -> int:
    return (await db.create_lead(tg_user_id=n, chat_id=n, name="К", username=None, is_night=False)).id


async def test_daily_budget_is_shared_by_all_leads(db: Database):
    a, b = await lead(db, 1), await lead(db, 2)
    assert [await db.spend_llm_call(a, 40, day_limit=3, day=TODAY) for _ in range(2)] == [True, True]
    assert await db.spend_llm_call(b, 40, day_limit=3, day=TODAY)
    assert not await db.spend_llm_call(b, 40, day_limit=3, day=TODAY)  # день исчерпан — и для новой заявки
    assert (await db.get_lead(b)).llm_calls == 1  # отказ не списывает с заявки
    assert await db.spend_llm_call(b, 40, day_limit=3, day=TOMORROW)  # новый день — новый лимит
    assert await db.llm_calls_on(TODAY) == 3


async def test_lead_limit_does_not_eat_daily_budget(db: Database):
    a = await lead(db)
    assert await db.spend_llm_call(a, 1, day_limit=10, day=TODAY)
    assert not await db.spend_llm_call(a, 1, day_limit=10, day=TODAY)
    assert await db.llm_calls_on(TODAY) == 1


async def test_zero_means_no_daily_limit(db: Database):
    a = await lead(db)
    assert all([await db.spend_llm_call(a, 100, day_limit=0, day=TODAY) for _ in range(5)])


def client_with(db: Database, provider: FakeProvider, **settings_kw) -> tuple[Client, FakeSession]:
    settings = Settings(bot_token="123:TEST", work_start=time(0), work_end=time(23, 59, 59), **settings_kw)
    session = FakeSession()
    dp = build_dispatcher(db, settings, None, None, LeadAssistant(LLMRouter([provider])))
    return Client(dp, Bot("123:TEST", session=session), session), session


async def test_dialog_goes_to_script_when_day_is_spent(db: Database):
    ds = FakeProvider("deepseek", turn("Квартира, понял. Какая площадь?", object="квартира"),
                      turn("18 м², отлично. Какой потолок?", area_m2=18))
    client, _ = client_with(db, ds, llm_calls_per_day=1)
    await client.text("Нужен потолок в квартиру")
    await client.text("метров 18")
    assert len(ds.calls) == 1  # второе сообщение — уже без LLM
    lead = await db.last_lead(USER.id)
    assert lead.area_m2 == 18 or lead.area_text  # скрипт всё равно принял ответ


async def test_summary_skipped_when_day_is_spent(db: Database):
    ds = FakeProvider("deepseek", SUMMARY)
    notifier, outbox, session, _ = await notify_env(db, ds)
    notifier.settings = notifier.settings.model_copy(update={"llm_calls_per_day": 1})
    day = datetime.now(notifier.settings.zone).date().isoformat()
    other = await lead(db, 50)
    await db.spend_llm_call(other, 40, day_limit=1, day=day)  # день уже израсходован
    await qualified_lead(db)
    await run_all(notifier, outbox)
    assert ds.calls == [] and "📝" not in session.sent(-5000)[0].text  # уведомление ушло без резюме


async def test_monitor_alerts_once_and_status_shows_usage(db: Database):
    monitor, clock, session, _ = await make_monitor(db, providers=[CheckedProvider("deepseek")], admin=ADMIN)
    monitor.settings = monitor.settings.model_copy(update={"llm_calls_per_day": 2})
    day = clock.now.astimezone(monitor.settings.zone).date().isoformat()
    a = await lead(db)
    await db.spend_llm_call(a, 40, day_limit=2, day=day)
    await monitor.check_models()
    assert not [m for m in session.sent(ADMIN) if "лимит" in m.text]
    await db.spend_llm_call(a, 40, day_limit=2, day=day)
    await monitor.check_models()
    await monitor.check_models()
    [alert] = [m for m in session.sent(ADMIN) if "лимит" in m.text]
    assert "2" in alert.text and "скрипт" in alert.text
    monitor.clock = lambda: datetime.now(UTC)
    assert "LLM за сегодня" in await monitor.status_text()
