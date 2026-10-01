from datetime import time

import pytest
from aiogram import Bot

from app.bot import texts
from app.config import Settings
from app.main import build_dispatcher
from tests.conftest import CHAT, USER, Client, FakeSession
from tests.test_trello import complete_dialog


async def test_full_flow_buttons(client: Client, db):
    await client.text("/start")
    assert client.last_text() == texts.Q_OBJECT

    await client.press("obj:flat")
    assert client.last_text() == texts.Q_AREA
    await client.press("area:15_30")
    assert client.last_text() == texts.Q_CEILING_TYPE
    await client.press("ct:matte")
    assert client.last_text() == texts.Q_PHONE
    await client.contact("79001234567")
    assert client.last_text() == texts.Q_MEASURE_TIME
    await client.text("в субботу после обеда")
    assert "Заявка №1 принята" in client.last_text()

    lead = await db.last_lead(USER.id)
    assert (lead.object, lead.area_text, lead.ceiling_type) == ("Квартира", "15–30 м²", "Матовый")
    assert lead.phone == "+79001234567"
    assert lead.measure_time == "в субботу после обеда"
    assert lead.status == "qualified" and lead.completed_at
    assert lead.name == "Анна Петрова"

    msgs = await db.get_messages(lead.id)
    ins = [m for m in msgs if m.direction == "in"]
    outs = [m for m in msgs if m.direction == "out"]
    assert [m.kind for m in ins] == ["text", "button", "button", "button", "contact", "text"]
    assert all(m.model == "script" for m in outs)
    assert outs[-1].text == client.last_text()


async def test_free_text_and_voice(client: Client, db):
    await client.text("Здравствуйте, сколько стоит потолок?")  # без /start
    assert client.last_text() == texts.Q_OBJECT
    await client.text("двушка")
    await client.text("не знаю")  # площадь не распознана → переспросить один раз
    assert client.last_text() == texts.Q_AREA_RETRY
    await client.text("около 40 квадратов")
    await client.voice()
    await client.text("позвоните мне")  # не номер
    assert client.last_text() == texts.Q_PHONE_RETRY
    await client.text("8 (900) 123-45-67")
    await client.voice("voice-2")

    lead = await db.last_lead(USER.id)
    assert lead.object == "двушка"
    assert lead.area_m2 == 40.0
    assert lead.ceiling_type == texts.VOICE_PLACEHOLDER
    assert lead.phone == "+79001234567"
    assert lead.status == "qualified"
    msgs = await db.get_messages(lead.id)
    assert msgs[0].text == "Здравствуйте, сколько стоит потолок?"
    assert [m.file_id for m in msgs if m.kind == "voice"] == ["voice-1", "voice-2"]


async def test_area_accepted_after_one_retry(client: Client, db):
    await client.text("/start")
    await client.press("obj:house")
    await client.text("не знаю")
    await client.text("всё ещё не знаю")
    assert client.last_text() == texts.Q_CEILING_TYPE
    lead = await db.last_lead(USER.id)
    assert lead.area_m2 is None and lead.area_text == "всё ещё не знаю"


async def test_no_phone_button(client: Client, db):
    await client.text("/start")
    await client.press("obj:office")
    await client.press("area:gt60")
    await client.press("ct:unsure")
    await client.text(texts.NO_PHONE)
    assert client.last_text() == texts.Q_MEASURE_TIME
    assert (await db.last_lead(USER.id)).phone == texts.NO_PHONE_VALUE


async def test_restart_mid_dialog_does_not_create_lead_by_itself(client: Client, db):
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("/start")  # без выбора клиента новая заявка не заводится
    assert client.last_text() == texts.RESTART_CHOICE.format(lead_id=1)
    assert (await db.last_lead(USER.id)).id == 1


async def test_after_done_ack_once_and_new_lead_on_start(client: Client, db):
    await client.text("/start")
    await client.press("obj:flat")
    await client.press("area:lt15")
    await client.press("ct:glossy")
    await client.contact("+79001234567")
    await client.text("завтра")
    await client.text("ещё хочу подсветку")
    assert client.last_text() == texts.AFTER_DONE_ACK
    n = len(client.session.sent())
    await client.text("и карниз")
    assert len(client.session.sent()) == n  # второй раз не отвечаем
    msgs = await db.get_messages(1)
    assert msgs[-1].text == "и карниз"

    await client.text("/start")
    assert (await db.last_lead(USER.id)).id == 2


async def test_stale_button_ignored(client: Client, db):
    await client.text("/start")
    await client.press("obj:flat")
    await client.press("obj:house")  # повторное нажатие на старый вопрос
    assert client.last_text() == texts.Q_AREA
    assert (await db.last_lead(USER.id)).object == "Квартира"


async def test_night_greeting(db):
    # Рабочее окно, в которое «сейчас» никогда не попадает.
    settings = Settings(bot_token="123:TEST", work_start=time(0, 0), work_end=time(0, 0))
    session = FakeSession()
    client = Client(build_dispatcher(db, settings), Bot("123:TEST", session=session), session)
    await client.text("/start")
    greeting = session.sent()[-2].text
    assert "нерабочее время" in greeting and "завтра в 0:00" in greeting
    assert (await db.last_lead(USER.id)).is_night


