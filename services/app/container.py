"""Single-container startup: persistent key, migrations, API, and worker."""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from sqlalchemy.engine import URL, make_url

LOG = logging.getLogger("dewarr")
MAINTENANCE_LOCK = 720041


def configure_environment() -> Path:
    """Keep BOOK_* overrides compatible with existing installations."""
    config = Path(os.environ.get("CONFIG_DIR", "/config"))
    if not config.is_absolute() or config == Path("/"):
        raise RuntimeError("CONFIG_DIR must be an absolute directory below /")
    if "BOOK_DATABASE_URL" not in os.environ:
        password = os.environ.get("DB_PASSWORD")
        if not password:
            raise RuntimeError("Set DB_PASSWORD to the PostgreSQL password")
        os.environ["BOOK_DATABASE_URL"] = URL.create(
            "postgresql+psycopg",
            username=os.environ.get("DB_USER", "dewarr"),
            password=password,
            host=os.environ.get("DB_HOST", "postgres"),
            port=int(os.environ.get("DB_PORT", "5432")),
            database=os.environ.get("DB_NAME", "dewarr"),
        ).render_as_string(hide_password=False)
    os.environ.setdefault("BOOK_PUBLIC_URL", os.environ.get("PUBLIC_URL", "http://localhost:8000"))
    os.environ.setdefault(
        "BOOK_COOKIE_SECURE", str(os.environ["BOOK_PUBLIC_URL"].startswith("https://")).lower()
    )
    os.environ.setdefault("BOOK_ENV_FILE", "")
    os.environ["HOME"] = str(config)
    if not os.environ.get("BOOK_SECRET_KEY"):
        os.environ.setdefault("BOOK_SECRET_KEY_FILE", str(config / "app_key"))
    return config


