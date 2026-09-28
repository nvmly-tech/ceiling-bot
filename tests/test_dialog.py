from datetime import time

import pytest
from aiogram import Bot

from app.bot import texts
from app.config import Settings
from app.main import build_dispatcher
from tests.conftest import USER, Client, FakeSession


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


async def test_restart_mid_dialog_continues(client: Client, db):
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("/start")
    assert client.last_text() == texts.Q_AREA
    assert client.session.sent()[-2].text == texts.CONTINUE
    assert (await db.last_lead(USER.id)).id == 1  # новый лид не создан


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
