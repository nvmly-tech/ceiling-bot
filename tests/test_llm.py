import asyncio
import json
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
import respx
from aiogram import Bot

from app.bot import texts
from app.bot.assistant import LeadAssistant, parse_summary, parse_turn
from app.bot.handlers import SCRIPT
from app.config import Settings
from app.db import TG_CLIENT_MSG, Database
from app.main import build_dispatcher
from app.services.llm import COOLDOWN, FAIL_THRESHOLD, LLMError, LLMProvider, LLMRouter, parse_json
from app.services.notifier import Notifier
from app.services.outbox import Outbox
from tests.conftest import USER, Client, FakeSession
from tests.test_trello import FakeTrello, make_sync


class FakeProvider(LLMProvider):
    """Модель в памяти: отвечает по очереди из answers (dict — JSON, Exception — ошибка, callable — от messages)."""

    def __init__(self, label: str, *answers, timeout: float = 5, delay: float = 0):
        self.label, self.model, self.timeout, self.extra = label, label, timeout, {}
        self.answers = list(answers)
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def chat_json(self, messages, *, max_tokens=700, temperature=0.4):
        self.calls.append(messages)
        await asyncio.sleep(self.delay)
        if not self.answers:
            raise LLMError(f"{self.label}: HTTP 503")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(messages) if callable(answer) else answer

    async def close(self):
        pass


def turn(reply: str, asks: str | None = None, **fields) -> dict:
    return {"reply": reply, "asks": asks, "fields": fields}


def ok(value):
    return value


# --- маршрутизатор ---


async def test_router_uses_primary_then_fallback():
    primary = FakeProvider("deepseek", {"a": 1}, LLMError("deepseek: HTTP 500"))
    fallback = FakeProvider("groq", {"a": 2})
    router = LLMRouter([primary, fallback])
    assert await router.json([], ok) == ({"a": 1}, "deepseek")
    assert await router.json([], ok) == ({"a": 2}, "groq")
    assert router.health["deepseek"].failures == 1


async def test_router_invalid_answer_goes_to_next_model():
    router = LLMRouter([FakeProvider("deepseek", {"reply": ""}), FakeProvider("groq", turn("Здравствуйте!"))])
    result, model = await router.json([], parse_turn)
    assert (result.reply, model) == ("Здравствуйте!", "groq")


async def test_router_timeout_goes_to_next_model():
    router = LLMRouter([FakeProvider("deepseek", {"a": 1}, timeout=0.05, delay=0.3), FakeProvider("groq", {"a": 2})])
    assert await router.json([], ok) == ({"a": 2}, "groq")


async def test_router_all_failed():
    router = LLMRouter([FakeProvider("deepseek"), FakeProvider("groq")])
    with pytest.raises(LLMError, match="deepseek.*groq"):
        await router.json([], ok)


async def test_circuit_breaker_skips_and_recovers():
    events = []
    primary = FakeProvider("deepseek", *[LLMError("down")] * FAIL_THRESHOLD, {"a": "back"})
    fallback = FakeProvider("groq", *[{"a": "fb"}] * 10)
    router = LLMRouter([primary, fallback], on_status_change=lambda *e: events.append(e[:2]))
    now = datetime.now(UTC)
    for _ in range(FAIL_THRESHOLD):
        await router.json([], ok, now=now)
    assert events == [("deepseek", False)]

    calls = len(primary.calls)
    assert await router.json([], ok, now=now + timedelta(minutes=1)) == ({"a": "fb"}, "groq")
    assert len(primary.calls) == calls  # отключённую модель не дёргаем

    assert await router.json([], ok, now=now + COOLDOWN + timedelta(seconds=1)) == ({"a": "back"}, "deepseek")
    assert events == [("deepseek", False), ("deepseek", True)]


async def test_format_errors_do_not_disable_model():
    from app.services.llm import FormatError

    primary = FakeProvider("deepseek", *[FormatError("deepseek: ответ не JSON")] * 5, {"a": "json"})
    fallback = FakeProvider("groq", *[{"a": "fb"}] * 5)
    router = LLMRouter([primary, fallback])
    for _ in range(5):
        assert await router.json([], ok) == ({"a": "fb"}, "groq")
    assert not router.health["deepseek"].is_down and router.health["deepseek"].failures == 0
    assert await router.json([], ok) == ({"a": "json"}, "deepseek")


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("Конечно! Вот ответ")
    with pytest.raises(LLMError):
        parse_json("[1, 2]")


