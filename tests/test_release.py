"""Серверная часть выкладки (deploy/release.sh) на временных папках: systemctl и install.sh — подставные.

Проверяется: бэкап базы до замены кода, прошлая версия для отката, откат кода и базы, отказ выкладки при
неудачном бэкапе, проверка, что бот поднялся. На сервер ничего не уходит.
"""

import gzip
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "deploy" / "release.sh"

SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$LOG"
case "$*" in
    "cat ceiling-bot-backup.service") [ -n "$NO_BACKUP_UNIT" ] && exit 1; exit 0 ;;
    "start ceiling-bot-backup.service")
        [ -n "$BACKUP_FAILS" ] && exit 1
        mkdir -p "$DATA/backups"
        gzip -c "$DATA/ceiling-bot.sqlite3" > "$DATA/backups/ceiling-bot-$(date +%s%N).sqlite3.gz" ;;
    "is-active --quiet ceiling-bot.service") [ -n "$BOT_DOWN" ] && exit 3; exit 0 ;;
esac
exit 0
"""


def code(path: Path, version: str) -> None:
    """Версия «кода бота»: app/main.py с меткой и install.sh, который пишет в журнал, какая версия ставится."""
    (path / "app").mkdir(parents=True, exist_ok=True)
    (path / "app" / "main.py").write_text(f"VERSION = {version!r}\n")
    (path / "deploy").mkdir(exist_ok=True)
    (path / "deploy" / "install.sh").write_text(f'echo "install {version}" >> "$LOG"\n')


def db_with(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.execute("DELETE FROM t")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


def db_value(path: Path) -> str:
    con = sqlite3.connect(path)
    value = con.execute("SELECT v FROM t").fetchone()[0]
    con.close()
    return value


@pytest.fixture
def server(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "sleep").chmod(0o755)
    env = {
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "APP": str(tmp_path / "opt" / "ceiling-bot"), "DATA": str(tmp_path / "data"), "LOG": str(tmp_path / "log"),
        "WAIT": "2",
    }

    class Server:
        app = Path(env["APP"])
        new = Path(env["APP"] + ".new")
        prev = Path(env["APP"] + ".prev")
        data = Path(env["DATA"])
        db = Path(env["DATA"]) / "ceiling-bot.sqlite3"

        @staticmethod
        def run(*args, **extra_env) -> subprocess.CompletedProcess:
            return subprocess.run(["bash", str(RELEASE), *args], env={**env, **extra_env},
                                  capture_output=True, text=True, timeout=30)

        @staticmethod
        def log() -> list[str]:
            path = Path(env["LOG"])
            return path.read_text().splitlines() if path.exists() else []

        def version(self) -> str:
            return (self.app / "app" / "main.py").read_text()

        def deploy(self, version: str, **extra_env) -> subprocess.CompletedProcess:
            code(self.new, version)
            return self.run("install", version, **extra_env)

    return Server()


def test_first_install(server):
    result = server.deploy("v1")
    assert result.returncode == 0, result.stderr
    assert "v1" in server.version() and (server.app / "REVISION").read_text().strip() == "v1"
    assert "install v1" in server.log()
    assert not server.prev.exists() and not server.new.exists()  # откатывать пока не на что
    assert "systemctl start ceiling-bot-backup.service" not in server.log()  # базы ещё нет


def test_upgrade_backs_up_db_and_keeps_previous_version(server):
    server.deploy("v1")
    (server.app / ".venv").mkdir()
    (server.app / ".venv" / "marker").write_text("окружение")
    db_with(server.db, "данные до выкладки")

    (server.app / "app" / "removed_in_v2.py").write_text("файл, которого в v2 нет")
    result = server.deploy("v2")
    assert result.returncode == 0, result.stderr
    assert not (server.app / "app" / "removed_in_v2.py").exists()  # старые файлы не остаются рядом с новыми
    log = server.log()
    assert log.index("systemctl start ceiling-bot-backup.service") < log.index("install v2")  # бэкап — до замены
    assert "v2" in server.version()
    assert (server.app / ".venv" / "marker").exists()  # окружение Python не трогаем
    assert "v1" in (server.prev / "app" / "main.py").read_text() and not (server.prev / ".venv").exists()
    backup = Path((server.prev / ".predeploy_backup").read_text().strip())
    assert backup.exists() and backup.parent == server.data / "backups"


def test_failed_backup_cancels_release(server):
    server.deploy("v1")
    db_with(server.db, "x")
    result = server.deploy("v2", BACKUP_FAILS="1")
    assert result.returncode != 0 and "бэкап" in result.stdout + result.stderr
    assert "v1" in server.version()  # сервер не тронут
    assert "install v2" not in server.log()


def test_rollback_restores_code_only_by_default(server):
    server.deploy("v1")
    db_with(server.db, "до")
    server.deploy("v2")
    db_with(server.db, "после выкладки")
    result = server.run("rollback")
    assert result.returncode == 0, result.stderr
    assert "v1" in server.version()
    assert server.log()[-2:] == ["install v1", "systemctl is-active --quiet ceiling-bot.service"]
    assert db_value(server.db) == "после выкладки"  # база — как была
    assert not (server.app / ".predeploy_backup").exists()


def test_rollback_with_db_restores_backup_taken_before_release(server):
    server.deploy("v1")
    db_with(server.db, "до выкладки")
    server.deploy("v2")
    db_with(server.db, "после выкладки")
    for suffix in ("-wal", "-shm"):
        Path(str(server.db) + suffix).write_text("служебный файл новой версии")
    result = server.run("rollback", "--with-db")
    assert result.returncode == 0, result.stderr
    assert db_value(server.db) == "до выкладки"
    assert not Path(str(server.db) + "-wal").exists() and not Path(str(server.db) + "-shm").exists()
    log = server.log()
    last_install = len(log) - 1 - log[::-1].index("install v1")  # установка при откате, а не самая первая
    assert log.index("systemctl stop ceiling-bot.service") < last_install


def test_rollback_without_previous_version(server):
    server.deploy("v1")
    result = server.run("rollback")
    assert result.returncode != 0 and "прошлой версии" in result.stdout + result.stderr
    assert "v1" in server.version()


def test_bot_that_does_not_start_is_reported_with_rollback_hint(server):
    result = server.deploy("v1", BOT_DOWN="1")
    assert result.returncode != 0
    assert "--rollback" in result.stdout + result.stderr


def test_unknown_command(server):
    result = server.run("explode")
    assert result.returncode != 0 and "использование" in result.stdout + result.stderr


def test_backups_are_gzip(server):
    server.deploy("v1")
    db_with(server.db, "x")
    server.deploy("v2")
    backup = Path((server.prev / ".predeploy_backup").read_text().strip())
    with gzip.open(backup) as f:
        assert f.read(16).startswith(b"SQLite format 3")
