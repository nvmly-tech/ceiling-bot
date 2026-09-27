"""Расшифровка голосового через GigaAM — один запрос на процесс.

Запускает systemd: ceiling-bot-stt.socket (Accept=yes) на каждое подключение стартует
ceiling-bot-stt@.service, stdin и stdout которого — это подключение. Модель живёт в памяти только
на время запроса, поэтому рядом с ботом на сервере с 2 ГБ ей хватает места.

Работает в своём окружении (/opt/ceiling-bot-stt/venv: gigaam, onnxruntime, torch CPU), не в venv бота.

Протокол (app/services/stt.py, GigaAMTranscriber):
  запрос — первая строка «PING» или «TRANSCRIBE», для TRANSCRIBE дальше аудио до конца потока;
  ответ — одна строка JSON: {"ok": true} | {"text": "..."} | {"error": "..."}.
"""

import json
import os
import sys
import tempfile

MODEL = os.environ.get("GIGAAM_MODEL", "v3_e2e_rnnt")
MODEL_DIR = os.environ.get("GIGAAM_MODEL_DIR", f"/opt/ceiling-bot-stt/models/{MODEL}_int8")
MAX_AUDIO = 20 * 1024 * 1024  # голосовые у бота — до 10 МБ (лимит скачивания из Telegram)
SAMPLE_RATE = 16000
# GigaAM распознаёт куски до 25 с; длиннее режем сами по самой тихой точке, без pyannote и токена HF.
CHUNK_MAX_S, CHUNK_MIN_S, WINDOW_S = 22.0, 12.0, 0.25


def reply(obj: dict) -> None:
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
    sys.stdout.buffer.flush()


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


def transcribe(audio: bytes) -> str:
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

    sessions, cfg = load_onnx(MODEL_DIR, MODEL)
    cfg.decoding.model_path = f"{MODEL_DIR}/{MODEL}_tokenizer.model"  # в yaml — путь машины, где модель готовили
    with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
        f.write(audio)
        f.flush()
        wav = load_audio(f.name).numpy()  # ffmpeg: любой формат → 16 кГц моно
    texts = infer_onnx(split(wav, np), cfg, sessions, progress=False, batch_size=1)
    return " ".join(t.strip() for t in texts if t.strip())


def main() -> None:
    command = sys.stdin.buffer.readline(64).strip()
    if command == b"PING":
        reply({"ok": True})
        return
    if command != b"TRANSCRIBE":
        reply({"error": "неизвестная команда"})
        return
    audio = sys.stdin.buffer.read(MAX_AUDIO + 1)
    if not audio or len(audio) > MAX_AUDIO:
        reply({"error": "нет аудио или больше 20 МБ"})
        return
    try:
        reply({"text": transcribe(audio)})
    except Exception as e:  # noqa: BLE001 — любая ошибка уходит боту, он переключится на Groq
        print(f"gigaam: {type(e).__name__}: {e}", file=sys.stderr)
        reply({"error": type(e).__name__})


if __name__ == "__main__":
    main()
