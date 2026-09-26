"""Скачивание файлов из Telegram (голосовые, фото, документы)."""

from aiogram import Bot


async def download(bot: Bot, file_id: str) -> tuple[bytes, str]:
    """Содержимое файла и его путь на серверах Telegram (по пути видно расширение)."""
    file = await bot.get_file(file_id)
    buf = await bot.download_file(file.file_path)
    return buf.getvalue(), file.file_path