@respx.mock
async def test_provider_http():
    route = respx.post("https://router.cheap/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": '{"reply": "ok"}'}}]})
    )
    p = LLMProvider("deepseek", "https://router.cheap/v1", "SECRET", "deepseek-v4.1-flash", extra={"x": 1})
    assert await p.chat_json([{"role": "user", "content": "hi"}]) == {"reply": "ok"}
    req = route.calls.last.request
    body = json.loads(req.read())
    assert body["model"] == "deepseek-v4.1-flash" and body["response_format"] == {"type": "json_object"}
    assert body["x"] == 1 and req.headers["Authorization"] == "Bearer SECRET"

    route.mock(return_value=httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError) as e:
        await p.chat_json([])
    assert "401" in str(e.value) and "SECRET" not in str(e.value)
    await p.close()


# --- разбор ответа модели ---


def test_parse_turn_normalizes_fields():
    t = parse_turn(turn("Отлично!", object="двушка", area_m2="40,5", ceiling_type="глянец",
                        phone="8 (900) 123-45-67", measure_time="в субботу"))
    assert t.updates == {"object": "двушка", "area_m2": 40.5, "area_text": "40.5 м²", "ceiling_type": "глянец",
                         "phone": "+79001234567", "measure_time": "в субботу"}
    area = parse_turn(turn("ок", area_text="около 20 метров")).updates
    assert area == {"area_m2": 20.0, "area_text": "около 20 метров"}
    assert parse_turn(turn("ок", phone="позвоните", area_m2=99999)).updates == {}
    assert parse_turn(turn("ок", phone_refused=True)).updates == {"phone": texts.NO_PHONE_VALUE}
    assert parse_turn(turn("ок", object="null", ceiling_type="")).updates == {}
    with pytest.raises(ValueError):
        parse_turn({"reply": "   "})
    with pytest.raises(ValueError):
        parse_turn({"reply": "ок", "fields": "всё"})


def test_parse_summary():
    s = parse_summary({"summary": "Кухня 12 м²", "hotness": "Теплый", "reason": "нет телефона"})
    assert (s.hotness, s.reason) == ("тёплый", "нет телефона")
    with pytest.raises(ValueError):
        parse_summary({"summary": "x", "hotness": "очень горячий"})


# --- диалог ---


def make_client(db: Database, *providers: FakeProvider) -> tuple[Client, FakeSession]:
    settings = Settings(bot_token="123:TEST", work_start=time(0), work_end=time(23, 59, 59))
    session = FakeSession()
    assistant = LeadAssistant(LLMRouter(list(providers))) if providers else None
    client = Client(build_dispatcher(db, settings, None, None, assistant), Bot("123:TEST", session=session), session)
    return client, session


async def test_first_message_several_fields_at_once(db):
    ds = FakeProvider("deepseek (router.cheap)", turn(
        "Глянец в двушку на 40 м² — отличный выбор. Оставите номер телефона для замера?",
        object="двушка", area_m2=40, ceiling_type="глянцевый"))
    client, session = make_client(db, ds)
    await client.text("Здравствуйте! Нужен глянец в двушку, метров 40")

    lead = await db.last_lead(USER.id)
    assert (lead.object, lead.area_m2, lead.ceiling_type) == ("двушка", 40.0, "глянцевый")
    sent = session.sent()
    assert sent[0].text.startswith("Здравствуйте, Анна!")  # приветствие — скрипт
    assert sent[1].text == "Глянец в двушку на 40 м² — отличный выбор. Оставите номер телефона для замера?"
    assert sent[1].reply_markup.keyboard[0][0].request_contact  # клавиатура под следующий вопрос — телефон

    system = ds.calls[0][0]["content"]
    assert "Следующий вопрос — про object" in system and "Факты о студии" in system
    assert ds.calls[0][-2] == {"role": "user", "content": "Здравствуйте! Нужен глянец в двушку, метров 40"}
    assert ds.calls[0][-1]["role"] == "system" and "JSON" in ds.calls[0][-1]["content"]  # напоминание о формате
    assert [m["role"] for m in ds.calls[0][1:-2]] == ["assistant"]  # приветствие — до сообщения клиента

    msgs = await db.get_messages(lead.id)
    assert [(m.direction, m.model) for m in msgs] == [("in", None), ("out", SCRIPT), ("out", "deepseek (router.cheap)")]


