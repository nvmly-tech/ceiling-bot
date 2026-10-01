#!/usr/bin/env bash
# Выкладка сервиса расшифровки GigaAM: модель + установка на сервере.
#   ./deploy/stt/deploy_stt.sh <host>
# Код сервиса (worker.py, юниты, install.sh) приносит на сервер обычный deploy.sh — запускать после него.
# Модель готовится локально и разово (экспорт в ONNX + int8, ~1.6 ГБ памяти), лежит в ~/.cache/ceiling-bot-stt.
set -euo pipefail

HOST=${1:-}
[ -n "$HOST" ] || { echo "использование: $0 <host>"; exit 1; }  # адрес — всегда явно
MODEL=v3_e2e_rnnt
GIGAAM="gigaam[torch] @ git+https://github.com/salute-developers/GigaAM@7447938"  # тот же, что в install.sh
HERE=$(cd "$(dirname "$0")" && pwd)
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/ceiling-bot-stt
LOCAL=$CACHE/${MODEL}_int8
REMOTE=/opt/ceiling-bot-stt/models/${MODEL}_int8

if [ ! -f "$LOCAL/SHA256SUMS" ]; then
    echo ">> готовлю модель $MODEL (разово, несколько минут)"
    uv run --no-project --quiet --python 3.12 --index-strategy unsafe-best-match \
        --extra-index-url https://download.pytorch.org/whl/cpu --with "$GIGAAM" \
        python "$HERE/prepare_model.py" "$MODEL" "$LOCAL"
fi

if ssh "$HOST" "cat $REMOTE/SHA256SUMS 2>/dev/null" | cmp -s - "$LOCAL/SHA256SUMS"; then
    echo ">> модель на $HOST уже актуальна"
else
    echo ">> выкладываю модель на $HOST ($(du -sh "$LOCAL" | cut -f1))"
    tar -C "$LOCAL" -cf - . | ssh "$HOST" "set -e
        mkdir -p $(dirname $REMOTE) && rm -rf $REMOTE.new && mkdir $REMOTE.new
        tar -x -C $REMOTE.new && cd $REMOTE.new && sha256sum --quiet -c SHA256SUMS
        rm -rf $REMOTE && mv $REMOTE.new $REMOTE"
fi

ssh "$HOST" 'bash /opt/ceiling-bot/deploy/stt/install.sh'
