from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из окружения. На VPS их задаёт EnvironmentFile=/etc/Solaris/ceiling-bot.env,
    локально — файл .env в корне проекта."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    # Groq: расшифровка голосовых. Без ключа голосовые принимаются как ответ, а расшифровка ждёт в очереди.
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_stt_model: str = "whisper-large-v3"
    groq_stt_language: str = "ru"

    # Trello. Без ключей бот работает, а задачи для Trello копятся в outbox до появления ключей.
    trello_api_key: SecretStr | None = None
    trello_token: SecretStr | None = None
    trello_board_id: str | None = None
    trello_list_new: str = "Новые запросы"
    trello_list_in_work: str = "В работе"

    @property
    def trello_enabled(self) -> bool:
        return bool(self.trello_api_key and self.trello_token and self.trello_board_id)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.studio_tz)


@lru_cache
def get_settings() -> Settings:
    return Settings()
