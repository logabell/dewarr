import os
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock

import psycopg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url

from app import container
from app.container import (
    configure_environment,
    connect_database,
    describe_database_error,
    ensure_key,
    run_migrations,
    supervise,
)


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in list(os.environ):
        if key.startswith(("BOOK_", "DB_")) or key in {"CONFIG_DIR", "PUBLIC_URL"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PASSWORD", "test/password@with:special#characters%")
    return tmp_path


def test_inline_database_password_is_encoded_and_https_uses_secure_cookies(
    environment, monkeypatch
):
    monkeypatch.setenv("PUBLIC_URL", "https://books.example.com")
    assert configure_environment() == environment
    url = make_url(os.environ["BOOK_DATABASE_URL"])
    assert url.password == "test/password@with:special#characters%"
    assert url.host == "postgres"
    assert os.environ["BOOK_COOKIE_SECURE"] == "true"
    assert os.environ["BOOK_SECRET_KEY_FILE"] == str(environment / "app_key")


def test_existing_book_settings_take_precedence(environment, monkeypatch):
    monkeypatch.setenv("BOOK_DATABASE_URL", "postgresql+psycopg://existing/db")
    monkeypatch.setenv("BOOK_PUBLIC_URL", "http://existing:8000")
    monkeypatch.setenv("BOOK_SECRET_KEY_FILE", "/run/secrets/app_key")
    configure_environment()
    assert os.environ["BOOK_DATABASE_URL"] == "postgresql+psycopg://existing/db"
    assert os.environ["BOOK_PUBLIC_URL"] == "http://existing:8000"
    assert os.environ["BOOK_SECRET_KEY_FILE"] == "/run/secrets/app_key"


@pytest.mark.parametrize("mask", ["002", "022", "0002"])
def test_dropping_root_keeps_compose_groups_but_never_the_root_group(monkeypatch, tmp_path, mask):
    dropped = {}
    monkeypatch.setenv("UMASK", mask)
    monkeypatch.setenv("PUID", "1000")
    monkeypatch.setenv("PGID", "1000")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getgroups", lambda: [0, 1001, 44])
    monkeypatch.setattr(os, "chown", lambda *args: None)
    monkeypatch.setattr(os, "umask", lambda mask: dropped.update(mask=mask))
    monkeypatch.setattr(os, "setgroups", lambda groups: dropped.update(groups=groups))
    monkeypatch.setattr(os, "setgid", lambda gid: dropped.update(gid=gid))
    monkeypatch.setattr(os, "setuid", lambda uid: dropped.update(uid=uid))
    container.prepare_user(tmp_path)
    assert dropped == {"groups": [44, 1000, 1001], "gid": 1000, "uid": 1000, "mask": int(mask, 8)}


DATABASE_URL = "postgresql://dewarr:s3cret@dewarr-postgres:5432/dewarr"


@pytest.mark.parametrize(
    ("driver_message", "expected"),
    [
        (
            "failed to resolve host 'dewarr-postgres': [Errno -2] Name or service not known",
            "DB_HOST dewarr-postgres does not resolve",
        ),
        (
            'connection failed: connection to server at "172.18.0.2", port 5432 failed: '
            "could not receive data from server: Connection refused",
            "nothing accepted connections at dewarr-postgres:5432",
        ),
        ("connection timeout expired", "dewarr-postgres:5432 did not answer"),
        (
            'connection failed: connection to server at "172.18.0.2", port 5432 failed: '
            'FATAL:  password authentication failed for user "dewarr"',
            "rejected DB_PASSWORD for DB_USER dewarr",
        ),
        ('FATAL:  database "dewarr" does not exist', "DB_NAME dewarr does not exist"),
        ("server closed the connection unexpectedly", "unavailable at dewarr-postgres:5432"),
    ],
)
def test_database_errors_name_the_failed_step(driver_message, expected):
    message = describe_database_error(psycopg.OperationalError(driver_message), DATABASE_URL)
    assert expected in message
    assert " ".join(driver_message.split()) in message


def test_database_error_never_echoes_the_password():
    error = psycopg.OperationalError("unexpected reply containing s3cret")
    message = describe_database_error(error, DATABASE_URL)
    assert "s3cret" not in message


def test_unreachable_database_logs_and_reports_the_driver_reason(environment, monkeypatch, caplog):
    configure_environment()
    attempts = []

    def unresolvable(*args, **kwargs):
        attempts.append(1)
        raise psycopg.OperationalError(
            "failed to resolve host 'postgres': Name or service not known"
        )

    monkeypatch.setattr(psycopg, "connect", unresolvable)
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    with caplog.at_level("WARNING", logger="dewarr"):
        with pytest.raises(RuntimeError, match="DB_HOST postgres does not resolve"):
            connect_database(stop, timeout=0.05)
    assert len(attempts) > 1
    assert caplog.text.count("Waiting for database") == 1


def test_lost_connection_during_startup_is_described(environment, monkeypatch, caplog):
    def disconnect(*args):
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(container, "prepare_user", lambda config: None)
    monkeypatch.setattr(container, "connect_database", lambda stop: MagicMock())
    monkeypatch.setattr(container, "ensure_key", disconnect)
    with caplog.at_level("ERROR", logger="dewarr"):
        assert container.main() == 1
    assert "server closed the connection unexpectedly" in caplog.text
    assert "test/password" not in caplog.text


def test_config_key_is_created_once_and_keeps_credentials_decryptable(environment):
    configure_environment()
    database = MagicMock()
    database.execute.return_value.fetchone.return_value = [False]
    ensure_key(database, environment)
    path = environment / "app_key"
    key = path.read_bytes().strip()
    assert path.stat().st_mode & 0o777 == 0o600
    encrypted = Fernet(key).encrypt(b"saved provider credential")
    ensure_key(database, environment)
    assert Fernet(path.read_bytes().strip()).decrypt(encrypted) == b"saved provider credential"
    assert path.read_bytes().strip() == key


def test_missing_config_cannot_silently_replace_an_existing_database_key(environment):
    configure_environment()
    database = MagicMock()
    database.execute.return_value.fetchone.return_value = [True]
    with pytest.raises(RuntimeError, match="restore the original encryption key"):
        ensure_key(database, environment)
    assert not (environment / "app_key").exists()


def test_explicit_missing_key_is_not_replaced(environment, monkeypatch):
    monkeypatch.setenv("BOOK_SECRET_KEY_FILE", str(environment / "missing"))
    configure_environment()
    with pytest.raises(RuntimeError, match="does not exist"):
        ensure_key(MagicMock(), environment)


def test_failed_migration_is_fatal_and_releases_lock(monkeypatch):
    database = MagicMock()
    database.execute.return_value.fetchone.return_value = [True]
    child = MagicMock()
    child.poll.return_value = child.returncode = 1
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(RuntimeError, match="migration failed"):
        run_migrations(database, threading.Event())
    assert "pg_advisory_unlock" in database.execute.call_args.args[0]


def wait_for_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def test_service_failure_stops_sibling_and_returns_failure(tmp_path):
    marker = tmp_path / "worker-stopped"
    ready = tmp_path / "worker-ready"
    worker = (
        "import signal,time; from pathlib import Path; "
        f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(marker)!r}).touch(), exit(0))); "
        f"Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    failed = (
        "import time; from pathlib import Path; "
        f"p=Path({str(ready)!r}); "
        "\nwhile not p.exists(): time.sleep(0.02)\nraise SystemExit(7)"
    )
    assert (
        supervise(
            [[sys.executable, "-c", worker], [sys.executable, "-c", failed]], threading.Event()
        )
        == 7
    )
    assert marker.exists()


