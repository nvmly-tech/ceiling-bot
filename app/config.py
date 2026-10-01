from datetime import date, time
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

    # Рабочие часы студии: вне их клиенту говорим, когда ответит менеджер.
    studio_tz: str = "Europe/Moscow"
    work_start: time = time(9, 0)
    work_end: time = time(21, 0)
    # Рабочие дни недели (1 — понедельник … 7 — воскресенье): «1-5», «1-6», «1,2,3,5». Нерабочий день — как ночь:
    # уведомления без звука, без напоминаний, клиенту — «менеджер ответит в понедельник в 9:00».
    work_days: str = "1-7"
    # Нерабочие даты через запятую: полные (2026-12-31) или каждый год (01-01).
    days_off: str = ""

    log_level: str = "INFO"

    # Чат (обычно группа) менеджеров, куда бот шлёт уведомления о лидах.
    manager_chat_id: int | None = None
    abandon_after_min: int = 30       # клиент молчит столько минут посреди анкеты → «не завершил анкету»
    remind_after_min: int = 15        # лид не взяли за столько минут рабочего времени → напоминание
    remind_max: int = 3               # сколько раз напоминать
    client_msg_delay_sec: int = 60    # сообщения клиента после анкеты собираем в одно уведомление
    # Взятые заявки без итога (app/stages.py): напоминание тому, кто взял, дальше — владельцу.
    owner_chat_id: int | None = None      # куда эскалации владельцу; пусто — в группу менеджеров
    stuck_after_min: int = 120            # взял / не дозвонился, и нет итога столько минут рабочего времени
    measure_result_after_min: int = 120   # замер прошёл столько минут назад, а итога нет — «чем закончился?»
    thinking_remind_days: int = 3         # клиент «думает» столько дней — напомнить позвонить
    escalate_after_hours: int = 24        # итога нет столько часов — сообщить владельцу
    # Вопросы клиенту (только в рабочее время, по одному разу на заявку).
    ask_contact_after_min: int = 180      # взяли, а итога нет столько минут — «с вами связался менеджер?»
    rate_after_min: int = 180             # через столько минут после замера — «оцените замер от 1 до 5»
    # Недельный отчёт владельцу (в OWNER_CHAT_ID или группу менеджеров); за любой срок — командой /report.
    weekly_report: bool = True

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

    @field_validator("work_days")
    @classmethod
    def _valid_work_days(cls, value: str) -> str:
        parse_work_days(value)
        return value

    @field_validator("days_off")
    @classmethod
    def _valid_days_off(cls, value: str) -> str:
        parse_days_off(value)
        return value

    @property
    def workdays(self) -> frozenset[int]:
        return parse_work_days(self.work_days)

    @property
    def holidays(self) -> tuple[frozenset[date], frozenset[tuple[int, int]]]:
        """Нерабочие даты: (конкретные даты, ежегодные (месяц, день))."""
        return parse_days_off(self.days_off)

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


def parse_work_days(value: str) -> frozenset[int]:
    """«1-5» / «1,3,5-7» → номера дней недели. Ошибка — сразу при старте, а не на первом клиенте."""
    days: set[int] = set()
    for part in value.replace(" ", "").split(","):
        first, dash, last = part.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ValueError(f"WORK_DAYS: {value!r} — нужно вида 1-5 или 1,2,3,4,5 (1 — понедельник)")
        lo, hi = int(first), int(last or first)
        if not 1 <= lo <= hi <= 7:
            raise ValueError(f"WORK_DAYS: {part!r} — дни от 1 (понедельник) до 7 (воскресенье)")
        days.update(range(lo, hi + 1))
    return frozenset(days)


def parse_days_off(value: str) -> tuple[frozenset[date], frozenset[tuple[int, int]]]:
    dates: set[date] = set()
    yearly: set[tuple[int, int]] = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        try:
            if len(part) == 10:
                dates.add(date.fromisoformat(part))
            elif len(part) == 5 and part[2] == "-":
                month, day = int(part[:2]), int(part[3:])
                date(2024, month, day)  # високосный год: 02-29 допустим
                yearly.add((month, day))
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"DAYS_OFF: {part!r} — нужно 2026-12-31 (дата) или 01-01 (каждый год)") from None
    return frozenset(dates), frozenset(yearly)


@lru_cache
def get_settings() -> Settings:
    return Settings()
