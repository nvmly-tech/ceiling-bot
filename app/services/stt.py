"""Расшифровка голосовых: GigaAM на своём сервере (основная), Groq Whisper (резервная).

Сразу при получении голосового бот пытается расшифровать его в диалоге (см. handlers.receive):
сначала основной моделью, при ошибке или долгом ответе — резервной. Если не успели обе — задача
stt.transcribe ждёт в outbox, а готовый текст потом попадает в переписку, анкету и отдельным
комментарием в карточку Trello. Какая модель расшифровала — в messages.model (видно только в Trello).

Теневое сравнение: после расшифровки другая модель расшифровывает то же голосовое в фоне
(задача shadow.stt), текст ложится в messages.text_alt — только для оценки, клиент и менеджер его не видят.
"""

import asyncio
import difflib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.bot import texts
from app.db import CARD_TRANSCRIPT, STT_SHADOW, STT_TRANSCRIBE, Database, Message, OutboxTask
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
SHADOW_ATTEMPTS = 3    # теневая расшифровка — только для сравнения, долго не упорствуем
# Расшифровка «на лету» ограничена по времени (клиент ждёт): основной модели — эта доля, остальное — резервной.
PRIMARY_SHARE = 0.7
GIGAAM, GROQ = "GigaAM", "Groq"
GIGAAM_REPLY_MAX = 1024 * 1024

FetchFile = Callable[[str], Awaitable[tuple[bytes, str]]]


class SttError(Exception):
    pass


class Transcriber(Protocol):
    label: str

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str: ...
    async def ping(self) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class Transcript:
    text: str
    model: str  # какая модель расшифровала (label)


def model_label(t: Transcriber) -> str:
    return getattr(t, "label", type(t).__name__)


class GigaAMTranscriber:
    """GigaAM на этом же сервере: сервис ceiling-bot-stt (deploy/stt), unix-сокет, процесс на запрос.
    Голос не покидает сервер. Расшифровок — не больше одной за раз: модели нужно ~0.8 ГБ памяти."""

    label = GIGAAM

    def __init__(self, socket_path: str, timeout: float = 60):
        self.socket_path, self.timeout = socket_path, timeout
        self._busy = asyncio.Lock()

    async def _ask(self, payload: bytes) -> dict:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError as e:
            raise SttError(f"GigaAM: сервис недоступен ({type(e).__name__})") from None
        try:
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            data = await reader.read(GIGAAM_REPLY_MAX)
        except OSError as e:
            raise SttError(f"GigaAM: обрыв связи ({type(e).__name__})") from None
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        lines = [line for line in data.splitlines() if line.strip()]
        if not lines:
            raise SttError("GigaAM: пустой ответ — обработчик упал (journalctl -u 'ceiling-bot-stt@*')")
        try:
            answer = json.loads(lines[-1])
        except ValueError:
            raise SttError("GigaAM: ответ не JSON") from None
        if "error" in answer:
            raise SttError(f"GigaAM: {answer['error']}")
        return answer

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        async with self._busy:
            try:
                answer = await asyncio.wait_for(self._ask(b"TRANSCRIBE\n" + audio), self.timeout)
            except TimeoutError:
                raise SttError(f"GigaAM: не ответил за {self.timeout:g} с") from None
        return clean_transcript(str(answer.get("text", "")))

    async def ping(self) -> None:
        """Для сторожа: сервис отвечает. Модель не грузит — это делает только настоящая расшифровка."""
        if self._busy.locked():
            return  # идёт расшифровка — сервис жив, второй процесс ради проверки не запускаем
        await asyncio.wait_for(self._ask(b"PING\n"), 10)

    async def close(self) -> None:
        pass


