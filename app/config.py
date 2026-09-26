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
