"""Расшифровка голосовых через Groq (Whisper, OpenAI-совместимый API).

Сразу при получении голосового бот пытается расшифровать его в диалоге (см. handlers.receive).
Если Groq не ответил вовремя или ключа нет — задача stt.transcribe ждёт в outbox, а готовый
текст потом попадает в переписку, анкету и отдельным комментарием в карточку Trello.
"""

import logging
import re
from collections.abc import Awaitable, Callable

import httpx

from app.bot import texts
from app.db import CARD_TRANSCRIPT, STT_TRANSCRIBE, Database, OutboxTask
from app.parsing import normalize_phone, parse_area

log = logging.getLogger(__name__)

# Подсказка для Whisper: словарь предметной области улучшает распознавание терминов.
PROMPT = (
    "Натяжные потолки. Квартира, дом, офис. Площадь в квадратных метрах. "
    "Матовый, глянцевый, сатиновый, тканевый, парящий потолок, световые линии, "
    "теневой профиль, карниз, люстра, точечные светильники. Замер, телефон."
)
UNINTELLIGIBLE = "[неразборчиво]"

# Whisper на тишине и шуме «слышит» фразы из субтитров, на которых учился. Если расшифровка
# целиком из таких фраз — считаем голосовое неразборчивым, а не ответом клиента.
_HALLUCINATIONS = re.compile(
    r"(продолжение следует|субтитры (сделал|создавал|подогнал)[^.!?]*|редактор субтитров[^.!?]*|"
    r"корректор[^.!?]*|спасибо за просмотр|подписывайтесь на канал|ставьте лайк[^.!?]*|"
    r"звонок в дверь|музыка|аплодисменты|смех|dimatorzok)",
    re.IGNORECASE,
)


def clean_transcript(text: str) -> str:
    stripped = _HALLUCINATIONS.sub("", text)
    if not re.sub(r"[\W_]+", "", stripped):
        return UNINTELLIGIBLE
    return text.strip()
GIVE_UP_ATTEMPTS = 10  # ~1 час ретраев, потом «расшифровать не удалось»

FetchFile = Callable[[str], Awaitable[tuple[bytes, str]]]


class SttError(Exception):
    pass


class GroqTranscriber:
    def __init__(self, api_key: str, base_url: str, model: str, language: str, http: httpx.AsyncClient | None = None):
        self.model, self.language = model, language
        self._http = http or httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        try:
            resp = await self._http.post(
                "/audio/transcriptions",
                files={"file": (filename, audio, "audio/ogg")},
                data={
                    "model": self.model,
                    "language": self.language,
                    "response_format": "json",
                    "temperature": "0",
                    "prompt": PROMPT,
                },
            )
        except httpx.HTTPError as e:
            raise SttError(f"Groq: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise SttError(f"Groq: HTTP {resp.status_code} {resp.text[:200]}")
        return clean_transcript(resp.json().get("text", ""))

    async def ping(self) -> None:
        """Проверка для сторожа (этап 6): ключ рабочий, API отвечает."""
        resp = await self._http.get("/models")
        if resp.status_code >= 400:
            raise SttError(f"Groq: HTTP {resp.status_code}")


def field_value(field: str, text: str) -> dict[str, object]:
    """Как расшифровка голосового ложится в поле анкеты."""
    if field == "area":
        return {"area_text": text, "area_m2": parse_area(text)}
    if field == "phone":
        return {"phone": normalize_phone(text) or text}
    return {field: text}


class SpeechService:
    def __init__(self, db: Database, transcriber: GroqTranscriber, fetch_file: FetchFile):
        self.db, self.transcriber, self.fetch_file = db, transcriber, fetch_file

    @property
    def handlers(self):
        return {STT_TRANSCRIBE: self.transcribe_task}

    async def transcribe_file(self, file_id: str) -> str:
        audio, _ = await self.fetch_file(file_id)
        # Telegram отдаёт голосовые как .oga — Groq принимает их под расширением .ogg.
        return await self.transcriber.transcribe(audio, "voice.ogg")

    async def transcribe_task(self, task: OutboxTask) -> None:
        msg = await self.db.get_message(task.payload["message_id"])
        if msg is None or msg.text:
            return
        try:
            text = await self.transcribe_file(msg.file_id)
        except Exception:
            if task.attempts + 1 < GIVE_UP_ATTEMPTS:
                raise
            log.error("Голосовое %s так и не расшифровано", msg.id)
            # Заглушка «голосовое сообщение» в анкете выглядит как ответ — помечаем, что его надо прослушать.
            events = [(CARD_TRANSCRIPT, {"message_id": msg.id, "failed": True})]
            field = task.payload.get("field")
            values = field_value(field, texts.VOICE_FAILED_VALUE) if field else {}
            await self._fill_field(task, msg.lead_id, events, values)
            return
        await self.db.set_message_text(msg.id, text)
        events = [(CARD_TRANSCRIPT, {"message_id": msg.id})]
        field = task.payload.get("field")
        await self._fill_field(task, msg.lead_id, events, field_value(field, text) if field else {})

    async def _fill_field(self, task: OutboxTask, lead_id: int, events: list, values: dict) -> None:
        """Подставить значение в поле анкеты, только если там всё ещё заглушка (клиент мог ответить заново)."""
        field = task.payload.get("field")
        lead = await self.db.get_lead(lead_id)
        current = getattr(lead, "area_text" if field == "area" else field, None) if field and lead else None
        if field and current == task.payload.get("placeholder"):
            await self.db.update_lead(lead_id, events=events, **values)
        else:
            await self.db.update_lead(lead_id, events=events)