class GroqTranscriber:
    label = GROQ

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
    """Расшифровка голосовых моделями по порядку: первая — основная, дальше — резервные."""

    def __init__(
        self, db: Database, transcribers: Transcriber | Sequence[Transcriber], fetch_file: FetchFile,
        *, shadow: bool = False,
    ):
        self.db, self.fetch_file = db, fetch_file
        self.transcribers = list(transcribers) if isinstance(transcribers, (list, tuple)) else [transcribers]
        self.shadow = shadow and len(self.transcribers) > 1

    @property
    def handlers(self):
        return {STT_TRANSCRIBE: self.transcribe_task, STT_SHADOW: self.shadow_task}

    async def close(self) -> None:
        for t in self.transcribers:
            await t.close()

    async def transcribe(self, file_id: str, budget: float | None = None) -> Transcript:
        """budget — сколько секунд всего есть (расшифровка «на лету»); None — без ограничения (из очереди)."""
        audio, _ = await self.fetch_file(file_id)
        return await self.transcribe_audio(audio, budget)

    async def transcribe_audio(self, audio: bytes, budget: float | None = None) -> Transcript:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget if budget else None
        errors = []
        for i, t in enumerate(self.transcribers):
            limit = None
            if deadline is not None:
                left = deadline - loop.time()
                limit = left * PRIMARY_SHARE if i < len(self.transcribers) - 1 else left
                if limit <= 0:
                    break
            try:
                # Telegram отдаёт голосовые как .oga — Groq принимает их под расширением .ogg.
                call = t.transcribe(audio, "voice.ogg")
                text = await (asyncio.wait_for(call, limit) if limit else call)
            except Exception as e:  # noqa: BLE001 — любая ошибка: пробуем следующую модель
                error = str(e) or type(e).__name__
                errors.append(f"{model_label(t)}: {error}")
                log.warning("Расшифровка %s не удалась: %s", model_label(t), error)
                continue
            return Transcript(text, model_label(t))
        raise SttError("; ".join(errors) or "не хватило времени")

    async def transcribe_task(self, task: OutboxTask) -> None:
        msg = await self.db.get_message(task.payload["message_id"])
        if msg is None or msg.text:
            return
        try:
            result = await self.transcribe(msg.file_id)
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
        await self.db.set_message_text(msg.id, result.text, result.model)
        events = [(CARD_TRANSCRIPT, {"message_id": msg.id})]
        if self.shadow:
            events.append((STT_SHADOW, {"message_id": msg.id}))
        field = task.payload.get("field")
        await self._fill_field(task, msg.lead_id, events, field_value(field, result.text) if field else {})

    async def shadow_task(self, task: OutboxTask) -> None:
        """Расшифровать голосовое другой моделью — только для сравнения (messages.text_alt)."""
        msg = await self.db.get_message(task.payload["message_id"])
        if msg is None or not msg.text or msg.text_alt is not None:
            return
        other = next((t for t in self.transcribers if model_label(t) != msg.model), None)
        if other is None:
            return
        try:
            audio, _ = await self.fetch_file(msg.file_id)
            text = await other.transcribe(audio, "voice.ogg")
        except Exception as e:
            if task.attempts + 1 < SHADOW_ATTEMPTS:
                raise
            log.warning("Теневая расшифровка %s голосового %s не удалась: %s", model_label(other), msg.id, e)
            return
        await self.db.set_message_alt(msg.id, text, model_label(other))

    async def _fill_field(self, task: OutboxTask, lead_id: int, events: list, values: dict) -> None:
        """Подставить значение в поле анкеты, только если там всё ещё заглушка (клиент мог ответить заново)."""
        field = task.payload.get("field")
        lead = await self.db.get_lead(lead_id)
        current = getattr(lead, "area_text" if field == "area" else field, None) if field and lead else None
        if field and current == task.payload.get("placeholder"):
            await self.db.update_lead(lead_id, events=events, **values)
        else:
            await self.db.update_lead(lead_id, events=events)


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def similarity(a: str, b: str) -> float:
    """Похожесть двух расшифровок по словам (регистр, пунктуация и «ё» не важны), 0…1."""
    return difflib.SequenceMatcher(None, _words(a), _words(b)).ratio()


def compare_summary(pairs: Sequence[Message]) -> tuple[int, float]:
    """Сколько голосовых сравнили и средняя похожесть расшифровок."""
    if not pairs:
        return 0, 0.0
    return len(pairs), sum(similarity(m.text or "", m.text_alt or "") for m in pairs) / len(pairs)
