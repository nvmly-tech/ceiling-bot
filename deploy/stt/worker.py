"""Расшифровка голосовых через GigaAM — один процесс, модель загружается один раз.

Запускает systemd: ceiling-bot-stt.socket (Accept=no) при первом подключении стартует ceiling-bot-stt.service
и отдаёт ему слушающий сокет. Подключения обслуживаются по одному: двух моделей в памяти не бывает, лишние
запросы ждут в очереди сокета. Модель грузится при первой расшифровке (~7 с) и остаётся, пока идут голосовые;
IDLE_EXIT секунд без расшифровок — процесс завершается и отдаёт память, следующее подключение systemd
запустит заново. Проверка сторожа (PING) модель не грузит и процесс не держит.

Раньше на каждое голосовое был свой процесс с холодной загрузкой модели, и бот, не дождавшись ответа, отключался,
а процесс досчитывал — следующий голос поднимал второй: две модели на сервере с 2 ГБ.

Работает в своём окружении (/opt/ceiling-bot-stt/venv: gigaam, onnxruntime, torch CPU), не в venv бота.

Протокол (app/services/stt.py, GigaAMTranscriber), одно подключение — один запрос:
  запрос — первая строка «PING» или «TRANSCRIBE», для TRANSCRIBE дальше аудио до конца потока;
  ответ — одна строка JSON: {"ok": true} | {"text": "..."} | {"error": "..."}.
"""

import json
import os
import socket
import sys
import tempfile
import time
from typing import Protocol

MODEL = os.environ.get("GIGAAM_MODEL", "v3_e2e_rnnt")
MODEL_DIR = os.environ.get("GIGAAM_MODEL_DIR", f"/opt/ceiling-bot-stt/models/{MODEL}_int8")
MAX_AUDIO = 20 * 1024 * 1024  # голосовые у бота — до 10 МБ (лимит скачивания из Telegram)
IDLE_EXIT = float(os.environ.get("GIGAAM_IDLE_EXIT", 600))  # с без расшифровок — выйти и отдать память
READ_TIMEOUT = 30  # с: подключившийся, но молчащий клиент не держит очередь
SAMPLE_RATE = 16000
# GigaAM распознаёт куски до 25 с; длиннее режем сами по самой тихой точке, без pyannote и токена HF.
CHUNK_MAX_S, CHUNK_MIN_S, WINDOW_S = 22.0, 12.0, 0.25
SD_LISTEN_FD = 3  # первый дескриптор, который передаёт systemd (sd_listen_fds)


class Transcriber(Protocol):
    def transcribe(self, audio: bytes) -> str: ...


def log(text: str) -> None:
    print(f"gigaam: {text}", file=sys.stderr, flush=True)


