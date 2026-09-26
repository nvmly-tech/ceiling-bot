#!/usr/bin/env bash
# Установка/обновление бота на сервере. Запускается от root из /opt/ceiling-bot (это делает deploy.sh).
set -euo pipefail

APP=/opt/ceiling-bot
ENV_DIR=/etc/Solaris
ENV_FILE=$ENV_DIR/ceiling-bot.env
UNIT=ceiling-bot.service

cd "$APP"

if ! command -v uv >/dev/null; then
    echo ">> ставлю uv"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

echo ">> зависимости (Python 3.12 в $APP/python, venv в $APP/.venv)"
export UV_PYTHON_INSTALL_DIR="$APP/python"
uv sync --frozen --no-dev --python 3.12 --quiet
chmod -R a+rX "$APP"   # сервис работает от временного пользователя — код и python должны быть читаемы

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
    # DB_PATH задаёт unit-файл; строка из шаблона перекрыла бы его (EnvironmentFile сильнее Environment).
    grep -v '^DB_PATH=' .env.example > "$ENV_FILE"
    echo "!! создан $ENV_FILE из шаблона — впишите ключи и запустите: systemctl restart $UNIT"
fi
chmod 600 "$ENV_FILE"
if grep -q '^DB_PATH=' "$ENV_FILE"; then
    echo "!! в $ENV_FILE есть DB_PATH — он перекроет путь к базе из unit-файла; удалите строку, если это не намеренно"
fi

for unit in $UNIT ceiling-bot-alert.service ceiling-bot-backup.service ceiling-bot-backup.timer; do
    install -m 644 "deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --quiet $UNIT
systemctl enable --quiet --now ceiling-bot-backup.timer
systemctl restart $UNIT
sleep 5
systemctl --no-pager --lines=15 status $UNIT
