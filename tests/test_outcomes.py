"""Итоги заявки после «Взял в работу»: панель заявки в группе, кнопки этапов, списки Trello."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText
from aiogram.types import Chat

from app import stages
from app.bot.outcomes import DAYS_AHEAD, parse_moment
from app.config import Settings
from app.db import Database
from tests.conftest import MANAGER2, USER
from tests.test_notifier import Env, make_env
from tests.test_trello import complete_dialog

ZONE = ZoneInfo("Europe/Moscow")


@pytest.fixture
async def env(db: Database) -> Env:
    return await make_env(db)


async def take(env: Env) -> None:
    """Анкета заполнена, уведомление ушло, менеджер нажал «Взял», панель отправлена."""
    await complete_dialog(env.client)
    await env.tick()
    await env.client.press_in_group(env.group()[0], "take:1")
    await env.tick()


def panel(env: Env):
    return [m for m in env.group() if m.text.startswith("📋")][-1]


async def press(env: Env, data: str, user=None) -> None:
    await env.client.press_in_group(panel(env), data, **({"user": user} if user else {}))


def buttons(markup) -> list[str]:
    """Кнопки этапов (st:…); «Ответить через бота» — в tests/test_relay.py."""
    return [b.callback_data for row in markup.inline_keyboard for b in row
            if (b.callback_data or "").startswith("st:")] if markup else []


def last_markup(env: Env):
    """Кнопки после последней правки сообщения — текста с кнопками или только кнопок."""
    edits = [c for c in env.session.calls if isinstance(c, (EditMessageReplyMarkup, EditMessageText))]
    return edits[-1].reply_markup


def answers(env: Env) -> list[AnswerCallbackQuery]:
    return [c for c in env.session.calls if isinstance(c, AnswerCallbackQuery)]


def day_code(days_ahead: int = 0) -> str:
    return (datetime.now(ZONE).date() + timedelta(days=days_ahead)).strftime("%Y%m%d")


# --- этапы: тексты ---


def test_stage_text():
    at = datetime(2026, 10, 2, 11, 0, tzinfo=UTC).isoformat()  # 14:00 по Москве, пятница
    assert stages.stage_text(stages.MEASURE, at, None, ZONE) == "📅 замер назначен: пт 02.10 в 14:00"
    assert stages.stage_text(stages.REFUSED, None, "price", ZONE) == "❌ отказ: дорого"
    assert stages.stage_text(None, None, None, ZONE) == "🛠 в работе, итога ещё нет"
    assert stages.stage_text(stages.CONTRACT, at, None, ZONE) == "✅ договор"


def test_measure_hours_follow_work_time():
    from datetime import time

    assert stages.measure_hours(time(9), time(21)) == list(range(9, 21))
    assert stages.measure_hours(time(9), time(20, 30)) == list(range(9, 21))
    assert stages.measure_hours(time(0), time(23, 59, 59)) == list(range(24))


# --- панель ---


async def test_take_posts_panel_with_stage_buttons(env: Env):
    await take(env)
    p = panel(env)
    assert "№1" in p.text and "Анна Петрова" in p.text and "+79001234567" in p.text
    assert "Иван Менеджеров" in p.text and "в работе, итога ещё нет" in p.text
    assert buttons(p.reply_markup) == ["st:1:measure", "st:1:no_answer", "st:1:refuse"]
    assert p.reply_parameters.message_id == 500  # ответом на уведомление, которое взяли
    # Панель — сообщение о заявке: удалится, если клиент удалит заявку.
    assert [m.kind for m in await env.db.tg_messages(1)].count("panel") == 1


async def test_measure_picker_sets_stage_and_moves_card(env: Env):
    await take(env)
    await press(env, "st:1:measure")
    days = buttons(last_markup(env))
    assert days[:2] == [f"st:1:day:{day_code(0)}", f"st:1:day:{day_code(1)}"] and len(days) == 7 + 1  # + «назад»
    assert (await env.db.get_lead(1)).stage is None  # выбор даты ещё ничего не меняет

    await press(env, f"st:1:day:{day_code(1)}")
    hours = buttons(last_markup(env))
    assert f"st:1:at:{day_code(1)}14" in hours and "st:1:measure" in hours  # «другой день»

    await press(env, f"st:1:at:{day_code(1)}14")
    lead = await env.db.get_lead(1)
    tomorrow = datetime.now(ZONE).date() + timedelta(days=1)
    expected = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 14, tzinfo=ZONE).astimezone(UTC)
    assert lead.stage == stages.MEASURE and lead.measure_at == expected.isoformat(timespec="seconds")
    assert lead.stage_by_name == "Иван Менеджеров"
    edit = env.session.edits()[-1]
    assert "📅 замер назначен:" in edit.text and "в 14:00" in edit.text
    assert buttons(edit.reply_markup) == ["st:1:contract", "st:1:thinking", "st:1:measure", "st:1:refuse"]
    assert answers(env)[-1].text.startswith("Отмечено")

    await env.tick()
    card = env.trello.cards["C1"]
    lists = {lst["name"]: lst["id"] for lst in env.trello.lists_}
    assert card["idList"] == lists["Замер"]
    assert card["comments"][-1].startswith("📅 замер назначен:") and "Иван Менеджеров" in card["comments"][-1]
    assert "Этап:" in card["desc"] and "Иван Менеджеров" in card["desc"]


async def test_refuse_with_reason_then_reopen(env: Env):
    await take(env)
    await press(env, "st:1:refuse")
    assert buttons(last_markup(env)) == [f"st:1:rsn:{code}" for code in stages.REFUSE_REASONS] + ["st:1:back"]
    await press(env, "st:1:rsn:price", user=MANAGER2)  # отметить может любой менеджер группы
    lead = await env.db.get_lead(1)
    assert (lead.stage, lead.refuse_reason, lead.stage_by_name) == (stages.REFUSED, "price", "Олег")
    assert buttons(env.session.edits()[-1].reply_markup) == ["st:1:reopen"]
    await env.tick()
    lists = {lst["name"]: lst["id"] for lst in env.trello.lists_}
    assert env.trello.cards["C1"]["idList"] == lists["Отказ"]
    assert "отказ: дорого" in env.trello.cards["C1"]["comments"][-1]

    await press(env, "st:1:reopen")
    lead = await env.db.get_lead(1)
    assert lead.stage is None and lead.refuse_reason is None
    await env.tick()
    assert env.trello.cards["C1"]["idList"] == lists["В работе"]


async def test_back_restores_stage_buttons(env: Env):
    await take(env)
    await press(env, "st:1:refuse")
    await press(env, "st:1:back")
    assert buttons(last_markup(env)) == ["st:1:measure", "st:1:no_answer", "st:1:refuse"]


async def test_simple_stages(env: Env):
    await take(env)
    await press(env, "st:1:no_answer")
    assert (await env.db.get_lead(1)).stage == stages.NO_ANSWER
    await press(env, f"st:1:at:{day_code(2)}10")
    await press(env, "st:1:thinking")
    assert (await env.db.get_lead(1)).stage == stages.THINKING
    await press(env, "st:1:contract")
    lead = await env.db.get_lead(1)
    assert lead.stage == stages.CONTRACT and lead.measure_at  # дата замера сохраняется для истории
    await env.tick()
    lists = {lst["name"]: lst["id"] for lst in env.trello.lists_}
    assert env.trello.cards["C1"]["idList"] == lists["Договор"]


async def test_stale_button_does_not_change_stage(env: Env):
    await take(env)
    await press(env, "st:1:no_answer")
    await press(env, f"st:1:at:{day_code(1)}12")
    await press(env, "st:1:contract")
    await press(env, "st:1:no_answer")  # кнопка со старой панели
    assert (await env.db.get_lead(1)).stage == stages.CONTRACT
    assert answers(env)[-1].show_alert and "договор" in answers(env)[-1].text
    assert buttons(env.session.edits()[-1].reply_markup) == ["st:1:reopen"]  # панель обновлена


async def test_stage_buttons_need_taken_lead_and_manager_chat(env: Env):
    await complete_dialog(env.client)
    await env.tick()
    notification = env.group()[0]
    await env.client.press_in_group(notification, "st:1:no_answer")
    assert (await env.db.get_lead(1)).stage is None
    assert answers(env)[-1].show_alert and "Взял в работу" in answers(env)[-1].text

    await env.client.press_in_group(notification, "take:1")
    await env.client.press_in_group(notification, "st:1:no_answer", chat=Chat(id=-999, type="group"))
    assert (await env.db.get_lead(1)).stage is None


@pytest.mark.parametrize("data", [
    "st:x:no_answer", "st:1", "st:1:fly", "st:1:at:garbage", "st:1:at:2026133014", "st:1:rsn:unknown",
    "st:1:day:2026", "st:99:no_answer",
    # Дата вне окна, которое предлагают кнопки (аудит run-2: «9999 год» ронял каждый тик планировщика).
    "st:1:at:9999123123", f"st:1:at:{day_code(DAYS_AHEAD + 3)}12", f"st:1:at:{day_code(-3)}12",
])
async def test_forged_stage_callbacks_change_nothing(env: Env, data: str):
    await take(env)
    await press(env, data)
    lead = await env.db.get_lead(1)
    assert lead.stage is None and lead.measure_at is None


async def test_stage_button_of_deleted_lead(env: Env):
    await take(env)
    p = panel(env)
    await env.db.delete_lead_data(1)
    await env.client.press_in_group(p, "st:1:no_answer")
    assert answers(env)[-1].show_alert and "удалена" in answers(env)[-1].text
    assert (await env.db.get_lead(1)).stage is None


async def test_order_status_for_client(env: Env):
    """Клиент в /order видит не только «взяли в работу», но и назначенный замер."""
    await take(env)
    await press(env, f"st:1:at:{day_code(1)}15")
    await env.client.text("/order")
    assert "замер назначен" in env.client.last_text() and "в 15:00" in env.client.last_text()
    assert (await env.db.last_lead(USER.id)).stage == stages.MEASURE


async def test_forged_far_day_shows_no_hours(env: Env):
    await take(env)
    await press(env, "st:1:measure")
    await press(env, "st:1:day:99991231")
    assert not [b for b in buttons(last_markup(env)) if ":at:" in b]  # часы на 9999 год не предлагаем


def test_measure_hour_outside_work_time_rejected():
    from datetime import time

    settings = Settings(bot_token="123:TEST", work_start=time(9), work_end=time(21))
    assert parse_moment(f"{day_code(1)}14", settings) is not None
    assert parse_moment(f"{day_code(1)}03", settings) is None  # такой кнопки не было