def test_greeting_promises_four_questions_as_in_spec():
    # ТЗ: квалификация на 3–4 вопроса. Нумерованных вопросов 4, телефон и время замера — в одном,
    # уточнение времени (если прислали только номер) — без номера, это не отдельный вопрос анкеты.
    questions = [texts.Q_OBJECT, texts.Q_AREA, texts.Q_CEILING_TYPE, texts.Q_PHONE]
    assert all(q.startswith(f"{i}/4. ") for i, q in enumerate(questions, 1))
    assert "замер" in texts.Q_PHONE and not texts.Q_MEASURE_TIME[0].isdigit()
    assert "4 коротких вопроса" in texts.GREETING_DAY and "4 коротких вопроса" in texts.GREETING_NIGHT


async def test_phone_and_time_in_one_answer(client: Client, db):
    await client.text("/start")
    for button in ("obj:flat", "area:15_30", "ct:matte"):
        await client.press(button)
    await client.text("мой номер 8 912 345-67-89, удобно в субботу после обеда")
    assert "Заявка №1 принята" in client.last_text()  # время уже есть — не переспрашиваем
    lead = await db.last_lead(USER.id)
    assert lead.phone == "+79123456789" and lead.measure_time == "удобно в субботу после обеда"


async def test_phone_without_time_asks_time(client: Client, db):
    await client.text("/start")
    for button in ("obj:flat", "area:15_30", "ct:matte"):
        await client.press(button)
    await client.text("мой номер 8 912 345-67-89")  # «мой номер» — не время замера
    assert client.last_text() == texts.Q_MEASURE_TIME
    assert (await db.last_lead(USER.id)).measure_time is None


@pytest.mark.parametrize(("rest", "expected"), [
    (" , завтра в 18:00", "завтра в 18:00"),
    ("мой номер  , удобно в выходные", "удобно в выходные"),
    ("звоните после 18", "после 18"),
    ("телефон ", None),
    ("мой номер", None),
    (" спасибо", None),
])
def test_measure_time_from_rest(rest, expected):
    from app.bot.handlers import measure_time_from

    assert measure_time_from(rest) == expected


# --- /start посреди анкеты: продолжить или начать новую ---


async def start_and_answer_object(client: Client) -> None:
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("/start")


async def test_restart_offers_continue_or_new(client: Client, db):
    await start_and_answer_object(client)
    last = client.session.sent(CHAT.id)[-1]
    assert last.text == texts.RESTART_CHOICE.format(lead_id=1)
    assert [b.callback_data for b in last.reply_markup.inline_keyboard[0]] == ["restart:continue:1", "restart:new:1"]


async def test_restart_continue_keeps_questionnaire(client: Client, db):
    await start_and_answer_object(client)
    await client.press("restart:continue:1")
    assert [m.text for m in client.session.sent(CHAT.id)[-2:]] == [texts.CONTINUE, texts.Q_AREA]
    assert (await db.last_lead(USER.id)).id == 1


async def test_restart_new_closes_previous_lead(client: Client, db):
    await start_and_answer_object(client)
    await client.press("restart:new:1")

    old, new = await db.get_lead(1), await db.last_lead(USER.id)
    assert old.status == "cancelled" and new.id == 2 and new.status == "new" and new.object is None
    assert client.last_text() == texts.Q_OBJECT
    sent = [m.text for m in client.session.sent(CHAT.id)]
    assert texts.RESTART_CLOSED.format(lead_id=1) in sent
    # Выбор клиента виден в переписке старой заявки (и в карточке Trello), новая начинается с него же.
    assert texts.RESTART_NEW in [m.text for m in await db.get_messages(1) if m.kind == "button"]

    await client.press("obj:house")  # ответы идут уже в новую заявку
    assert (await db.get_lead(2)).object == texts.OBJECT_OPTIONS["house"] and (await db.get_lead(1)).object


async def test_restart_new_respects_daily_limit(db):
    settings = Settings(bot_token="123:TEST", work_start=time(0), work_end=time(23, 59, 59))
    session = FakeSession()
    client = Client(build_dispatcher(db, settings), Bot("123:TEST", session=session), session)
    for _ in range(2):
        await complete_dialog(client)
    await start_and_answer_object(client)  # третья заявка за сутки — не закончена
    await client.press("restart:new:3")
    assert client.session.sent(CHAT.id)[-2].text == texts.RESTART_LIMIT.format(limit=3, lead_id=3)
    assert client.last_text() == texts.Q_AREA
    assert (await db.get_lead(3)).status == "new" and (await db.last_lead(USER.id)).id == 3


async def test_stale_restart_button_is_ignored(client: Client, db):
    await start_and_answer_object(client)
    await client.press("restart:new:1")
    await client.press("restart:new:1")  # повторное нажатие старой кнопки
    await client.press("restart:new:abc")  # подделанный callback
    assert (await db.last_lead(USER.id)).id == 2


async def test_cancelled_lead_gets_no_reminders(db):
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    await db.update_lead(lead.id, status="abandoned", notified_status="abandoned",
                         notified_at="2026-09-29T10:00:00+00:00")
    assert [x.id for x in await db.leads_waiting()] == [lead.id]
    await db.update_lead(lead.id, status="cancelled")
    assert await db.leads_waiting() == [] and await db.leads_to_notify() == []


async def test_phone_dictated_in_words_with_measure_time(client: Client, db):
    """Номер словами — так его пишут и так его отдаёт расшифровка голосового — и время замера одной фразой."""
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("20")
    await client.press("ct:matte")
    await client.text("мой номер восемь девятьсот двенадцать триста сорок пять шестьдесят семь восемьдесят девять, "
                      "в субботу после обеда")
    lead = await db.last_lead(USER.id)
    assert lead.phone == "+79123456789" and lead.measure_time == "в субботу после обеда"
    assert lead.status == "qualified"
