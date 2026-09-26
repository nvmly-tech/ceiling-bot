import asyncio
import json
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
import respx
from aiogram import Bot
from aiogram.methods import SendChatAction

from app.bot import handlers, texts
from app.config import Settings
from app.db import STT_TRANSCRIBE, Database
from app.main import build_dispatcher
from app.services.outbox import Outbox
from app.services.stt import (
    GIVE_UP_ATTEMPTS,
    UNINTELLIGIBLE,
    GroqTranscriber,
    SpeechService,
    SttError,
    clean_transcript,
)
from app.services.tgfiles import download
from tests.conftest import USER, Client, FakeSession
from tests.test_trello import FakeTrello, make_sync


class FakeTranscriber:
    """Groq в памяти: отдаёт тексты по очереди; Exception в очереди — ошибка; up=False — Groq лежит."""

    def __init__(self, *answers: str | Exception, delay: float = 0):
        self.answers = list(answers)
        self.delay = delay
        self.up = True
        self.calls: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        self.calls.append((audio, filename))
        await asyncio.sleep(self.delay)
        if not self.up:
            raise SttError("Groq: HTTP 503")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self) -> None:
        pass


class Env:
    def __init__(self, db: Database, transcriber: FakeTranscriber | None):
        settings = Settings(bot_token="123:TEST", work_start=time(0), work_end=time(23, 59, 59))
        self.db = db
        self.session = FakeSession()
        bot = Bot("123:TEST", session=self.session)

        async def fetch(file_id: str):
            return await download(bot, file_id)

        self.transcriber = transcriber
        self.stt = SpeechService(db, transcriber, fetch) if transcriber else None
        self.trello = FakeTrello()
        sync = make_sync(db, self.trello)
        sync.fetch_file = fetch
        self.outbox = Outbox(db, {**sync.handlers, **(self.stt.handlers if self.stt else {})})
        self.client = Client(build_dispatcher(db, settings, None, self.stt), bot, self.session)

    async def run(self, now: datetime | None = None) -> None:
        """Два прохода очереди: задачи, поставленные во время первого, выполняются во втором
        (в работе бота второй проход начинается сразу — по сигналу о новой задаче)."""
        for _ in range(2):
            await self.outbox.run_once(now or datetime.now(UTC))

    async def to_area(self) -> None:
        await self.client.text("/start")
        await self.client.press("obj:flat")


# --- расшифровка сразу ---


async def test_voice_answers_are_transcribed_and_parsed(db):
    env = Env(db, FakeTranscriber("около двадцати пяти, 25 метров", "глянцевый с подсветкой",
                                  "8 900 123 45 67", "в субботу после обеда"))
    await env.to_area()
    for i in range(4):
        await env.client.voice(f"voice-{i}")

    lead = await db.last_lead(USER.id)
    assert lead.area_m2 == 25.0 and lead.area_text == "около двадцати пяти, 25 метров"
    assert lead.ceiling_type == "глянцевый с подсветкой"
    assert lead.phone == "+79001234567"
    assert lead.measure_time == "в субботу после обеда"
    assert lead.status == "qualified"

    assert any(isinstance(c, SendChatAction) for c in env.session.calls)  # «печатает…» пока ждём Groq
    assert env.transcriber.calls[0] == (b"FILE:voice-0.oga", "voice.ogg")
    assert not [t for t in await db.outbox_pending() if t.kind == STT_TRANSCRIBE]

    voices = [m for m in await db.get_messages(lead.id) if m.kind == "voice"]
    assert [m.text for m in voices][:2] == ["около двадцати пяти, 25 метров", "глянцевый с подсветкой"]


async def test_unparseable_voice_area_asks_again(db):
    env = Env(db, FakeTranscriber("не знаю, большая комната"))
    await env.to_area()
    await env.client.voice()
    assert env.client.last_text() == texts.Q_AREA_RETRY


async def test_voice_first_message_starts_dialog(db):
    env = Env(db, FakeTranscriber("Здравствуйте, нужен потолок на кухню"))
    await env.client.voice()
    lead = await db.last_lead(USER.id)
    assert (await db.get_messages(lead.id))[0].text == "Здравствуйте, нужен потолок на кухню"
    assert env.client.last_text() == texts.Q_OBJECT


# --- Groq не успел: очередь ---


async def test_slow_groq_falls_back_to_queue(db, monkeypatch):
    monkeypatch.setattr(handlers, "STT_TIMEOUT", 0.05)
    env = Env(db, FakeTranscriber("около 30", "около 30", delay=0.2))
    await env.to_area()
    await env.client.voice("voice-slow")

    # Клиент не ждёт: ответ засчитан заглушкой, бот задал следующий вопрос.
    lead = await db.last_lead(USER.id)
    assert lead.area_text == texts.VOICE_PLACEHOLDER
    assert env.client.last_text() == texts.Q_CEILING_TYPE

    env.transcriber.delay = 0
    await env.run()
    lead = await db.last_lead(USER.id)
    assert lead.area_text == "около 30" and lead.area_m2 == 30.0
    comments = env.trello.cards["C1"]["comments"]
    assert "🎤 Голосовое (расшифровка будет ниже)" in "\n".join(comments)
    assert comments[-1].startswith("🎤 Расшифровка голосового от ") and comments[-1].endswith(":\n\n> около 30")


