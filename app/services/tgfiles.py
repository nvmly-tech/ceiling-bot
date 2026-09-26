"""Скачивание файлов из Telegram (голосовые, фото, документы)."""

from aiogram import Bot

MAX_DOWNLOAD = 10 * 1024 * 1024  # Trello на бесплатном тарифе не принимает вложения больше 10 МБ


class TelegramFileError(Exception):
    """Файл не скачан. Текст — без URL: в ссылке на файл Telegram стоит токен бота."""


class FileTooLarge(TelegramFileError):
    pass


async def download(bot: Bot, file_id: str) -> tuple[bytes, str]:
    """Содержимое файла и его путь на серверах Telegram (по пути видно расширение)."""
    try:
        file = await bot.get_file(file_id)
    except Exception as e:  # noqa: BLE001
        raise TelegramFileError(f"Telegram getFile: {type(e).__name__}") from None
    if file.file_size and file.file_size > MAX_DOWNLOAD:
        mb = 1024 * 1024
        raise FileTooLarge(f"файл {file.file_size // mb} МБ — больше лимита {MAX_DOWNLOAD // mb} МБ")
    try:
        buf = await bot.download_file(file.file_path)
    except Exception as e:  # noqa: BLE001 — str(e) у aiohttp содержит URL с токеном
        status = getattr(e, "status", None)
        raise TelegramFileError(f"Telegram download: {type(e).__name__}" + (f" {status}" if status else "")) from None
    return buf.getvalue(), file.file_path
