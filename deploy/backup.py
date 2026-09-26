#!/usr/bin/env python3
"""Ночной бэкап базы бота (юнит ceiling-bot-backup.service по таймеру). Только стандартная библиотека —
не зависит от venv и работает, даже если бот лежит.

Запускается от того же временного пользователя systemd, что и бот (User=ceiling-bot): иначе SQLite мог бы
создать служебные файлы -wal/-shm с чужим владельцем, и бот не открыл бы базу.
Копия снимается через SQLite backup API — корректно при работающем боте и WAL.
Хранится KEEP последних копий: /var/lib/ceiling-bot/backups/ceiling-bot-YYYYmmdd-HHMM.sqlite3.gz

Восстановление (от root):
  systemctl stop ceiling-bot
  gunzip -c /var/lib/private/ceiling-bot/backups/<копия> > /var/lib/private/ceiling-bot/ceiling-bot.sqlite3
  rm -f /var/lib/private/ceiling-bot/ceiling-bot.sqlite3-wal /var/lib/private/ceiling-bot/ceiling-bot.sqlite3-shm
  systemctl start ceiling-bot
"""

import gzip
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DB = Path("/var/lib/ceiling-bot/ceiling-bot.sqlite3")
DEST = Path("/var/lib/ceiling-bot/backups")
KEEP = 14


def backup(db: Path = DB, dest: Path = DEST, keep: int = KEEP, now: datetime | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)  # в базе телефоны и переписка клиентов
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    target = dest / f"ceiling-bot-{stamp}.sqlite3.gz"
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        copy = Path(tmp) / "copy.sqlite3"
        src = sqlite3.connect(db)
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        src.close()
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.close()
        if check != "ok":
            raise RuntimeError(f"копия не прошла integrity_check: {check}")
        with open(copy, "rb") as fin, gzip.open(target, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    target.chmod(0o600)
    for old in sorted(dest.glob("ceiling-bot-*.sqlite3.gz"))[:-keep]:
        old.unlink()
    return target


if __name__ == "__main__":
    path = backup()
    print(f"бэкап: {path} ({path.stat().st_size} байт)")
    sys.exit(0)
