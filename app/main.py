import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher

from app.bot.handlers import create_router
from app.bot.manager import create_manager_router
from app.bot.storage import SQLiteStorage
from app.config import Settings, get_settings
from app.db import Database
from app.services.notifier import Notifier
from app.services.outbox import Outbox
from app.services.trello import TrelloClient, TrelloSync

log = logging.getLogger("ceiling-bot")


def build_dispatcher(db: Database, settings: Settings, notifier: Notifier | None = None) -> Dispatcher:
    dp = Dispatcher(storage=SQLiteStorage(db))
    dp["db"] = db
    dp["settings"] = settings
    dp["notifier"] = notifier
    dp.include_router(create_manager_router())
    dp.include_router(create_router())
    return dp


def build_trello(db: Database, settings: Settings) -> TrelloSync | None:
    if not settings.trello_enabled:
        log.warning("Trello не настроен (TRELLO_API_KEY/TRELLO_TOKEN/TRELLO_BOARD_ID) — задачи копятся в очереди")
        return None
    client = TrelloClient(settings.trello_api_key.get_secret_value(), settings.trello_token.get_secret_value())
    return TrelloSync(
        db, client, settings.trello_board_id, settings.zone, settings.trello_list_new, settings.trello_list_in_work
    )


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    # httpx логирует URL запросов на INFO, а в URL Trello — ключ и токен.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = Database(settings.db_path)
    await db.connect()
    bot = Bot(settings.bot_token.get_secret_value())
    trello = build_trello(db, settings)
    notifier = Notifier(bot, db, settings, trello_enabled=trello is not None)

    handlers = dict(trello.handlers) if trello else {}
    if settings.manager_chat_id is None:
        log.warning("MANAGER_CHAT_ID не задан — уведомления менеджерам копятся в очереди")
    else:
        handlers |= notifier.handlers
    outbox = Outbox(db, handlers)
    tasks = [
        asyncio.create_task(outbox.run(), name="outbox"),
        asyncio.create_task(notifier.run(), name="notifier"),
    ]

    dp = build_dispatcher(db, settings, notifier)
    try:
        log.info("Bot started, db=%s", settings.db_path)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if trello:
            await trello.client.close()
        await bot.session.close()
        await db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
