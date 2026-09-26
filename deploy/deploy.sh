#!/usr/bin/env bash
# Публикация закоммиченной версии на сервер.
#   ./deploy/deploy.sh [host]          — проверки, затем выкладка (host по умолчанию aezn1)
#   ./deploy/deploy.sh [host] --check  — только проверки, без выкладки
# Перед выкладкой: нет незакоммиченных изменений в ceiling-bot/, линтер чист, тесты зелёные, покрытие не ниже
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

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
REV=$(git rev-parse --short HEAD)

# Тесты гоняются по рабочей копии, а на сервер уходит коммит — они должны совпадать.
if [ -n "$(git status --porcelain -- ceiling-bot)" ]; then
    echo "!! в ceiling-bot/ есть незакоммиченные изменения — закоммитьте их или уберите:"
    git status --short -- ceiling-bot | head -10
    exit 1
fi

echo ">> проверки $REV"
cd "$ROOT/ceiling-bot"
uvx ruff check app tests deploy
uv run pytest -q --cov 2>&1 | tail -3
# pipefail: код выхода pytest (тесты или порог покрытия) не теряется за tail
echo ">> проверки пройдены"

if [ "$CHECK_ONLY" = 1 ]; then
    exit 0
fi

cd "$ROOT"
echo ">> $HOST: выкладываю $REV"
git archive --format=tar HEAD:ceiling-bot | ssh "$HOST" '
    set -e
    mkdir -p /opt/ceiling-bot && cd /opt/ceiling-bot
    find . -mindepth 1 -maxdepth 1 ! -name .venv ! -name python -exec rm -rf {} +
    tar -x
    echo '"$REV"' > REVISION
    bash deploy/install.sh'
