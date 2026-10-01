#!/usr/bin/env bash
# Публикация закоммиченной версии на сервер.
#   ./deploy/deploy.sh <host>                       — проверки, затем выкладка (только из ветки main)
#   ./deploy/deploy.sh --check                      — только проверки, без выкладки
#   ./deploy/deploy.sh <host> --rollback [--with-db] — вернуть прошлую версию (и базу на момент выкладки)
#   ./deploy/deploy.sh <host> --allow-branch        — выложить не из main (осознанно: например, на стенд)
# Перед выкладкой: нет незакоммиченных изменений в проекте бота, линтер чист, тесты зелёные, покрытие не ниже
# порога из pyproject.toml. Что-то не так — на сервер ничего не уходит. .env и базы в git нет — не уходят тоже.
# На сервере (deploy/release.sh): бэкап базы до замены кода, прошлая версия — в /opt/ceiling-bot.prev,
# после установки — проверка, что бот поднялся.
set -euo pipefail

HOST=""
CHECK_ONLY=0
ROLLBACK=0
WITH_DB=""
ALLOW_BRANCH=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --rollback) ROLLBACK=1 ;;
        --with-db) WITH_DB=--with-db ;;
        --allow-branch) ALLOW_BRANCH=1 ;;
        -*) echo "!! неизвестный флаг $arg"; exit 1 ;;
        *) HOST=$arg ;;
    esac
done
# Адрес — всегда явно: раньше по умолчанию стоял сервер исполнителя, и «просто запустить» выкладывало туда.
if [ "$CHECK_ONLY" = 0 ] && [ -z "$HOST" ]; then
    echo "использование: $0 <host> [--rollback [--with-db]] [--allow-branch] | $0 --check"
    exit 1
fi
if [ -n "$WITH_DB" ] && [ "$ROLLBACK" = 0 ]; then
    echo "!! --with-db — только вместе с --rollback"
    exit 1
fi

# Папка проекта — на уровень выше скрипта. Работает и когда бот — корень репозитория, и когда он в подпапке.
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$APP_DIR"
PREFIX=$(git rev-parse --show-prefix)  # путь проекта внутри репозитория ("" — если он и есть корень)
REV=$(git rev-parse --short HEAD)

if [ "$ROLLBACK" = 1 ]; then
    echo ">> $HOST: откат на прошлую версию ${WITH_DB:+(вместе с базой на момент выкладки)}"
    # Скрипт — из локальной копии через stdin: файлы на сервере при откате подменяются.
    ssh "$HOST" "bash -s -- rollback $WITH_DB" < deploy/release.sh
    exit 0
fi

# Выкладывается HEAD текущей ветки — с рабочей ветки на сервер ушла бы недоделка.
BRANCH=$(git branch --show-current)
if [ "$CHECK_ONLY" = 0 ] && [ "$BRANCH" != main ] && [ "$ALLOW_BRANCH" = 0 ]; then
    echo "!! выкладывается только main (сейчас: ${BRANCH:-нет ветки}); осознанно с другой ветки — --allow-branch"
    exit 1
fi

# Тесты гоняются по рабочей копии, а на сервер уходит коммит — они должны совпадать.
if [ -n "$(git status --porcelain -- .)" ]; then
    echo "!! в проекте есть незакоммиченные изменения — закоммитьте их или уберите:"
    git status --short -- . | head -10
    exit 1
fi

echo ">> проверки $REV"
uvx ruff check app tests deploy
uv run pytest -q --cov 2>&1 | tail -3
# pipefail: код выхода pytest (тесты или порог покрытия) не теряется за tail
echo ">> проверки пройдены"

if [ "$CHECK_ONLY" = 1 ]; then
    exit 0
fi

# Архив собираем заранее и проверяем: пустой архив (например, неверный путь) раньше молча стирал код на сервере.
ARCHIVE=$(mktemp)
trap 'rm -f "$ARCHIVE"' EXIT
# Из корня репозитория: из подпапки git archive кладёт в архив только её же пути — и архив выходит пустым.
git -C "$(git rev-parse --show-toplevel)" archive --format=tar -o "$ARCHIVE" "HEAD:${PREFIX%/}"
# Без grep -q: он выходит на первой находке, tar получает SIGPIPE, и при pipefail проверка «проваливается».
if ! tar -tf "$ARCHIVE" | grep -x 'app/main.py' >/dev/null; then
    echo "!! в архиве нет app/main.py — выкладка отменена, сервер не тронут"
    exit 1
fi

echo ">> $HOST: выкладываю $REV"
# Сначала распаковываем рядом; бэкап, замену кода и установку делает release.sh (из локальной копии — stdin).
ssh "$HOST" 'set -e; rm -rf /opt/ceiling-bot.new && mkdir -p /opt/ceiling-bot.new && tar -x -C /opt/ceiling-bot.new' \
    < "$ARCHIVE"
ssh "$HOST" "bash -s -- install $REV" < deploy/release.sh
