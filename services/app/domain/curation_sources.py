"""Common discovery sources. Credentials and arbitrary remote URLs never enter snapshots."""

import hashlib
import re
from datetime import UTC, datetime

from app.adapters.catalog_types import cover_url
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.goodreads_discovery import CollectionBook
from app.config import get_settings
from app.domain.catalog_network import CatalogGateway


def entry_id(provider, title, authors):
    return (
        provider
        + "-"
        + hashlib.sha256((title + "\0" + "\0".join(authors)).encode()).hexdigest()[:24]
    )


def snapshot(key, provider, title, url, *, books=None, **extra):
    books = books or []
    return dict(
        id=key,
        provider=provider,
        title=title,
        source_url=url,
        kind="chart",
        genres=[],
        books=books,
        count=len(books),
        coverage="partial",
        updated_at=datetime.now(UTC).isoformat(),
        **extra,
    )


def nyt_collections():
    if not get_settings().nyt_api_key:
        return {}
    return {
        "nyt-" + slug: snapshot(
            "nyt-" + slug,
            "nyt",
            title,
            "https://www.nytimes.com/books/best-sellers/" + slug + "/",
            description="The New York Times bestseller list. Open to load the current edition.",
            language="en",
            audience="all",
            refresh_mode="live",
        )
        for slug, title in [
            ("combined-print-and-e-book-fiction", "NYT · Fiction"),
            ("combined-print-and-e-book-nonfiction", "NYT · Nonfiction"),
            ("hardcover-fiction", "NYT · Hardcover fiction"),
            ("young-adult-hardcover", "NYT · Young adult"),
            ("audio-fiction", "NYT · Audio fiction"),
            ("audio-nonfiction", "NYT · Audio nonfiction"),
        ]
    }


AUDIBLE_SHELVES = {"popular": "charts/best", "releases": "newreleases", "upcoming": "coming-soon"}


