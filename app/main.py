import asyncio
import contextlib
import logging
from functools import partial

from aiogram import Bot, Dispatcher

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
from app.services.stt import FetchFile, GroqTranscriber, SpeechService
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
    key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    if not key:
        log.warning("GROQ_API_KEY не задан — голосовые принимаются без расшифровки, расшифровка ждёт в очереди")
        return None
    transcriber = GroqTranscriber(key, settings.groq_base_url, settings.groq_stt_model, settings.groq_stt_language)
    return SpeechService(db, transcriber, fetch_file)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    # httpx логирует URL запросов на INFO, а в URL Trello — ключ и токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = Database(settings.db_path)
    await db.connect()
    bot = Bot(settings.bot_token.get_secret_value())
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
        bot, db, settings, outbox, notifier, alerter, llm=llm, stt=stt.transcriber if stt else None
    )
    bot.session.middleware(monitor.session_middleware)
    await monitor.on_start()

    tasks = [
        asyncio.create_task(outbox.run(), name="outbox"),
        asyncio.create_task(notifier.run(), name="notifier"),
        asyncio.create_task(monitor.run_watchdog(), name="watchdog"),
        asyncio.create_task(monitor.run_checks(), name="checks"),
    ]

    dp = build_dispatcher(db, settings, notifier, stt, assistant, monitor)
    try:
        log.info("Bot started, db=%s", settings.db_path)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        systemd.notify("STOPPING=1")
        await monitor.on_stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if trello:
            await trello.client.close()
        if stt:
            await stt.transcriber.close()
        if llm:
            await llm.close()
        await bot.session.close()
        await db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
