#!/usr/bin/env bash
# Публикация закоммиченной версии на сервер: ./deploy/deploy.sh [host]  (по умолчанию aezn1)
# Незакоммиченные изменения на сервер НЕ попадают. .env и базы — тоже (их нет в git).
set -euo pipefail

HOST=${1:-aezn1}
cd "$(git rev-parse --show-toplevel)"
REV=$(git rev-parse --short HEAD)
if ! git diff --quiet HEAD -- ceiling-bot; then
    echo "!! в ceiling-bot/ есть незакоммиченные изменения — на сервер уйдёт $REV без них"
fi

echo ">> $HOST: выкладываю $REV"
git archive --format=tar HEAD:ceiling-bot | ssh "$HOST" '
    set -e
    mkdir -p /opt/ceiling-bot && cd /opt/ceiling-bot
    find . -mindepth 1 -maxdepth 1 ! -name .venv ! -name python -exec rm -rf {} +
    tar -x
    echo '"$REV"' > REVISION
    bash deploy/install.sh'
