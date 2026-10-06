"""/order: клиент смотрит свою заявку и правит поля."""

from datetime import UTC, datetime, timedelta

from app.bot import texts
from app.db import TG_CLIENT_MSG, Database
from app.stages import REFUSED
from tests.conftest import CHAT, USER, Client
from tests.test_notifier import Env, make_env
from tests.test_trello import complete_dialog


def buttons(client: Client) -> list[str]:
    markup = client.session.sent(CHAT.id)[-1].reply_markup
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_order_without_lead(client: Client):
    await client.text("/order")
    assert client.last_text() == texts.ORDER_NONE


async def test_order_shows_lead_with_edit_buttons(client: Client, db):
    await complete_dialog(client)
    await client.text("/order")
    view = client.last_text()
    assert view.startswith("📋 Заявка №1 — передана менеджеру")
    for line in ("Помещение: Квартира", "Площадь: 18,5", "Потолок: Матовый", "Телефон: +79001234567",
                 "Время замера: в субботу"):
        assert line in view
    assert buttons(client) == ["edit:object:1", "edit:area:1", "edit:ceiling_type:1", "edit:phone:1",
                               "edit:measure_time:1", "delete:ask:1", "edit:ok:1"]
    # Просмотр — не часть переписки: в базе (и в карточке) его нет.
    assert "/order" not in [m.text for m in await db.get_messages(1)]


async def test_edit_area_after_done(client: Client, db):
    await complete_dialog(client)
    await client.text("/order")
    await client.press("edit:area:1")
    assert client.last_text().startswith("Площадь: сейчас «18,5»")
    await client.text("примерно 30 метров")

    lead = await db.get_lead(1)
    assert (lead.area_m2, lead.area_text, lead.status) == (30.0, "примерно 30 метров", "qualified")
    edits = [m.text for m in await db.get_messages(1) if m.kind == "edit"]
    assert edits == ["✏️ Площадь: 18,5 → примерно 30 метров"]
    sent = [m.text for m in client.session.sent(CHAT.id)[-2:]]
    assert sent[0] == texts.EDIT_DONE.format(label="Площадь", value="примерно 30 метров")
    assert sent[1].startswith("📋 Заявка №1")  # обновлённая заявка — можно поправить ещё

    await client.text("спасибо")  # после правки — снова обычный режим «после анкеты»
    assert (await db.get_lead(1)).area_text == "примерно 30 метров"


async def test_edit_by_button_and_bad_phone_retry(client: Client, db):
    await complete_dialog(client)
    await client.text("/order")
    await client.press("edit:ceiling_type:1")
    await client.press("ct:fabric")
    assert (await db.get_lead(1)).ceiling_type == texts.CEILING_OPTIONS["fabric"]

    await client.press("edit:phone:1")
    await client.text("не помню")
    assert client.last_text() == texts.Q_PHONE_RETRY
    await client.text("8 912 345-67-89")
    assert (await db.get_lead(1)).phone == "+79123456789"


async def test_edit_during_questionnaire_returns_to_it(client: Client, db):
    await client.text("/start")
    await client.press("obj:flat")  # сейчас вопрос про площадь
    await client.text("/order")
    await client.press("edit:object:1")
    await client.press("obj:house")
    assert (await db.get_lead(1)).object == texts.OBJECT_OPTIONS["house"]
    assert client.last_text() == texts.Q_AREA  # анкета продолжается с того же места
    await client.text("20")
    assert client.last_text() == texts.Q_CEILING_TYPE


async def test_stale_or_foreign_edit_buttons_ignored(client: Client, db):
    await complete_dialog(client)
    await client.press("edit:area:7")  # чужая заявка
    await client.press("edit:hack:1")  # несуществующее поле
    assert (await db.get_lead(1)).area_text == "18,5"
    await client.text("и ещё карниз")  # режим правки не включился
    assert (await db.get_lead(1)).area_text == "18,5"


