"""Вырезание секретов (токен бота, ключи API) из логов, ошибок в базе и алертов.

Первый слой защиты — не пропускать секреты в тексты ошибок (см. tgfiles.download). Этот модуль — страховка:
всё, что уходит в журнал, в outbox.last_error, в /status и алерты, проходит через redact().
"""

import logging

MASK = "***"
_secrets: set[str] = set()


def register(*values: str | None) -> None:
    """Запомнить секреты. Короткие значения не берём — они дали бы ложные замены."""
    for v in values:
        if v and len(v.strip()) >= 8:
            _secrets.add(v.strip())


def redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


class RedactingFilter(logging.Filter):
    """Фильтр для обработчиков логов: секреты вырезаются из сообщения и из трейсбэка."""

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(settings) -> None:
    """Зарегистрировать все секреты из настроек и повесить фильтр на обработчики корневого логгера."""
    for field in ("bot_token", "trello_api_key", "trello_token", "groq_api_key", "llm_primary_api_key"):
        value = getattr(settings, field, None)
        register(value.get_secret_value() if value is not None else None)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