def prepare_user(config: Path) -> None:
    """Initialize only the config directory, then permanently drop root."""
    uid = int(os.environ.get("PUID", "1000"))
    gid = int(os.environ.get("PGID", "1000"))
    if uid <= 0 or gid <= 0:
        raise RuntimeError("PUID and PGID must be positive, non-root IDs")
    config.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(config, uid, gid)
        # Do not recursively change ownership of an existing library or media mount.
        key = config / "app_key"
        if key.exists() and not key.is_symlink():
            os.chown(key, uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    elif (os.geteuid(), os.getegid()) != (uid, gid):
        raise RuntimeError("Match PUID/PGID to --user, or omit --user and let Dewarr set them")
    if not os.access(config, os.W_OK):
        raise RuntimeError("The configured PUID/PGID must be able to write /config")
    os.umask(0o022)


DATABASE_HINTS = (
    (
        ("failed to resolve host", "could not translate host name"),
        "DB_HOST {host} does not resolve; attach Dewarr and PostgreSQL to the same Docker network",
    ),
    (
        ("connection refused",),
        "nothing accepted connections at {host}:{port}; check DB_PORT and that PostgreSQL runs",
    ),
    (
        ("timeout expired",),
        "{host}:{port} did not answer; check the Docker network and any firewall",
    ),
    (("password authentication failed",), "PostgreSQL rejected DB_PASSWORD for DB_USER {user}"),
    (('role "',), "DB_USER {user} does not exist in PostgreSQL"),
    (('database "',), "DB_NAME {database} does not exist in PostgreSQL"),
)


def describe_database_error(error: psycopg.OperationalError, url: str) -> str:
    target = make_url(url)
    detail = " ".join(str(error).split()) or type(error).__name__
    if target.password:
        detail = detail.replace(target.password, "***")
    lowered = detail.lower()
    for needles, hint in DATABASE_HINTS:
        if any(needle in lowered for needle in needles):
            summary = hint.format(
                host=target.host,
                port=target.port or 5432,
                user=target.username,
                database=target.database,
            )
            return f"PostgreSQL unavailable: {summary} ({detail})"
    return f"PostgreSQL unavailable at {target.host}:{target.port or 5432} ({detail})"


def connect_database(stop: threading.Event, timeout: float = 60):
    # The key may not exist yet, so do not load Settings before initialization.
    url = os.environ["BOOK_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    deadline = time.monotonic() + timeout
    reported = None
    while not stop.is_set():
        try:
            return psycopg.connect(url, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as error:
            message = describe_database_error(error, url)
            if time.monotonic() >= deadline:
                raise RuntimeError(message) from None
            if message != reported:
                LOG.warning("Waiting for database. %s", message)
                reported = message
            stop.wait(1)
    raise InterruptedError


def ensure_key(connection, config: Path) -> None:
    if os.environ.get("BOOK_SECRET_KEY"):
        Fernet(os.environ["BOOK_SECRET_KEY"].encode())
        return
    path = Path(os.environ["BOOK_SECRET_KEY_FILE"])
    if not path.exists():
        if path != config / "app_key":
            raise RuntimeError("The configured BOOK_SECRET_KEY_FILE does not exist")
        has_users = connection.execute("SELECT to_regclass('public.users') IS NOT NULL").fetchone()[
            0
        ]
        if has_users and connection.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]:
            raise RuntimeError(
                "Existing database: restore the original encryption key to /config/app_key"
            )
        # Exclusive creation prevents an accidental replacement of installation credentials.
        with open(path, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            handle.write(Fernet.generate_key() + b"\n")
        LOG.info("Created installation key in /config")
    Fernet(path.read_bytes().strip())


def stop_children(children, timeout: float = 25) -> None:
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def run_migrations(connection, stop: threading.Event) -> None:
    deadline = time.monotonic() + 60
    while not connection.execute("SELECT pg_try_advisory_lock(%s)", (MAINTENANCE_LOCK,)).fetchone()[
        0
    ]:
        if stop.wait(1):
            raise InterruptedError
        if time.monotonic() >= deadline:
            raise RuntimeError("Stop other Dewarr API/worker containers before upgrading")
    try:
        LOG.info("Preparing database")
        child = subprocess.Popen(["alembic", "upgrade", "head"], start_new_session=True)
        try:
            while child.poll() is None:
                if stop.wait(0.2):
                    raise InterruptedError
            if child.returncode:
                raise RuntimeError("Database migration failed; app services were not started")
        finally:
            stop_children([child])
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (MAINTENANCE_LOCK,))


def supervise(commands: list[list[str]], stop: threading.Event) -> int:
    children = []
    try:
        for command in commands:
            if stop.is_set():
                return 0
            children.append(subprocess.Popen(command, start_new_session=True))
        while not stop.wait(0.2):
            for child in children:
                if child.poll() is not None:
                    LOG.error(
                        "An app service stopped; stopping the container so Docker can restart it"
                    )
                    return child.returncode or 1
        return 0
    finally:
        stop_children(children)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        config = configure_environment()
        prepare_user(config)
        with connect_database(stop) as connection:
            ensure_key(connection, config)
            if len(sys.argv) > 1:
                # Explicit administrative commands retain the same user, key, and DB configuration.
                connection.close()
                os.execvp(sys.argv[1], sys.argv[1:])
            run_migrations(connection, stop)
            recovering = connection.execute(
                "SELECT coalesce(current_setting('book_search.restore_pending', true), '') "
                "= 'true' "
                "OR EXISTS(SELECT 1 FROM restore_checkpoints WHERE active)"
            ).fetchone()[0]
        if stop.is_set():
            return 0
        commands = [
            ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
        ]
        if recovering:
            commands.append([sys.executable, "-m", "app.jobs.worker", "--recovery"])
        elif os.environ.get("BOOK_RECOVERY_MODE", "false").lower() not in {"true", "1", "yes"}:
            commands.append([sys.executable, "-m", "app.jobs.worker"])
        LOG.info("Starting Dewarr")
        return supervise(commands, stop)
    except InterruptedError:
        return 0
    except RuntimeError as error:
        LOG.error("%s", error)
        return 1
    except psycopg.OperationalError as error:
        LOG.error("%s", describe_database_error(error, os.environ["BOOK_DATABASE_URL"]))
        return 1
    except (ValueError, OSError, psycopg.Error) as error:
        # Avoid logging database URLs or credential values from configuration validation.
        LOG.error(
            "Startup failed (%s); check database access, /config permissions and the saved app key",
            type(error).__name__,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