async def test_off_topic_question_then_script_after_two_stalls(db):
    ds = FakeProvider("deepseek",
                      turn("Точную цену посчитает мастер на бесплатном замере. Где нужен потолок?"),
                      turn("Понимаю. Подскажите, где нужен потолок?"))
    client, session = make_client(db, ds)
    await client.text("/start")
    await client.text("а сколько стоит метр?")
    assert client.last_text().startswith("Точную цену посчитает мастер")
    await client.text("ну просто скажите цену")
    assert len(ds.calls) == 2

    # Третий раз LLM не зовём: вопрос ведёт скрипт — принимает ответ как есть и идёт дальше.
    await client.text("ладно, у меня кухня")
    assert len(ds.calls) == 2
    assert client.last_text() == texts.Q_AREA
    assert (await db.last_lead(USER.id)).object == "ладно, у меня кухня"


async def test_state_follows_question_asked_by_model(db):
    # Модель пропустила «где потолок» и спросила про тип — кнопки должны быть под тип потолка.
    ds = FakeProvider("deepseek", turn("А какой потолок хотите?", asks="ceiling_type", area_m2=14))
    client, session = make_client(db, ds)
    await client.text("/start")
    await client.text("метров 14")
    buttons = [b.callback_data for row in session.sent()[-1].reply_markup.inline_keyboard for b in row]
    assert buttons[0].startswith("ct:")
    await client.press("ct:matte")
    assert client.last_text() == texts.Q_OBJECT  # потом бот вернётся к пропущенному вопросу


async def test_progress_on_other_field_is_not_a_stall(db):
    ds = FakeProvider("deepseek",
                      turn("Где нужен потолок?", asks="object", area_m2=14),
                      turn("Где нужен потолок?", asks="object", ceiling_type="матовый"),
                      turn("Оставите телефон?", asks="phone", object="спальня"))
    client, _ = make_client(db, ds)
    await client.text("/start")
    for text in ("14 метров", "матовый", "в спальню"):
        await client.text(text)
    assert len(ds.calls) == 3  # скрипт не перехватывал: каждый раз что-то извлекалось
    assert (await db.last_lead(USER.id)).object == "спальня"


async def test_completion_reply_with_question_is_not_sent(db):
    # Анкета собрана, а модель зачем-то задаёт вопрос — клиенту уходит только подтверждение заявки.
    ds = FakeProvider("deepseek", turn("Где будет потолок?", asks="object", measure_time="в субботу"))
    client, session = make_client(db, ds)
    await client.text("/start")
    await client.press("obj:flat")
    await client.press("area:lt15")
    await client.press("ct:matte")
    await client.contact("+79001234567")
    await client.text("в субботу. замер бесплатный?")
    assert "Где будет потолок?" not in [m.text for m in session.sent()]
    assert "Заявка №1 принята" in client.last_text()


async def test_all_models_down_falls_back_to_script(db):
    client, session = make_client(db, FakeProvider("deepseek"), FakeProvider("groq"))
    await client.text("/start")
    await client.press("obj:flat")
    await client.text("около 20")
    lead = await db.last_lead(USER.id)
    assert lead.area_m2 == 20.0
    assert client.last_text() == texts.Q_CEILING_TYPE
    ins = [m for m in await db.get_messages(lead.id) if m.direction == "in"]
    assert [m.text for m in ins].count("около 20") == 1  # без дублей после SkipHandler


async def test_fallback_model_signed_in_history(db):
    fallback = FakeProvider("groq: gpt-oss", turn("Какая площадь?", object="дом"))
    client, _ = make_client(db, FakeProvider("deepseek"), fallback)
    await client.text("/start")
    await client.text("частный дом")
    out = [m for m in await db.get_messages(1) if m.direction == "out"]
    assert out[-1].model == "groq: gpt-oss" and out[-1].text == "Какая площадь?"


async def test_buttons_skip_fields_filled_by_llm(db):
    ds = FakeProvider("deepseek", turn("Где нужен потолок?", area_m2=18, ceiling_type="матовый"))
    client, _ = make_client(db, ds)
    await client.text("/start")
    await client.text("матовый, 18 метров")
    await client.press("obj:flat")
    assert client.last_text() == texts.Q_PHONE  # площадь и тип уже известны — сразу телефон


async def test_completion_via_llm(db):
    ds = FakeProvider("deepseek", turn("Спасибо!", measure_time="в пятницу вечером"),
                      turn("Да, замер бесплатный. Спасибо!", measure_time="завтра"))
    client, session = make_client(db, ds)
    for step in ("/start",):
        await client.text(step)
    await client.press("obj:flat")
    await client.press("area:lt15")
    await client.press("ct:matte")
    await client.contact("+79001234567")
    await client.text("в пятницу вечером")
    lead = await db.last_lead(USER.id)
    assert lead.status == "qualified" and lead.measure_time == "в пятницу вечером"
    assert "Заявка №1 принята" in client.last_text()
    assert "Спасибо!" not in [m.text for m in session.sent()]  # без вопроса клиента лишний ответ не шлём