async def test_start_or_photo_during_edit(client: Client, db):
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("/order")
    await client.press("edit:object:1")
    await client.photo()  # не значение — просим прислать текстом, без падения
    assert client.last_text() == texts.EDIT_TEXT_ONLY
    await client.text("/start")  # выход из правки: незаконченная анкета — обычный выбор
    assert client.last_text() == texts.RESTART_CHOICE.format(lead_id=1)
    assert (await db.last_lead(USER.id)).id == 1


async def test_edit_ok_closes_view(client: Client):
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("/order")
    await client.press("edit:ok:1")
    assert client.last_text() == texts.Q_AREA  # анкета не закончена — продолжаем её


async def test_manager_sees_edit_but_not_questionnaire_again(db):
    env: Env = await make_env(db)
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)  # уведомление о заявке ушло
    await env.client.text("/order")
    await env.client.press("edit:measure_time:1")
    await env.client.text("в воскресенье утром")
    assert TG_CLIENT_MSG in [t.kind for t in await env.db.outbox_pending()]
    await env.tick(t0 + timedelta(seconds=61))
    [msg] = [m for m in env.group() if "дописал" in m.text]
    assert "• ✏️ Время замера: в субботу → в воскресенье утром" in msg.text
    assert "/start" not in msg.text and "Квартира" not in msg.text  # анкету менеджер уже видел


async def test_edit_phone_by_contact_and_time_by_voice(client: Client, db):
    from app.db import STT_TRANSCRIBE

    await complete_dialog(client)
    await client.text("/order")
    await client.press("edit:phone:1")
    await client.contact("+7 912 000-00-00")
    assert (await db.get_lead(1)).phone == "+79120000000"

    await client.press("edit:measure_time:1")
    await client.voice("voice-time")  # без Groq — расшифровка позже, в поле пока заглушка
    assert (await db.get_lead(1)).measure_time == texts.VOICE_PLACEHOLDER
    [task] = [t for t in await db.outbox_pending() if t.kind == STT_TRANSCRIBE]
    assert task.payload["field"] == "measure_time"  # расшифровка ляжет именно в правленое поле
    kinds = [m.kind for m in await db.get_messages(1)][-4:]
    assert kinds == ["contact", "edit", "voice", "edit"]  # контакт и голос — в переписке (вложение, расшифровка)


# --- удаление заявки ---


async def test_delete_asks_confirmation_and_no_keeps_lead(client: Client, db):
    await complete_dialog(client)
    await client.text("/order")
    assert "delete:ask:1" in buttons(client)
    await client.press("delete:ask:1")
    assert client.last_text() == texts.DELETE_CONFIRM.format(lead_id=1)
    assert buttons(client) == ["delete:yes:1", "delete:no:1"]
    await client.press("delete:no:1")
    assert client.last_text() == texts.DELETE_KEPT
    lead = await db.get_lead(1)
    assert lead.status == "qualified" and lead.phone == "+79001234567"


async def test_delete_wipes_data_card_and_tells_manager(db):
    env: Env = await make_env(db)
    await complete_dialog(env.client)
    t0 = datetime.now(UTC)
    await env.tick(t0)  # карточка создана, менеджер уведомлён
    assert env.trello.cards and env.group()
    await env.client.text("/order")
    await env.client.press("delete:ask:1")
    await env.client.press("delete:yes:1")
    assert env.client.last_text() == texts.DELETED.format(lead_id=1)

    lead = await db.get_lead(1)  # строка обезличена: остались номер и отметка «удалена»
    assert lead.status == "deleted" and lead.tg_user_id == 0 and lead.chat_id == 0
    assert (lead.name, lead.username, lead.phone, lead.object, lead.area_text, lead.ceiling_type,
            lead.measure_time, lead.summary) == (None,) * 8
    assert await db.get_messages(1) == []
    assert await db.last_lead(USER.id) is None

    await env.tick(t0 + timedelta(seconds=1))
    assert env.trello.cards == {}  # карточка удалена вместе с перепиской и вложениями
    assert (await db.get_lead(1)).trello_card_id is None
    notice = env.group()[-1].text
    assert "Заявка №1 удалена клиентом" in notice
    assert "Анна" not in notice and "79001234567" not in notice  # в уведомлении нет личных данных

    await env.client.text("/order")
    assert env.client.last_text() == texts.ORDER_NONE
    await env.client.text("/start")  # можно оставить новую
    assert (await db.last_lead(USER.id)).id == 2


