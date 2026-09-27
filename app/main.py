import asyncio
import contextlib
import logging
from dataclasses import dataclass
from functools import partial

from aiogram import Bot, Dispatcher

from app import redact
from app.bot.assistant import LeadAssistant
from app.bot.handlers import create_router
from app.bot.manager import create_manager_router
from app.bot.storage import SQLiteStorage
from app.config import Settings, get_settings
from app.db import Database
from app.services import systemd
from app.services.health import Alerter, HealthMonitor
from app.services.llm import LLMProvider, LLMRouter
from app.services.notifier import Notifier
from app.services.outbox import Outbox
from app.services.stt import (
    FetchFile,
    GigaAMTranscriber,
    GroqTranscriber,
    SpeechService,
    Transcriber,
    model_label,
)
from app.services.tgfiles import download
from app.services.trello import TrelloClient, TrelloSync

log = logging.getLogger("ceiling-bot")


def build_dispatcher(
    db: Database, settings: Settings, notifier: Notifier | None = None, stt: SpeechService | None = None,
    assistant: LeadAssistant | None = None, monitor: HealthMonitor | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=SQLiteStorage(db))
    dp["monitor"] = monitor
    dp["db"] = db
    dp["settings"] = settings
    dp["notifier"] = notifier
    dp["stt"] = stt
    dp["assistant"] = assistant
    dp.include_router(create_manager_router())
    dp.include_router(create_router())
    return dp


def build_trello(db: Database, settings: Settings, fetch_file: FetchFile | None = None) -> TrelloSync | None:
    if not settings.trello_enabled:
        log.warning("Trello не настроен (TRELLO_API_KEY/TRELLO_TOKEN/TRELLO_BOARD_ID) — задачи копятся в очереди")
        return None
    client = TrelloClient(settings.trello_api_key.get_secret_value(), settings.trello_token.get_secret_value())
    return TrelloSync(
        db, client, settings.trello_board_id, settings.zone, settings.trello_list_new, settings.trello_list_in_work,
        fetch_file,
    )


def build_llm(settings: Settings) -> LLMRouter | None:
    """Основная модель (router.cheap) и резервная (Groq) — каждая, если для неё всё задано."""
    providers = []
    primary_key = settings.llm_primary_api_key.get_secret_value().strip() if settings.llm_primary_api_key else ""
    if settings.llm_primary_base_url and primary_key and settings.llm_primary_model:
        providers.append(LLMProvider(
            settings.llm_primary_name, settings.llm_primary_base_url, primary_key, settings.llm_primary_model,
            timeout=settings.llm_timeout_sec,
        ))
    else:
        log.warning("Основная LLM не настроена (LLM_PRIMARY_BASE_URL / _API_KEY / _MODEL)")
    groq_key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    if groq_key and settings.llm_fallback_model:
        # gpt-oss «рассуждает» перед ответом; для диалога хватает минимума — быстрее и дешевле.
        extra = {"reasoning_effort": "low"} if settings.llm_fallback_model.startswith("openai/gpt-oss") else {}
        providers.append(LLMProvider(
            f"groq: {settings.llm_fallback_model}", settings.groq_base_url, groq_key, settings.llm_fallback_model,
            timeout=settings.llm_timeout_sec, extra=extra,
        ))
    if not providers:
        log.warning("Ни одна LLM не настроена — бот ведёт анкету по скрипту")
        return None
    log.info("LLM: %s", " → ".join(p.label for p in providers) + " → скрипт")
    return LLMRouter(providers)


def build_stt(db: Database, settings: Settings, fetch_file: FetchFile) -> SpeechService | None:
    transcribers: list[Transcriber] = []
    if settings.gigaam_socket:
        transcribers.append(GigaAMTranscriber(settings.gigaam_socket, settings.gigaam_timeout_sec))
    key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    if key:
        transcribers.append(
            GroqTranscriber(key, settings.groq_base_url, settings.groq_stt_model, settings.groq_stt_language)
        )
    if not transcribers:
        log.warning("Расшифровка не настроена (нет GIGAAM_SOCKET и GROQ_API_KEY) — голосовые ждут в очереди")
        return None
    names = " → ".join(model_label(t) for t in transcribers)
    shadow = settings.stt_shadow and len(transcribers) > 1
    log.info("Расшифровка голосовых: %s%s", names, " (+ теневое сравнение)" if shadow else "")
    return SpeechService(db, transcribers, fetch_file, shadow=shadow)


@dataclass
class App:
    """Собранный бот: всё, что запускает run(). Отдельно от run(), чтобы сборку можно было проверить в тестах."""

    bot: Bot
    db: Database
    dp: Dispatcher
    outbox: Outbox
    notifier: Notifier
    monitor: HealthMonitor
    trello: TrelloSync | None
    stt: SpeechService | None
    llm: LLMRouter | None

    def start_tasks(self) -> list[asyncio.Task]:
        return [
            asyncio.create_task(self.outbox.run(), name="outbox"),
            asyncio.create_task(self.notifier.run(), name="notifier"),
            asyncio.create_task(self.monitor.run_watchdog(), name="watchdog"),
            asyncio.create_task(self.monitor.run_checks(), name="checks"),
        ]

    async def close(self, tasks: list[asyncio.Task] = ()) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.trello:
            await self.trello.client.close()
        if self.stt:
            await self.stt.close()
        if self.llm:
            await self.llm.close()
        await self.bot.session.close()
        await self.db.close()


async def build_app(settings: Settings, bot: Bot | None = None, db: Database | None = None) -> App:
    if db is None:
        db = Database(settings.db_path)
        await db.connect()
    bot = bot or Bot(settings.bot_token.get_secret_value())
    fetch_file = partial(download, bot)
    trello = build_trello(db, settings, fetch_file)
    stt = build_stt(db, settings, fetch_file)
    llm = build_llm(settings)
    assistant = LeadAssistant(llm) if llm else None
    notifier = Notifier(bot, db, settings, trello_enabled=trello is not None, assistant=assistant)

    handlers = dict(trello.handlers) if trello else {}
    if stt:
        handlers |= stt.handlers
    if settings.manager_chat_id is None:
        log.warning("MANAGER_CHAT_ID не задан — уведомления менеджерам копятся в очереди")
    else:
        handlers |= notifier.handlers
    outbox = Outbox(db, handlers)

    alerter = Alerter(bot, settings, notifier)
    monitor = HealthMonitor(
        bot, db, settings, outbox, notifier, alerter, llm=llm, stt=stt.transcribers if stt else None
    )
    bot.session.middleware(monitor.session_middleware)
    dp = build_dispatcher(db, settings, notifier, stt, assistant, monitor)
    return App(bot, db, dp, outbox, notifier, monitor, trello, stt, llm)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    # httpx логирует URL запросов на INFO, а в URL Trello — ключ и токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    redact.install(settings)  # страховка: секреты вырезаются из любых логов и трейсбэков

    app = await build_app(settings)
    await app.monitor.on_start()
    tasks = app.start_tasks()
    try:
        log.info("Bot started, db=%s", settings.db_path)
        await app.dp.start_polling(app.bot, allowed_updates=app.dp.resolve_used_update_types())
    finally:
        systemd.notify("STOPPING=1")
        await app.monitor.on_stop()
        await app.close(tasks)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