def storefront_asins(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    identifiers = []
    for row in soup.select(".productListItem")[:50]:
        match = re.fullmatch(r"product-list-item-([A-Z0-9]{10})", row.get("id", ""))
        if match and match[1] not in identifiers:
            identifiers.append(match[1])
    if not identifiers:
        raise AdapterError(FailureKind.PARSER, "Audible’s storefront layout could not be read")
    return identifiers


def audible_collections():
    from app.adapters.audible import MARKETPLACES

    region = get_settings().audible_region
    return {
        f"audible-{region}-{kind}": snapshot(
            f"audible-{region}-{kind}",
            "audible",
            title,
            f"https://www.audible.{MARKETPLACES[region]}/{AUDIBLE_SHELVES[kind]}",
            description=description,
            medium="audio",
            region=region,
            refresh_mode="live",
        )
        for kind, title, description in [
            (
                "popular",
                "Audible bestsellers",
                "Selected from Audible’s bestseller storefront in your server’s marketplace.",
            ),
            (
                "releases",
                "Recent Audible releases",
                "Recording releases in the last 30 days. A new recording can be an older book.",
            ),
            (
                "upcoming",
                "Coming to Audible",
                "Scheduled recording releases in the next 90 days. Dates may change.",
            ),
        ]
    }


async def fetch(value):
    try:
        return await _fetch(value)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise AdapterError(
            FailureKind.PARSER, "The collection source returned invalid entries"
        ) from error


async def _fetch(value):
    provider = value.get("provider", "goodreads")
    if provider == "nyt":
        return await fetch_nyt(value)
    if provider == "audible":
        return await fetch_audible(value)
    if provider == "goodreads":
        from app.adapters.goodreads_discovery import fetch_collection

        return await fetch_collection(value["source_url"])
    from app.domain.discovery_catalog import catalog

    # Official awards are editorially verified, versioned facts. App updates refresh
    # their manifests; runtime workers never scrape an unverified redesign as an empty list.
    if value["id"] in catalog():
        return dict(catalog()[value["id"]])
    raise AdapterError(FailureKind.NOT_FOUND, "Collection is no longer available")


async def fetch_nyt(value):
    if not get_settings().nyt_api_key:
        raise AdapterError(
            FailureKind.AUTHENTICATION, "The New York Times API key is not configured"
        )
    slug = value["id"].removeprefix("nyt-")
    if not re.fullmatch(r"[a-z0-9-]{1,100}", slug):
        raise AdapterError(FailureKind.PARSER, "Invalid New York Times list")
    async with CatalogGateway("nyt", "public") as gateway:
        data = await gateway.request("GET", f"svc/books/v3/lists/current/{slug}.json")
        if gateway.stale:
            raise AdapterError(
                FailureKind.UNAVAILABLE, "New York Times refresh is temporarily unavailable"
            )
    results = data.get("results") or {}
    rows = results.get("books")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise AdapterError(FailureKind.PARSER, "The New York Times list response is incomplete")
    books = []
    for row in rows:
        title, author = row.get("title"), row.get("author")
        if not title or not author:
            raise AdapterError(FailureKind.PARSER, "The New York Times entry is incomplete")
        isbn = row.get("primary_isbn13") or row.get("primary_isbn10")
        book = CollectionBook(
            provider="nyt",
            external_id=entry_id("nyt", title, [author]),
            title=title,
            authors=[author],
            source_url=value["source_url"],
            rank=row.get("rank"),
            cover_url=cover_url(row.get("book_image")),
            language="en",
            identifiers={"isbn": isbn} if isbn and isbn.strip("0") else {},
        )
        books.append(book.model_dump(mode="json"))
    return {
        **value,
        "books": books,
        "count": len(books),
        "coverage": "complete",
        "edition_date": results.get("published_date"),
        "description": "Current New York Times bestseller rankings.",
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def fetch_audible(value):
    from datetime import timedelta

    from app.adapters.audible import GROUPS, product

    if value.get("region") != get_settings().audible_region:
        raise AdapterError(
            FailureKind.UNSUPPORTED, "This shelf belongs to a different Audible marketplace"
        )
    kind = value["id"].rsplit("-", 1)[-1]
    today = datetime.now(UTC).date()
    async with CatalogGateway("audible-storefront", "public") as gateway:
        page = await gateway.request("GET", AUDIBLE_SHELVES[kind])
        if gateway.stale:
            raise AdapterError(
                FailureKind.UNAVAILABLE, "Audible storefront is temporarily unavailable"
            )
        try:
            identifiers = storefront_asins(page["html"])
        except AdapterError:
            await gateway.invalidate()
            raise
    async with CatalogGateway("audible", "public") as gateway:
        data = await gateway.request(
            "GET",
            "1.0/catalog/products",
            params={
                "asins": ",".join(identifiers),
                "num_results": 50,
                "response_groups": GROUPS,
            },
        )
        if gateway.stale:
            raise AdapterError(
                FailureKind.UNAVAILABLE, "Audible refresh is temporarily unavailable"
            )
    rows = data.get("products")
    if not isinstance(rows, list) or not rows or len(rows) > 50:
        raise AdapterError(FailureKind.PARSER, "The Audible collection response is incomplete")
    books = []
    products = {row.get("asin"): row for row in rows if isinstance(row, dict)}
    for rank, identifier in enumerate(identifiers, 1):
        if identifier not in products:
            continue
        book = product(products[identifier])
        edition = book.editions[0]
        # Storefronts can change during refresh. Validate the actual recording date too;
        # never label a released recording as upcoming or a preorder as recent.
        release = edition.release_date or ""
        if kind == "upcoming" and not str(today) < release <= str(today + timedelta(days=90)):
            continue
        if kind == "releases" and not str(today - timedelta(days=30)) <= release <= str(today):
            continue
        books.append(
            CollectionBook(
                provider="audible",
                external_id=book.external_id,
                title=book.title,
                authors=book.authors,
                cover_url=book.cover_url,
                identifiers=edition.identifiers,
                language=book.language,
                subject="recording",
                narrators=edition.narrators,
                source_url=book.source_url,
                rank=rank if kind == "popular" else None,
            ).model_dump(mode="json")
        )
    if not books:
        raise AdapterError(
            FailureKind.UNAVAILABLE, "No verified recordings were returned for this shelf"
        )
    return {
        **value,
        "books": books,
        "count": len(books),
        "coverage": "partial",
        "updated_at": datetime.now(UTC).isoformat(),
    }
