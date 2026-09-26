"""Алерт о падении бота. Запускает systemd:

- ExecStopPost (`python -m app.ops.alert stop`) — после каждой остановки; при штатной (SERVICE_RESULT=success,
  например systemctl restart при публикации) молчит, иначе сообщает причину: watchdog, код выхода, сигнал;
- OnFailure-юнит (`python -m app.ops.alert failed`) — systemd исчерпал лимит перезапусков и сдался.

Никогда не завершается ошибкой: сбой алерта не должен мешать systemd перезапускать бота.
"""

import sqlite3
import sys
from html import escape

import httpx

from app.config import Settings
from app.services.health import revision

REASONS = {
    "watchdog": "завис — сторож не получил сигнал «жив» за 60 с",
    "exit-code": "завершился с ошибкой (код {status})",
    "signal": "убит сигналом {status}",
    "core-dump": "аварийно завершился (core dump, сигнал {status})",
    "timeout": "не уложился в таймаут запуска или остановки",
    "resources": "не хватило ресурсов для запуска",
    "oom-kill": "убит из-за нехватки памяти",
    "protocol": "не сообщил systemd о готовности",
}


def message(mode: str, env: dict[str, str]) -> str | None:
    rev = escape(revision())
    if mode == "failed":
        return (
            "🛑 <b>ceiling-bot остановлен</b>: слишком много падений подряд, systemd больше не перезапускает.\n"
            f"Нужна ручная проверка: <code>systemctl status ceiling-bot</code>, "
            f"<code>journalctl -u ceiling-bot -n 100</code> (версия {rev})"
        )
    result = env.get("SERVICE_RESULT", "success")
    if result == "success":
        return None
    reason = REASONS.get(result, result).format(status=escape(env.get("EXIT_STATUS", "?")))
    return (
        f"⚠️ <b>ceiling-bot упал</b>: {reason}.\n"
        f"systemd перезапустит его через 5 с. Логи: <code>journalctl -u ceiling-bot -n 100</code> (версия {rev})"
    )


def chat_id(settings: Settings) -> int | None:
    if settings.admin_chat_id:
        return settings.admin_chat_id
    try:  # группу менеджеров могли превратить в супергруппу — её новый id бот хранит в базе
        with sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = 'manager_chat_id'").fetchone()
        if row and row[0]:
            return int(row[0])
    except sqlite3.Error:
        pass
    return settings.manager_chat_id


def main(argv: list[str], env: dict[str, str]) -> int:
    mode = argv[1] if len(argv) > 1 else "stop"
    text = message(mode, env)
    if text is None:
        return 0
    try:
        settings = Settings()
        target = chat_id(settings)
        if target is None:
            print("alert: нет ADMIN_CHAT_ID и MANAGER_CHAT_ID", file=sys.stderr)
            return 0
        resp = httpx.post(
            f"https://api.telegram.org/bot{settings.bot_token.get_secret_value()}/sendMessage",
            data={"chat_id": target, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"alert: Telegram HTTP {resp.status_code}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"alert: {type(e).__name__}", file=sys.stderr)  # без текста — в нём может быть URL с токеном
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv, dict(os.environ)))
