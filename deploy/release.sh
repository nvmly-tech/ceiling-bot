#!/usr/bin/env bash
# Серверная часть выкладки. Запускает deploy.sh по ssh, передавая этот файл через stdin (bash -s): bash читает
# скрипт по ходу выполнения, а при откате файлы в /opt/ceiling-bot подменяются — свой же файл читать нельзя.
#   release.sh install <ревизия>      — код из $NEW: бэкап базы → прошлая версия в $PREV → новая на место
#   release.sh rollback [--with-db]   — вернуть прошлую версию; --with-db — и базу из бэкапа перед выкладкой
# Пути — переменными окружения: тесты гоняют скрипт на временных папках (tests/test_release.py).
set -euo pipefail

APP=${APP:-/opt/ceiling-bot}
NEW=${NEW:-$APP.new}
PREV=${PREV:-$APP.prev}
DATA=${DATA:-/var/lib/private/ceiling-bot}
DB=$DATA/ceiling-bot.sqlite3
UNIT=ceiling-bot.service
WAIT=${WAIT:-60}  # с: сколько ждать, что бот поднялся

die() { echo "!! $*" >&2; exit 1; }

# Код бота — всё в $APP, кроме окружения Python (.venv, python): его обновляет install.sh.
# code_entries <папка> [действие find…]
code_entries() {
    local dir=$1
    shift
    find "$dir" -mindepth 1 -maxdepth 1 ! -name .venv ! -name python "$@"
}

put_code() {  # заменить код в $APP содержимым папки $1
    code_entries "$APP" -exec rm -rf {} +
    cp -a "$1/." "$APP/"
}

verify() {
    for _ in $(seq "$WAIT"); do
        if systemctl is-active --quiet "$UNIT"; then
            echo ">> бот работает"
            return 0
        fi
        sleep 1
    done
    echo "!! бот не поднялся за $WAIT с — журнал: journalctl -u $UNIT -n 50" >&2
    echo "!! откат: ./deploy/deploy.sh <сервер> --rollback (и база на момент выкладки: --rollback --with-db)" >&2
    exit 1
}

install_release() {
    local rev=$1 backup=""
    [ -f "$NEW/app/main.py" ] || die "в $NEW нет app/main.py — выкладка отменена"
    # Миграции новой версии меняют базу при первом же запуске — до этого снимаем копию.
    if [ -f "$DB" ] && systemctl cat ceiling-bot-backup.service >/dev/null 2>&1; then
        echo ">> бэкап базы перед выкладкой"
        systemctl start ceiling-bot-backup.service || die "бэкап не удался — выкладка отменена, сервер не тронут"
        backup=$(find "$DATA/backups" -name '*.sqlite3.gz' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)
    fi
    mkdir -p "$APP"
    if [ -f "$APP/app/main.py" ]; then
        rm -rf "$PREV" && mkdir -p "$PREV"
        code_entries "$APP" -exec cp -a {} "$PREV/" \;
        echo "$backup" > "$PREV/.predeploy_backup"
    fi
    put_code "$NEW" && rm -rf "$NEW"
    echo "$rev" > "$APP/REVISION"
    bash "$APP/deploy/install.sh"
    verify
}

rollback() {
    local backup=""
    [ -f "$PREV/app/main.py" ] || die "нет прошлой версии в $PREV — откатывать не на что"
    if [ "${1:-}" = --with-db ]; then
        backup=$(cat "$PREV/.predeploy_backup" 2>/dev/null || true)
        [ -n "$backup" ] && [ -f "$backup" ] || die "нет бэкапа базы, снятого перед выкладкой"
    fi
    echo ">> возвращаю версию $(cat "$PREV/REVISION" 2>/dev/null || echo '?')"
    put_code "$PREV"
    rm -f "$APP/.predeploy_backup"
    if [ -n "$backup" ]; then
        # Всё, что пришло после выкладки, из базы пропадёт — так и задумано: прошлая версия может не понять
        # колонки, которые добавила новая.
        systemctl stop "$UNIT"
        gunzip -c "$backup" > "$DB"
        rm -f "$DB-wal" "$DB-shm"
        echo ">> база восстановлена из $backup"
    fi
    bash "$APP/deploy/install.sh"
    verify
}

case "${1:-}" in
    install) [ -n "${2:-}" ] || die "использование: release.sh install <ревизия>"; install_release "$2" ;;
    rollback) rollback "${2:-}" ;;
    *) die "использование: release.sh install <ревизия> | rollback [--with-db]" ;;
esac