def test_stop_event_gracefully_terminates_both_services(tmp_path):
    stop = threading.Event()
    commands = []
    for name in ("api", "worker"):
        commands.append(
            [
                sys.executable,
                "-c",
                (
                    "import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, lambda *_: "
                    f"(Path({str(tmp_path / (name + '-stopped'))!r}).touch(), exit(0))); "
                    f"Path({str(tmp_path / (name + '-ready'))!r}).touch(); time.sleep(30)"
                ),
            ]
        )
    thread = threading.Thread(
        target=lambda: (
            wait_for_file(tmp_path / "api-ready"),
            wait_for_file(tmp_path / "worker-ready"),
            stop.set(),
        )
    )
    thread.start()
    try:
        assert supervise(commands, stop) == 0
    finally:
        stop.set()
        thread.join(timeout=6)
    assert (tmp_path / "api-stopped").exists()
    assert (tmp_path / "worker-stopped").exists()


@pytest.mark.parametrize("url", ["HTTPS://books.example.com", "Https://BOOKS.example.com:443/"])
def test_https_scheme_case_keeps_cookies_secure(environment, monkeypatch, url):
    monkeypatch.setenv("PUBLIC_URL", url)
    configure_environment()
    assert os.environ["BOOK_PUBLIC_URL"] == "https://books.example.com"
    assert os.environ["BOOK_COOKIE_SECURE"] == "true"


def test_explicit_cookie_override_and_public_url_precedence(environment, monkeypatch, caplog):
    monkeypatch.setenv("PUBLIC_URL", "http://old.example")
    monkeypatch.setenv("BOOK_PUBLIC_URL", "HTTPS://books.example.com")
    monkeypatch.setenv("BOOK_COOKIE_SECURE", "false")
    configure_environment()
    assert os.environ["BOOK_PUBLIC_URL"] == "https://books.example.com"
    assert os.environ["BOOK_COOKIE_SECURE"] == "false"
    assert "overrides a different PUBLIC_URL" in caplog.text


def test_invalid_public_url_fails_with_configuration_hint(environment, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://user:private-password@books.example")
    with pytest.raises(RuntimeError, match="Invalid PUBLIC_URL/BOOK_PUBLIC_URL") as error:
        configure_environment()
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize("mask", ["888", "777", "foo", "-002", "2"])
def test_invalid_umask_fails_before_changing_storage(monkeypatch, tmp_path, mask):
    monkeypatch.setenv("UMASK", mask)
    with pytest.raises(RuntimeError, match="UMASK"):
        container.prepare_user(tmp_path / "untouched")
    assert not (tmp_path / "untouched").exists()
