"""Retire the setup-form scenario's fake endpoints before recovery observes connections."""

import os
from urllib.parse import urlsplit

import psycopg

url = os.environ.get(
    "BOOK_E2E_DATABASE_URL",
    "postgresql+psycopg://book@127.0.0.1:55438/book_search_browser_test",
)
if not urlsplit(url).path.endswith("_browser_test"):
    raise SystemExit("Downloader fixture cleanup requires an isolated _browser_test database")
with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as connection:
    # These rows belong only to downloader-simple.spec.ts. Keep real synthetic providers
    # enabled, and retain the saved settings so all setup assertions remain meaningful.
    connection.execute(
        "UPDATE integrations SET enabled=false WHERE base_url = ANY(%s)",
        (["http://10.0.0.2:8080", "http://10.0.0.3:8080", "http://10.0.0.4:6789"],),
    )
