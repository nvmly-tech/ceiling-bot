#!/usr/bin/env bash
# Установка/обновление сервиса расшифровки GigaAM на сервере. Запускается от root (это делает deploy_stt.sh)
# из /opt/ceiling-bot/deploy/stt — туда код приносит обычный deploy.sh. Модель выкладывает deploy_stt.sh.
set -euo pipefail

APP=/opt/ceiling-bot-stt
SRC=$(cd "$(dirname "$0")" && pwd)
MODEL=v3_e2e_rnnt
# Проверенный коммит GigaAM (замеры — в сессии 006); обновлять осознанно, с повторным замером.
GIGAAM="gigaam[torch] @ git+https://github.com/salute-developers/GigaAM@7447938"

command -v ffmpeg >/dev/null || { echo "!! нужен ffmpeg: apt install ffmpeg"; exit 1; }
command -v uv >/dev/null || { echo "!! нет uv — сначала выложите бота (deploy.sh ставит uv)"; exit 1; }
[ -f "$APP/models/${MODEL}_int8/${MODEL}_encoder.onnx" ] || { echo "!! нет модели в $APP/models — её выкладывает deploy_stt.sh"; exit 1; }

mkdir -p "$APP"
if [ "$(cat "$APP/venv/.spec" 2>/dev/null)" != "$GIGAAM" ]; then
    echo ">> окружение GigaAM (Python 3.12, torch CPU) в $APP/venv"
    export UV_PYTHON_INSTALL_DIR="$APP/python"
    rm -rf "$APP/venv"
    nice -n 10 uv venv -q --python 3.12 "$APP/venv"
    VIRTUAL_ENV="$APP/venv" nice -n 10 uv pip install -q --index-strategy unsafe-best-match \
        --extra-index-url https://download.pytorch.org/whl/cpu "$GIGAAM"
    echo "$GIGAAM" > "$APP/venv/.spec"
fi
install -m 644 "$SRC/worker.py" "$APP/worker.py"
# Обработчик работает от временного пользователя: читать может всё, писать — никто.
chmod -R a+rX,go-w "$APP"

for unit in ceiling-bot-stt.socket ceiling-bot-stt@.service; do
    install -m 644 "$SRC/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --quiet ceiling-bot-stt.socket
systemctl restart ceiling-bot-stt.socket

echo ">> проверка: PING и расшифровка секунды тишины"
python3 - <<'PY'
import json, socket, subprocess
def ask(payload: bytes, timeout: float) -> dict:
    s = socket.socket(socket.AF_UNIX); s.settimeout(timeout); s.connect("/run/ceiling-bot-stt.sock")
    s.sendall(payload); s.shutdown(socket.SHUT_WR)
    data = b"".join(iter(lambda: s.recv(65536), b"")); s.close()
    return json.loads(data)
print("   PING:", ask(b"PING\n", 10))
silence = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1",
                          "-c:a", "libopus", "-f", "ogg", "-"], capture_output=True, check=True).stdout
answer = ask(b"TRANSCRIBE\n" + silence, 120)
print("   TRANSCRIBE:", answer)
if "error" in answer:
    raise SystemExit("!! расшифровка не работает — journalctl -u 'ceiling-bot-stt@*' -n 50")
PY