def split(wav, np):
    """Куски не длиннее CHUNK_MAX_S; граница — самое тихое окно между CHUNK_MIN_S и CHUNK_MAX_S."""
    parts, start, w = [], 0, int(WINDOW_S * SAMPLE_RATE)
    while len(wav) - start > CHUNK_MAX_S * SAMPLE_RATE:
        lo, hi = start + int(CHUNK_MIN_S * SAMPLE_RATE), start + int(CHUNK_MAX_S * SAMPLE_RATE)
        energy = [float(np.mean(wav[i:i + w] ** 2)) for i in range(lo, hi - w, w // 2)]
        cut = lo + energy.index(min(energy)) * (w // 2) + w // 2
        parts.append(wav[start:cut])
        start = cut
    parts.append(wav[start:])
    return parts


class GigaAM:
    """Модель грузится при первой расшифровке и остаётся в памяти до выхода процесса."""

    def __init__(self) -> None:
        self._model = None

    def _load(self):
        # Тяжёлые импорты — только здесь: PING отвечает без загрузки модели.
        import numpy as np
        import onnxruntime as rt
        import torch

        torch.set_num_threads(1)
        original = rt.InferenceSession

        def one_thread(path, sess_options=None, *args, **kwargs):
            # load_onnx ставит 8 потоков; на одном ядре сервера это в 5–6 раз медленнее.
            opts = sess_options or rt.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            return original(path, opts, *args, **kwargs)

        rt.InferenceSession = one_thread
        from gigaam.onnx_utils import infer_onnx, load_onnx
        from gigaam.preprocess import load_audio

        started = time.monotonic()
        sessions, cfg = load_onnx(MODEL_DIR, MODEL)
        cfg.decoding.model_path = f"{MODEL_DIR}/{MODEL}_tokenizer.model"  # в yaml — путь машины, где готовили
        log(f"модель загружена за {time.monotonic() - started:.1f} с")
        return np, infer_onnx, load_audio, sessions, cfg

    def transcribe(self, audio: bytes) -> str:
        if self._model is None:
            self._model = self._load()
        np, infer_onnx, load_audio, sessions, cfg = self._model
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
            f.write(audio)
            f.flush()
            wav = load_audio(f.name).numpy()  # ffmpeg: любой формат → 16 кГц моно
        texts = infer_onnx(split(wav, np), cfg, sessions, progress=False, batch_size=1)
        return " ".join(t.strip() for t in texts if t.strip())


def _answer(stream, model: Transcriber) -> tuple[dict, bool]:
    """Ответ на один запрос и была ли это расшифровка."""
    command = stream.readline(64).strip()
    if command == b"PING":
        return {"ok": True}, False
    if command != b"TRANSCRIBE":
        return {"error": "неизвестная команда"}, False
    audio = stream.read(MAX_AUDIO + 1)
    if not audio or len(audio) > MAX_AUDIO:
        return {"error": "нет аудио или больше 20 МБ"}, False
    try:
        return {"text": model.transcribe(audio)}, True
    except Exception as e:  # noqa: BLE001 — любая ошибка уходит боту, он переключится на Groq
        log(f"{type(e).__name__}: {e}")
        return {"error": type(e).__name__}, True


def handle(conn: socket.socket, model: Transcriber, read_timeout: float = READ_TIMEOUT) -> bool:
    """Обслужить одно подключение. True — была расшифровка (продлевает жизнь процесса)."""
    with conn:
        conn.settimeout(read_timeout)
        try:
            with conn.makefile("rb") as stream:
                reply, transcribed = _answer(stream, model)
        except OSError as e:  # клиент подключился и молчит — очередь не держим
            log(f"запрос не получен: {type(e).__name__}")
            return False
        try:
            conn.sendall(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
        except OSError as e:  # бот не дождался и отключился — модель всё равно работала
            log(f"ответ не доставлен: {type(e).__name__}")
    return transcribed


def serve(
    listener: socket.socket, model: Transcriber, *, idle_exit: float = IDLE_EXIT, read_timeout: float = READ_TIMEOUT,
) -> None:
    """Обслуживать подключения по одному, пока не пройдёт idle_exit секунд без расшифровок."""
    last_work = time.monotonic()
    while (left := idle_exit - (time.monotonic() - last_work)) > 0:
        listener.settimeout(left)
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            break
        if handle(conn, model, read_timeout):
            last_work = time.monotonic()
    log(f"{idle_exit:g} с без расшифровок — выхожу, память свободна")


def systemd_listener(env: dict = os.environ, pid: int | None = None, fd: int = SD_LISTEN_FD) -> socket.socket:
    """Слушающий сокет от systemd (Accept=no): LISTEN_FDS=1 и LISTEN_PID — наш процесс."""
    if env.get("LISTEN_FDS") != "1" or env.get("LISTEN_PID") != str(pid or os.getpid()):
        raise SystemExit("запускается только через ceiling-bot-stt.socket (systemd)")
    return socket.socket(fileno=fd)


def main() -> None:
    serve(systemd_listener(), GigaAM())


if __name__ == "__main__":
    main()