async def test_queue_retries_until_groq_is_back(db):
    env = Env(db, FakeTranscriber("вечером в пятницу"))
    env.transcriber.up = False
    await env.to_area()
    await env.client.press("area:15_30")
    await env.client.press("ct:matte")
    await env.client.contact("+79001234567")
    await env.client.voice("voice-late")
    assert (await db.last_lead(USER.id)).measure_time == texts.VOICE_PLACEHOLDER

    now = datetime.now(UTC)
    await env.run(now)
    task = [t for t in await db.outbox_pending() if t.kind == STT_TRANSCRIBE][0]
    assert task.attempts == 1 and "503" in task.last_error

    env.transcriber.up = True
    await env.run(now + timedelta(minutes=5))
    lead = await db.last_lead(USER.id)
    assert lead.measure_time == "вечером в пятницу"
    assert "**Замер:** вечером в пятницу" in env.trello.cards["C1"]["desc"]


async def test_transcript_does_not_overwrite_new_answer(db):
    env = Env(db, FakeTranscriber("старый ответ"))
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    msg_id = await db.add_message(lead.id, direction="in", kind="voice", file_id="voice-x")
    await db.update_lead(lead.id, object="Дом")  # поле уже заполнено другим ответом
    await db.enqueue(STT_TRANSCRIBE, lead.id,
                     {"message_id": msg_id, "field": "object", "placeholder": texts.VOICE_PLACEHOLDER})
    await env.run()
    assert (await db.get_lead(lead.id)).object == "Дом"
    assert (await db.get_message(msg_id)).text == "старый ответ"


async def test_give_up_after_many_attempts(db):
    env = Env(db, FakeTranscriber())
    env.transcriber.up = False
    lead = await db.create_lead(tg_user_id=1, chat_id=1, name="А", username=None, is_night=False)
    msg_id = await db.add_message(lead.id, direction="in", kind="voice", file_id="voice-x")
    await db.enqueue(STT_TRANSCRIBE, lead.id, {"message_id": msg_id})
    now = datetime.now(UTC)
    for i in range(GIVE_UP_ATTEMPTS + 1):
        await env.run(now + timedelta(hours=i))
    assert [t for t in await db.outbox_pending()] == []
    assert "расшифровать не удалось" in env.trello.cards["C1"]["comments"][-1]


async def test_without_groq_key_voice_waits_in_queue(db):
    env = Env(db, None)
    await env.to_area()
    await env.client.voice()
    assert (await db.last_lead(USER.id)).area_text == texts.VOICE_PLACEHOLDER
    await env.run()
    kinds = [t.kind for t in await db.outbox_pending()]
    assert kinds == [STT_TRANSCRIBE]  # всё остальное ушло в Trello, расшифровка ждёт ключа


# --- вложения в Trello ---


async def test_voice_and_photo_attached_to_card(db):
    env = Env(db, FakeTranscriber("около 20"))
    await env.to_area()
    await env.client.voice("voice-a")
    await env.client.photo("photo-b", caption="вот комната")
    await env.run()
    card = env.trello.cards["C1"]
    names = [(name, mime) for name, _, mime in card["attachments"]]
    msgs = {m.kind: m.id for m in await db.get_messages(1)}
    assert names == [(f"голосовое_{msgs['voice']}.ogg", "audio/ogg"), (f"фото_{msgs['photo']}.jpg", "image/jpeg")]
    assert card["attachments"][0][1] == b"FILE:voice-a.oga"
    assert any("📷 Фото: вот комната (во вложениях)" in c for c in card["comments"])


async def test_failed_attachment_does_not_block_card(db):
    env = Env(db, FakeTranscriber("около 20"))
    env.trello.fail["add_attachment"] = 99
    await env.to_area()
    await env.client.voice("voice-a")
    await env.client.press("ct:matte")
    now = datetime.now(UTC)
    for i in range(4):
        await env.run(now + timedelta(hours=i))
    assert await db.outbox_pending() == []
    comments = env.trello.cards["C1"]["comments"]
    assert any(c.startswith("⚠️ Не удалось приложить голосовое") for c in comments)
    assert "Нажал кнопку: **Матовый**" in "\n".join(comments)  # переписка после файла не застряла


# --- HTTP-клиент Groq ---


@respx.mock
async def test_groq_request_and_errors():
    route = respx.post("https://api.groq.com/openai/v1/audio/transcriptions")
    route.mock(return_value=httpx.Response(200, json={"text": "  двадцать метров  "}))
    t = GroqTranscriber("SECRET", "https://api.groq.com/openai/v1", "whisper-large-v3", "ru")
    assert await t.transcribe(b"OGG") == "двадцать метров"
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer SECRET"
    body = req.read().decode(errors="ignore")
    for part in ('name="model"', "whisper-large-v3", 'name="language"', "ru", 'filename="voice.ogg"', "Натяжные"):
        assert part in body

    route.mock(return_value=httpx.Response(200, json={"text": ""}))
    assert await t.transcribe(b"OGG") == UNINTELLIGIBLE

    route.mock(return_value=httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    with pytest.raises(SttError) as e:
        await t.transcribe(b"OGG")
    assert "401" in str(e.value) and "SECRET" not in str(e.value)

    route.mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(SttError, match="ConnectTimeout"):
        await t.transcribe(b"OGG")
    await t.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ЗВОНОК В ДВЕРЬ", UNINTELLIGIBLE),
        ("Продолжение следует...", UNINTELLIGIBLE),
        ("Субтитры сделал DimaTorzok", UNINTELLIGIBLE),
        ("[музыка]", UNINTELLIGIBLE),
        ("   ", UNINTELLIGIBLE),
        ("Квартира, двадцать метров", "Квартира, двадцать метров"),
        # Реальная речь с «опасным» словом остаётся как есть.
        ("Хочу тихо, без музыки, просто матовый потолок", "Хочу тихо, без музыки, просто матовый потолок"),
    ],
)
def test_clean_transcript(raw, expected):
    assert clean_transcript(raw) == expected


def test_payload_is_json_serializable():
    json.dumps({"message_id": 1, "field": "area", "placeholder": texts.VOICE_PLACEHOLDER}, ensure_ascii=False)