async def test_completion_with_question_sends_llm_answer(db):
    ds = FakeProvider("deepseek", turn("Да, замер бесплатный.", measure_time="завтра"))
    client, session = make_client(db, ds)
    await client.text("/start")
    await client.press("obj:flat")
    await client.press("area:lt15")
    await client.press("ct:matte")
    await client.contact("+79001234567")
    await client.text("завтра можно? замер же бесплатный?")
    texts_sent = [m.text for m in session.sent()]
    assert texts_sent[-2] == "Да, замер бесплатный." and "Заявка №1 принята" in texts_sent[-1]


async def test_no_phone_button_and_commands_bypass_llm(db):
    ds = FakeProvider("deepseek", turn("Какая площадь?", object="квартира"))
    client, _ = make_client(db, ds)
    await client.text("/start")
    await client.text("квартира")
    await client.press("area:lt15")
    await client.press("ct:matte")
    await client.text(texts.NO_PHONE)
    assert len(ds.calls) == 1
    assert (await db.last_lead(USER.id)).phone == texts.NO_PHONE_VALUE


async def test_after_done_llm_answers_and_updates_fields(db):
    ds = FakeProvider("deepseek")  # пока ответов нет — анкету закончит скрипт
    client, _ = make_client(db, ds)
    await client.text("/start")
    await client.press("obj:flat")
    await client.press("area:lt15")
    await client.press("ct:matte")
    await client.contact("+79001234567")
    await client.text("завтра")  # LLM отвечает ошибкой → скрипт завершает анкету
    ds.answers.append(turn("Записал новый номер, менеджер позвонит на него.", phone="+7 911 000-00-00"))
    await client.text("звоните лучше на +7 911 000-00-00")
    lead = await db.last_lead(USER.id)
    assert lead.phone == "+79110000000"
    assert client.last_text() == "Записал новый номер, менеджер позвонит на него."
    assert "заявка №1 передана менеджеру" in ds.calls[-1][0]["content"]
    assert TG_CLIENT_MSG in [t.kind for t in await db.outbox_pending()]


# --- резюме ---


SUMMARY = {"summary": "Кухня 12 м², хочет глянец с подсветкой, спрашивал про сроки.", "hotness": "горячий",
           "reason": "оставил телефон и время замера"}


async def notify_env(db: Database, *providers: FakeProvider):
    settings = Settings(bot_token="123:TEST", manager_chat_id=-5000, work_start=time(0), work_end=time(23, 59, 59))
    session = FakeSession()
    bot = Bot("123:TEST", session=session)
    assistant = LeadAssistant(LLMRouter(list(providers)))
    notifier = Notifier(bot, db, settings, trello_enabled=True, assistant=assistant)
    fake = FakeTrello()
    outbox = Outbox(db, {**make_sync(db, fake).handlers, **notifier.handlers})
    return notifier, outbox, session, fake


async def qualified_lead(db: Database, status: str = "qualified"):
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="Анна", username=None, is_night=False)
    await db.add_message(lead.id, direction="in", kind="text", text="кухня 12 м², глянец с подсветкой, когда сделаете?")
    return await db.update_lead(lead.id, status=status, object="Квартира", phone="+79001234567")


async def run_all(notifier, outbox, now=None):
    await notifier.scan(now or datetime.now(UTC))
    for _ in range(3):
        await outbox.run_once(now or datetime.now(UTC))  # время — после scan, см. Env.tick в test_notifier


async def test_summary_in_notification_and_card(db):
    ds = FakeProvider("deepseek (router.cheap)", SUMMARY)
    notifier, outbox, session, fake = await notify_env(db, ds)
    await qualified_lead(db)
    await run_all(notifier, outbox)

    msg = session.sent(-5000)[0].text
    assert "🔥 <b>горячий</b> — оставил телефон и время замера" in msg
    assert "📝 Кухня 12 м², хочет глянец с подсветкой" in msg
    assert "<i>— резюме: deepseek (router.cheap)</i>" in msg
    assert "Клиент: кухня 12 м²" in ds.calls[0][1]["content"]  # модель видела переписку
    assert "не угадывай пол по имени" in ds.calls[0][0]["content"]

    card = fake.cards["C1"]
    assert card["name"].startswith("🔥 №1 · ")
    assert "**Оценка:** 🔥 горячий — оставил телефон и время замера" in card["desc"]
    assert "_— резюме: модель deepseek (router.cheap)_" in card["desc"]