# Что заявка узнаёт о клиенте после «Взял в работу»: при удалении стирается вместе с анкетой.
CLIENT_TRACES = {
    "source": "avito", "measure_at": "2026-10-07T11:00:00+00:00", "measure_reminded_for": "2026-10-07T11:00:00+00:00",
    "visit_answer": "move", "contact_asked_at": "2026-10-06T12:00:00+00:00", "contact_answer": "no",
    "rating_asked_at": "2026-10-07T15:00:00+00:00", "rating": 2, "refuse_reason": "price",
}


async def mark_full_cycle(db: Database, lead_id: int) -> None:
    """Заявка прошла весь путь: источник, замер и его перенос, вопросы клиенту, оценка, отказ с причиной."""
    cols = ", ".join(f"{k} = ?" for k in CLIENT_TRACES)
    await db.conn.execute(
        f"UPDATE leads SET {cols}, stage = ?, stage_by_name = ? WHERE id = ?",
        (*CLIENT_TRACES.values(), REFUSED, "Иван", lead_id),
    )
    await db.conn.commit()


def traces(lead) -> dict:
    return {k: getattr(lead, k) for k in CLIENT_TRACES}


async def test_delete_wipes_what_lead_learned_after_take(client: Client, db):
    await complete_dialog(client)
    await mark_full_cycle(db, 1)
    await db.delete_lead_data(1)
    lead = await db.get_lead(1)
    assert traces(lead) == dict.fromkeys(CLIENT_TRACES)
    # Работа менеджера остаётся — клиента по ней не узнать.
    assert (lead.stage, lead.stage_by_name) == (REFUSED, "Иван")


async def test_startup_wipes_leads_deleted_by_older_version(tmp_path):
    path = tmp_path / "bot.sqlite3"
    old = Database(path)
    await old.connect()
    lead = await old.create_lead(tg_user_id=5, chat_id=5, name="Анна", username=None, is_night=False)
    await mark_full_cycle(old, lead.id)
    # Прежняя версия стирала только анкету: этап, замер и ответы клиента оставались в строке.
    await old.conn.execute("UPDATE leads SET status = 'deleted', name = NULL WHERE id = ?", (lead.id,))
    await old.conn.commit()
    await old.close()

    db = Database(path)
    await db.connect()
    try:
        assert traces(await db.get_lead(lead.id)) == dict.fromkeys(CLIENT_TRACES)
    finally:
        await db.close()


async def test_delete_before_card_created_cancels_pending_tasks(db):
    env: Env = await make_env(db)
    await env.client.text("/start")
    await env.client.press("obj:flat")  # карточка и комментарии ещё в очереди
    await env.client.text("/order")
    await env.client.press("delete:ask:1")
    await env.client.press("delete:yes:1")
    await env.tick()
    await env.tick()
    assert env.trello.cards == {} and env.group() == []  # ни карточки, ни уведомления менеджеру
    assert [t.kind for t in await db.outbox_pending()] == []


async def test_stale_delete_buttons_ignored(client: Client, db):
    await complete_dialog(client)
    await client.press("delete:yes:7")  # чужая заявка
    await client.press("delete:yes:1")  # без подтверждения (кнопки «Да» не показывали) — тоже нельзя
    assert (await db.get_lead(1)).status == "qualified"


async def test_deleted_lead_not_in_reminders(db):
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    await db.update_lead(lead.id, status="qualified", notified_status="qualified",
                         notified_at="2026-09-29T10:00:00+00:00")
    await db.delete_lead_data(lead.id)
    assert await db.leads_waiting() == []
