from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из окружения. На VPS их задаёт EnvironmentFile=/etc/Solaris/ceiling-bot.env,
    локально — файл .env в корне проекта."""

    # env_ignore_empty: пустая строка «KEY=» в env-файле означает «не задано» — берётся значение по умолчанию.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    bot_token: SecretStr
    db_path: Path = Path("data/ceiling-bot.sqlite3")

    # Рабочие часы студии: вне их клиенту говорим, что менеджер ответит утром.
    studio_tz: str = "Europe/Moscow"
    work_start: time = time(9, 0)
    work_end: time = time(21, 0)

    log_level: str = "INFO"

    # Чат (обычно группа) менеджеров, куда бот шлёт уведомления о лидах.
    manager_chat_id: int | None = None
    abandon_after_min: int = 30       # клиент молчит столько минут посреди анкеты → «не завершил анкету»
    remind_after_min: int = 15        # лид не взяли за столько минут рабочего времени → напоминание
    remind_max: int = 3               # сколько раз напоминать
    client_msg_delay_sec: int = 60    # сообщения клиента после анкеты собираем в одно уведомление

    # Факты о студии для LLM (цены «от …», сроки, гарантии) — файл, который студия правит сама.
    # Пусто — образец facts.md из репозитория. На сервере — /etc/ceiling-bot/facts.md (задаёт unit-файл).
    facts_path: str = ""

    # Расшифровка голосовых. Основная — GigaAM на этом же сервере (сервис ceiling-bot-stt, deploy/stt):
    # путь к его сокету; пусто — не используется. Резервная — Groq (GROQ_API_KEY).
    # Нет ни одной — голосовые принимаются как ответ, а расшифровка ждёт в очереди.
    gigaam_socket: str = ""
    gigaam_timeout_sec: float = 60
    # Теневое сравнение: голосовое расшифровывает и вторая модель, текст — только в базу (для оценки качества).
    stt_shadow: bool = True
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_stt_model: str = "whisper-large-v3"
    groq_stt_language: str = "ru"

    # LLM: основная модель (DeepSeek через router.cheap) и резервная на Groq (ключ GROQ_API_KEY).
    llm_primary_name: str = "deepseek (router.cheap)"
    llm_primary_base_url: str | None = None
    llm_primary_api_key: SecretStr | None = None
    llm_primary_model: str | None = None
    llm_fallback_model: str = "openai/gpt-oss-120b"
    llm_timeout_sec: float = 15

    # Куда слать алерты сторожа; пусто — в чат менеджеров.
    admin_chat_id: int | None = None
    # Необязательно: URL внешнего мониторинга (например, healthchecks.io) — сторож пингует его раз в 5 минут,
    # пока бот здоров. Если перестанет — значит, лежит весь сервер.
    healthcheck_url: str | None = None

    # Trello. Без ключей бот работает, а задачи для Trello копятся в outbox до появления ключей.
    trello_api_key: SecretStr | None = None
    trello_token: SecretStr | None = None
    trello_board_id: str | None = None
    trello_list_new: str = "Новые запросы"
    trello_list_in_work: str = "В работе"
    # Списки для этапов после «Взял»: замер назначен (и «клиент думает»), договор, отказ.
    trello_list_measure: str = "Замер"
    trello_list_won: str = "Договор"
    trello_list_lost: str = "Отказ"

    @property
    def trello_enabled(self) -> bool:
        return bool(self.trello_api_key and self.trello_token and self.trello_board_id)

    @field_validator("studio_tz")
    @classmethod
    def _known_tz(cls, value: str) -> str:
        # Иначе опечатка («Moscow») проходит загрузку, бот стартует и падает на первом же сообщении клиента.
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError(f"неизвестный часовой пояс {value!r}, нужен вида Europe/Moscow") from None
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.studio_tz)


@lru_cache
def get_settings() -> Settings:
    return Settings()