async def test_notification_without_summary_when_llm_down(db):
    notifier, outbox, session, _ = await notify_env(db, FakeProvider("deepseek"), FakeProvider("groq"))
    await qualified_lead(db)
    await run_all(notifier, outbox)
    msg = session.sent(-5000)[0].text
    assert "Новая заявка №1" in msg and "резюме" not in msg


async def test_summary_regenerated_on_status_change(db):
    ds = FakeProvider("deepseek", {**SUMMARY, "hotness": "холодный"}, SUMMARY)
    notifier, outbox, session, _ = await notify_env(db, ds)
    lead = await qualified_lead(db, status="abandoned")
    now = datetime.now(UTC)
    await run_all(notifier, outbox, now)
    assert (await db.get_lead(lead.id)).hotness == "холодный"

    await db.update_lead(lead.id, status="qualified")
    await run_all(notifier, outbox, now + timedelta(seconds=1))
    lead = await db.get_lead(lead.id)
    assert (lead.hotness, lead.summary_status) == ("горячий", "qualified")
    assert len(ds.calls) == 2


async def test_hanging_llm_does_not_hold_the_queue(db, monkeypatch):
    """Обе модели висят — уведомление уходит без резюме в пределах бюджета, очередь не стоит 2×таймаут."""
    import time as _time

    from app.services import notifier as notifier_mod

    monkeypatch.setattr(notifier_mod, "SUMMARY_BUDGET", 0.2)
    slow = [FakeProvider("deepseek", SUMMARY, timeout=5, delay=5), FakeProvider("groq", SUMMARY, timeout=5, delay=5)]
    notifier, outbox, session, _ = await notify_env(db, *slow)
    await qualified_lead(db)
    t0 = _time.monotonic()
    await run_all(notifier, outbox)
    assert _time.monotonic() - t0 < 2  # без бюджета было бы ~10 с (2 модели × 5 с)
    msg = session.sent(-5000)[0].text
    assert "Новая заявка №1" in msg and "резюме" not in msg


def test_studio_facts_give_price_guidance_not_commitments():
    from app.bot import prompts

    system = prompts.dialog_system({}, ["object"], done=False, lead_id=1, eta="завтра в 9:00")
    # Ориентиры по рынку (синтетика до реальных цен студии): цены «от», сроки, гарантия.
    for fact in ("от 500 ₽/м²", "от 1 700 ₽/м²", "2–5 дней", "2–4 часа", "10–15 лет"):
        assert fact in system
    # Правило: только как ориентир, точная сумма — на замере; итог заказа не считать.
    assert "ориентир" in system and "бесплатном замере" in system and "итоговую сумму" in system
    # Скидки, рассрочка и оплата — обязательства студии, бот их не обещает.
    assert "рассрочк" not in prompts.STUDIO_FACTS and "предоплат" not in prompts.STUDIO_FACTS
    assert "бот не называет" not in prompts.STUDIO_FACTS


@pytest.mark.parametrize("reply", [
    "Матовый в спальню 12 м² обойдётся ориентировочно от 6 000 ₽, точнее — на замере.",
    "Тканевый с линиями будет стоить от ≈ 34 000 ₽.",
    "Выйдет примерно 9250 рублей.",
    "Скидка 3 000 руб. при заказе сегодня!",
])
def test_reply_with_price_not_from_facts_is_rejected(reply):
    # Резервная модель сама перемножала цену на площадь — такую «смету» клиенту не показываем.
    with pytest.raises(ValueError, match="сумма не из фактов"):
        parse_turn(turn(reply))


def test_reply_with_prices_from_facts_passes():
    reply = "Матовый — от 500 ₽/м², тканевый — от 1 700 ₽/м², световые линии — от 3 700 ₽ за погонный метр."
    assert parse_turn(turn(reply)).reply == reply


async def test_made_up_price_goes_to_next_model():
    router = LLMRouter([FakeProvider("deepseek", turn("Итого от 34 000 ₽.")),
                        FakeProvider("groq", turn("Тканевый — от 1 700 ₽/м², точнее — на замере."))])
    result, model = await router.json([{"role": "user", "content": "сколько?"}], parse_turn)
    assert model == "groq" and "1 700" in result.reply
