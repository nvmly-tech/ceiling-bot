#!/usr/bin/env bash
# Публикация закоммиченной версии на сервер.
#   ./deploy/deploy.sh [host]          — проверки, затем выкладка (host по умолчанию aezn1)
#   ./deploy/deploy.sh [host] --check  — только проверки, без выкладки
# Перед выкладкой: нет незакоммиченных изменений в проекте бота, линтер чист, тесты зелёные, покрытие не ниже
# порога из pyproject.toml. Что-то не так — на сервер ничего не уходит. .env и базы в git нет — не уходят тоже.
set -euo pipefail

HOST=aezn1
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        *) HOST=$arg ;;
    esac
done

# Папка проекта — на уровень выше скрипта. Работает и когда бот — корень репозитория, и когда он в подпапке.
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$APP_DIR"
PREFIX=$(git rev-parse --show-prefix)  # путь проекта внутри репозитория ("" — если он и есть корень)
REV=$(git rev-parse --short HEAD)

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
ssh "$HOST" '
    set -e
    # Сначала распаковываем рядом и проверяем, только потом заменяем код (.venv и python не трогаем).
    rm -rf /opt/ceiling-bot.new && mkdir -p /opt/ceiling-bot.new /opt/ceiling-bot
    tar -x -C /opt/ceiling-bot.new
    test -f /opt/ceiling-bot.new/app/main.py
    cd /opt/ceiling-bot
    find . -mindepth 1 -maxdepth 1 ! -name .venv ! -name python -exec rm -rf {} +
    cp -a /opt/ceiling-bot.new/. /opt/ceiling-bot/ && rm -rf /opt/ceiling-bot.new
    echo '"$REV"' > REVISION
    bash deploy/install.sh' < "$ARCHIVE"
