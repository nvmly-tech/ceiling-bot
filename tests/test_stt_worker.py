"""Сервис GigaAM (deploy/stt/worker.py): один процесс, модель грузится один раз, подключения — по одному.

Раньше на каждое голосовое systemd запускал новый процесс с холодной загрузкой модели (~7 с, ~0,9 ГБ), а бот,
не дождавшись ответа, отключался — процесс досчитывал, и следующий голос поднимал второй: две модели на сервере
с 2 ГБ. Здесь модель — подставная: проверяется обслуживание, а не распознавание.
"""

import importlib.util
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "stt_worker", Path(__file__).resolve().parents[1] / "deploy" / "stt" / "worker.py"
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FakeModel:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay, self.fail = delay, fail
        self.calls = 0
        self.active = self.peak = 0
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self.calls > 0

    def transcribe(self, audio: bytes) -> str:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if self.fail:
                raise RuntimeError("модель упала")
            return f"текст {len(audio)} байт"
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def server(tmp_path):
    """Запустить serve() в потоке на unix-сокете; вернуть (путь, поток)."""
    threads = []

    def start(model, **kw):
        path = str(tmp_path / f"stt{len(threads)}.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(8)
        t = threading.Thread(target=worker.serve, args=(listener, model), kwargs=kw, daemon=True)
        t.start()
        threads.append((t, listener))
        return path, t

    yield start
    for t, listener in threads:
        t.join(timeout=3)
        listener.close()


def ask(path: str, payload: bytes, timeout: float = 5) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(path)
    s.sendall(payload)
    s.shutdown(socket.SHUT_WR)
    data = b"".join(iter(lambda: s.recv(65536), b""))
    s.close()
    return json.loads(data)


def test_ping_does_not_load_model_transcribe_reuses_it(server):
    model = FakeModel()
    path, _ = server(model, idle_exit=0.5)
    assert ask(path, b"PING\n") == {"ok": True}
    assert not model.loaded
    assert ask(path, b"TRANSCRIBE\n" + b"x" * 10) == {"text": "текст 10 байт"}
    assert ask(path, b"TRANSCRIBE\n" + b"y" * 3) == {"text": "текст 3 байт"}
    assert model.calls == 2  # одна модель на все запросы процесса


def test_requests_are_served_one_at_a_time(server):
    model = FakeModel(delay=0.15)
    path, _ = server(model, idle_exit=1)
    answers = []
    clients = [threading.Thread(target=lambda: answers.append(ask(path, b"TRANSCRIBE\nabc"))) for _ in range(3)]
    for c in clients:
        c.start()
    for c in clients:
        c.join(timeout=5)
    assert len(answers) == 3 and all("text" in a for a in answers)
    assert model.peak == 1  # никогда две расшифровки (и две модели) одновременно


def test_exits_after_idle_and_ping_does_not_keep_it(server):
    model = FakeModel()
    path, thread = server(model, idle_exit=0.3)
    ask(path, b"TRANSCRIBE\nabc")
    start = time.monotonic()
    time.sleep(0.15)
    ask(path, b"PING\n")  # проверка сторожа не держит модель в памяти
    thread.join(timeout=2)
    assert not thread.is_alive() and time.monotonic() - start < 1


def test_silent_client_does_not_block_others(server):
    path, _ = server(FakeModel(), idle_exit=1, read_timeout=0.2)
    silent = socket.socket(socket.AF_UNIX)
    silent.connect(path)  # подключился и молчит
    started = time.monotonic()
    assert ask(path, b"PING\n") == {"ok": True}
    assert time.monotonic() - started < 1
    silent.close()


@pytest.mark.parametrize(("payload", "error"), [
    (b"HELLO\n", "неизвестная команда"),
    (b"TRANSCRIBE\n", "нет аудио или больше 20 МБ"),
])
def test_bad_requests(server, payload, error):
    path, _ = server(FakeModel(), idle_exit=0.5)
    assert ask(path, payload) == {"error": error}


def test_model_failure_is_reported_and_server_keeps_working(server):
    path, _ = server(FakeModel(fail=True), idle_exit=0.5)
    assert ask(path, b"TRANSCRIBE\nabc") == {"error": "RuntimeError"}
    assert ask(path, b"PING\n") == {"ok": True}


def test_listener_from_systemd():
    real = socket.socket(socket.AF_UNIX)
    fd = os.dup(real.fileno())
    try:
        with pytest.raises(SystemExit):
            worker.systemd_listener({"LISTEN_FDS": "1", "LISTEN_PID": "1"}, pid=2, fd=fd)  # не нам
        with pytest.raises(SystemExit):
            worker.systemd_listener({}, pid=2, fd=fd)  # запущен не через сокет systemd
        listener = worker.systemd_listener({"LISTEN_FDS": "1", "LISTEN_PID": "2"}, pid=2, fd=fd)
        assert listener.family == socket.AF_UNIX
        listener.close()
    finally:
        real.close()
