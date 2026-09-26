"""LLM: OpenAI-совместимые провайдеры и маршрутизатор «основная → резервная».

Если модель ответила ошибкой, не уложилась в таймаут или вернула невалидный JSON, запрос уходит
к следующей. Если не справились все — LLMError, и вызывающий код переходит на скрипт.
После FAIL_THRESHOLD ошибок подряд модель отключается на COOLDOWN, затем пробуется снова.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx

log = logging.getLogger(__name__)

FAIL_THRESHOLD = 3
COOLDOWN = timedelta(minutes=5)

T = TypeVar("T")


class LLMError(Exception):
    pass


class FormatError(LLMError):
    """Модель ответила, но не в нужном формате. API работает — модель не отключаем."""


class LLMProvider:
    def __init__(
        self, label: str, base_url: str, api_key: str, model: str, *,
        timeout: float = 15, extra: dict[str, Any] | None = None, http: httpx.AsyncClient | None = None,
    ):
        self.label, self.model, self.timeout = label, model, timeout
        self.extra = extra or {}  # параметры конкретного провайдера, например reasoning_effort
        self._http = http or httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout + 5
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def chat_json(
        self, messages: list[dict[str, str]], *, max_tokens: int = 700, temperature: float = 0.4
    ) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            **self.extra,
        }
        try:
            resp = await self._http.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"{self.label}: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise LLMError(f"{self.label}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError):
            raise LLMError(f"{self.label}: неожиданный ответ API") from None
        return parse_json(content, self.label)

    async def check_available(self) -> None:
        """Бесплатная проверка (0 токенов): API доступен, ключ рабочий, модель есть в списке провайдера."""
        try:
            resp = await self._http.get("/models")
        except httpx.HTTPError as e:
            raise LLMError(f"{self.label}: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise LLMError(f"{self.label}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            ids = {m.get("id") for m in resp.json().get("data", [])}
        except (ValueError, AttributeError):
            raise LLMError(f"{self.label}: неожиданный ответ /models") from None
        if self.model not in ids:
            raise LLMError(f"{self.label}: модели {self.model} нет в списке провайдера")

    async def ping(self) -> None:
        """Настоящий запрос к модели (~25–130 токенов) — сторож делает его, только пока модель помечена упавшей.
        Запас max_tokens нужен «рассуждающим» моделям (gpt-oss): иначе они не успевают выдать JSON."""
        await self.chat_json([{"role": "user", "content": 'Ответь JSON {"ok": true}'}], max_tokens=200, temperature=0)


def parse_json(content: str, label: str = "llm") -> dict:
    """JSON из ответа модели; допускает обёртку ```json … ```."""
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise FormatError(f"{label}: ответ не JSON: {content[:120]!r}") from None
    if not isinstance(data, dict):
        raise FormatError(f"{label}: JSON не объект")
    return data


@dataclass
class Health:
    failures: int = 0
    down_until: datetime | None = None
    last_error: str | None = None
    last_ok: datetime | None = None

    def available(self, now: datetime) -> bool:
        return self.down_until is None or now >= self.down_until

    @property
    def is_down(self) -> bool:
        return self.down_until is not None


StatusCallback = Callable[[str, bool, str | None], None]  # (label, работает?, последняя ошибка)


@dataclass
class LLMRouter:
    providers: list[LLMProvider]
    health: dict[str, Health] = field(default_factory=dict)
    on_status_change: StatusCallback | None = None  # алерты сторожа (этап 6)

    def __post_init__(self) -> None:
        for p in self.providers:
            self.health.setdefault(p.label, Health())

    def record_ok(self, label: str, now: datetime | None = None) -> None:
        h = self.health[label]
        was_down = h.is_down
        h.failures, h.down_until, h.last_ok = 0, None, now or datetime.now(UTC)
        if was_down:
            log.info("LLM %s снова работает", label)
            if self.on_status_change:
                self.on_status_change(label, True, None)

    def record_fail(self, label: str, error: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        h = self.health[label]
        h.failures += 1
        h.last_error = error
        if h.failures >= FAIL_THRESHOLD:
            was_down = h.is_down
            h.down_until = now + COOLDOWN
            if not was_down:
                log.error("LLM %s отключена на %s после %s ошибок: %s", label, COOLDOWN, h.failures, error)
                if self.on_status_change:
                    self.on_status_change(label, False, error)

    async def json(
        self, messages: list[dict[str, str]], validate: Callable[[dict], T], *,
        max_tokens: int = 700, temperature: float = 0.4, now: datetime | None = None,
    ) -> tuple[T, str]:
        """Ответ первой справившейся модели, прошедший validate, и подпись этой модели."""
        now = now or datetime.now(UTC)
        errors = []
        for p in self.providers:
            if not self.health[p.label].available(now):
                errors.append(f"{p.label}: временно отключена")
                continue
            try:
                data = await asyncio.wait_for(
                    p.chat_json(messages, max_tokens=max_tokens, temperature=temperature), p.timeout
                )
                result = validate(data)
            except (FormatError, ValueError) as e:
                # Неверный формат ответа — пробуем следующую модель, но эту не отключаем: API работает.
                log.warning("LLM %s ответила не по формату: %s", p.label, e)
                errors.append(f"{p.label}: {e}")
                continue
            except Exception as e:  # noqa: BLE001 — сеть, HTTP, таймаут: пробуем следующую
                error = str(e) or f"{p.label}: {type(e).__name__}"
                log.warning("LLM %s не справилась: %s", p.label, error)
                self.record_fail(p.label, error, now)
                errors.append(error)
                continue
            self.record_ok(p.label, now)
            return result, p.label
        raise LLMError("; ".join(errors) or "нет моделей")

    async def close(self) -> None:
        for p in self.providers:
            await p.close()
