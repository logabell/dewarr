"""Run a disposable browser-test API and worker; never use an ordinary database."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from cryptography.fernet import Fernet

from app.db.models import Base

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from tests.media_fixtures import audio, epub, pdf  # noqa: E402


def shutdown(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 3
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def main():
    url = os.environ.get("BOOK_E2E_DATABASE_URL", "")
    if not urlsplit(url).path.endswith("_browser_test"):
        raise SystemExit(
            "Set BOOK_E2E_DATABASE_URL to an isolated database ending in _browser_test"
        )
    os.chdir(root)
    processes = []
    with tempfile.TemporaryDirectory(prefix="book-search-browser-media-") as media:
        media_root = Path(media).resolve()
        try:
            epub(
                media_root / "completed/book.epub",
                title="The Catalog Journey",
                author="Catalog Author",
            )
            destination_root = media_root / "library"
            staging_root = media_root / "staging"
            destination_root.mkdir()
            staging_root.mkdir(mode=0o700)
            download_root = media_root / "downloads"
            download_root.mkdir()
            (download_root / "bootstrap").mkdir()
            (media_root / "completed").rename(download_root / "completed")
            epub(
                download_root / "matched/book.epub",
                title="The Catalog Journey",
                author="Catalog Author",
                isbn="9781234567897",
            )
            epub(
                download_root / "formats/book.epub", title="Format Review", author="Catalog Author"
            )
            pdf(download_root / "formats/book.pdf", title="Format Review", author="Catalog Author")
            audio(
                download_root / "companion/book.mp3",
                title="Companion Review",
                author="Catalog Author",
            )
            pdf(
                download_root / "companion/notes.pdf",
                title="Supporting notes",
                author="Catalog Author",
            )
            os.environ.update(
                {
                    "BOOK_ENV_FILE": "",
                    "BOOK_DATABASE_URL": url,
                    "BOOK_SECRET_KEY": Fernet.generate_key().decode(),
                    "BOOK_PUBLIC_URL": "http://127.0.0.1:8001",
                    "BOOK_COOKIE_SECURE": "false",
                    "BOOK_DOWNLOAD_DISPATCH_ENABLED": "true",
                    "BOOK_HARDCOVER_URL": "http://127.0.0.1:13379/catalog",
                    "BOOK_OPENLIBRARY_URL": "http://127.0.0.1:13379/openlibrary",
                    "BOOK_IMPORT_SOURCES": json.dumps({"synthetic": str(download_root)}),
                    "BOOK_IMPORT_DESTINATIONS": json.dumps({"ebooks": str(destination_root)}),
                    "BOOK_IMPORT_STAGING_ROOT": str(staging_root),
                }
            )
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], check=True, timeout=60
            )
            # Reset only this specifically named, disposable test database.
            with psycopg.connect(
                url.replace("postgresql+psycopg://", "postgresql://"),
                connect_timeout=5,
                options="-c statement_timeout=15000 -c lock_timeout=5000",
            ) as connection:
                tables = ", ".join(f'public."{name}"' for name in Base.metadata.tables)
                connection.execute(
                    f"TRUNCATE {tables}, book_queue.procrastinate_jobs, "
                    "book_queue.procrastinate_workers RESTART IDENTITY CASCADE"
                )
            commands = [
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "e2e_abs:app",
                    "--app-dir",
                    "scripts",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "13379",
                ],
                [sys.executable, "scripts/e2e_worker.py"],
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "e2e_api:app",
                    "--app-dir",
                    "scripts",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8001",
                ],
            ]
            for command in commands:
                processes.append(subprocess.Popen(command))
            # A dead fixture or worker must fail promptly instead of leaving the API alive.
            while all(process.poll() is None for process in processes):
                time.sleep(0.2)
            raise SystemExit("Browser fixture process exited unexpectedly")
        finally:
            shutdown(processes)


if __name__ == "__main__":

    def interrupted(signum, frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    main()
