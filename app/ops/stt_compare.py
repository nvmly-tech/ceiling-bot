"""Сравнение расшифровок голосовых: основная модель против теневой — чтобы решить, какую оставить.

На сервере (база бота доступна только root):
  /opt/ceiling-bot/.venv/bin/python -m app.ops.stt_compare /var/lib/private/ceiling-bot/ceiling-bot.sqlite3 [дней]

Печатает пары от самых непохожих — расхождения и есть то, что стоит послушать. Номера телефонов скрыты.
"""

import sqlite3
import sys
from datetime import UTC, datetime, timedelta

from app.bot.assistant import mask_phones
from app.services.stt import similarity

DEFAULT_DAYS = 14


def report(db_path: str, days: int = DEFAULT_DAYS) -> str:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT id, created_at, model, text, text_alt_model, text_alt FROM messages"
            " WHERE kind = 'voice' AND text IS NOT NULL AND text_alt IS NOT NULL AND created_at >= ? ORDER BY id",
            (since,),
        ).fetchall()
    if not rows:
        return f"За {days} дн. сравнивать нечего: нет голосовых, расшифрованных обеими моделями."
    scored = sorted(((similarity(r[3], r[5]), r) for r in rows), key=lambda x: x[0])
    avg = sum(score for score, _ in scored) / len(scored)
    lines = [f"Голосовых: {len(rows)}, среднее совпадение: {avg:.0%}", ""]
    for score, (msg_id, created, model, text, alt_model, alt) in scored:
        lines += [
            f"#{msg_id} · {created[:16]} · совпадение {score:.0%}",
            f"  {model}: {mask_phones(text)[0]}",
            f"  {alt_model}: {mask_phones(alt)[0]}",
            "",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DAYS))
