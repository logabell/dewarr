"""Recording catalog. Marketplace products are editions, never independent evidence votes."""

import re

from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.adapters.catalog_types import (
    BookData,
    EditionData,
    SearchPage,
    SeriesData,
    cover_url,
    year,
)
from app.adapters.contracts import AdapterError, FailureKind
from app.domain.catalog_language import catalog_language

MARKETPLACES = {
    "us": "com",
    "uk": "co.uk",
    "au": "com.au",
    "ca": "ca",
    "de": "de",
    "es": "es",
    "fr": "fr",
    "in": "in",
    "it": "it",
    "jp": "co.jp",
}
GROUPS = "contributors,product_attrs,product_desc,series,product_details,media"


def asin(value):
    value = str(value or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{10}", value):
        raise AdapterError(FailureKind.PARSER, "Invalid audiobook identifier")
    return value


def text(value):
    return BeautifulSoup(str(value or "")[:100000], "html.parser").get_text(" ", strip=True) or None


def names(value):
    return list(
        dict.fromkeys(
            v["name"].strip()
            for v in (value or [])
            if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip()
        )
    )


def product(value, *, nexus=False):
    if not isinstance(value, dict) or not value.get("title") or not value.get("asin"):
        raise AdapterError(FailureKind.NOT_FOUND, "This recording is not available in the catalog")
    try:
        external_id = asin(value["asin"])
        release = value.get("releaseDate" if nexus else "release_date")
        runtime = value.get("runtimeLengthMin" if nexus else "runtime_length_min")
        runtime = int(runtime) if runtime and str(runtime).isdigit() else None
        language = catalog_language(value.get("language"))
        images = value.get("product_images") or {}
        cover = cover_url(
            value.get("image")
            if nexus
            else next((images[k] for k in ("500", "1024", "1215", "300") if images.get(k)), None)
        )
        description = text(
            value.get("summary" if nexus else "publisher_summary")
            or value.get("merchandising_summary")
        )
        identifiers = {"asin": external_id}
        if value.get("isbn"):
            identifiers["isbn"] = str(value["isbn"])
        abridgment = str(value.get("formatType" if nexus else "format_type", "")).lower()
        edition = EditionData(
            external_id=external_id,
            title=text(value["title"]),
            medium="audio",
            language=language,
            narrators=names(value.get("narrators")),
            publication_year=year(release),
            release_date=str(release)[:10] if release else None,
            publisher=text(value.get("publisherName" if nexus else "publisher_name")),
            description=description,
            identifiers=identifiers,
            cover_url=cover,
            runtime_minutes=runtime,
            field_sources={
                "narrators": "audnexus" if nexus else "audible",
                "runtime_minutes": "audnexus" if nexus else "audible",
            },
            abridged={"abridged": True, "unabridged": False}.get(abridgment),
        )
        series = value.get("series") or []
        if nexus:
            series = [value[k] for k in ("seriesPrimary", "seriesSecondary") if value.get(k)]
        from app.config import get_settings

        return BookData(
            source_url=f"https://www.audible.{MARKETPLACES[get_settings().audible_region]}/pd/{external_id}",
            provider="audible",
            external_id=external_id,
            title=edition.title,
            authors=names(value.get("authors")),
            description=description,
            language=language,
            # A recording's release date is not the work's first publication year.
            cover_url=cover,
            editions=[edition],
            series=[
                SeriesData(
                    external_id=str(s.get("asin") or ""),
                    name=s["title"],
                    position=str(s.get("sequence") or "") or None,
                )
                for s in series
                if isinstance(s, dict) and s.get("title") and s.get("asin")
            ],
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise AdapterError(
            FailureKind.PARSER, "The audiobook catalog returned invalid details"
        ) from error


class Audible:
    def __init__(self, request):
        self.request = request

    async def search(self, query, page=1, language=None):
        if re.fullmatch(r"[A-Za-z0-9]{10}", query.strip()) and any(c.isdigit() for c in query):
            try:
                book = await self.fetch(query.strip())
                return SearchPage(provider="audible", items=[book], page=page, has_more=False)
            except AdapterError as error:
                if error.kind != FailureKind.NOT_FOUND:
                    raise
        value = await self.request(
            "GET",
            "1.0/catalog/products",
            params={
                "keywords": query,
                "num_results": 20,
                "page": page - 1,
                "response_groups": GROUPS,
            },
        )
        rows = value.get("products")
        if not isinstance(rows, list):
            raise AdapterError(FailureKind.PARSER, "The audiobook search response is incomplete")
        books = [product(row) for row in rows]
        if language:
            books = [book for book in books if book.language == catalog_language(language)]
        return SearchPage(provider="audible", items=books, page=page, has_more=len(rows) == 20)

    async def fetch(self, external_id, offset=0):
        value = await self.request(
            "GET", f"1.0/catalog/products/{asin(external_id)}", params={"response_groups": GROUPS}
        )
        book = product(value.get("product"))
        if book.external_id != asin(external_id):
            raise AdapterError(FailureKind.PARSER, "The catalog returned a different recording")
        return book


async def recording(external_id):
    """Audnexus only fills missing fields on the same ASIN; it cannot corroborate identity."""
    from app.config import get_settings
    from app.domain.catalog_network import CatalogGateway

    async with CatalogGateway("audible", "public") as gateway:
        book = await Audible(gateway.request).fetch(external_id)
        if gateway.stale:
            raise AdapterError(
                FailureKind.UNAVAILABLE, "Recording details are temporarily unavailable"
            )
    edition = book.editions[0]
    if not edition.narrators or not edition.runtime_minutes:
        try:
            async with CatalogGateway("audnexus", "public") as gateway:
                raw = await gateway.request(
                    "GET",
                    f"books/{asin(external_id)}",
                    params={
                        "region": get_settings().audible_region,
                    },
                )
                other = product(raw, nexus=True)
                # Never fill one recording with a redirected or contradictory response.
                if (
                    not gateway.stale
                    and other.external_id == book.external_id
                    and (
                        other.title.casefold() == book.title.casefold()
                        and set(other.authors) == set(book.authors)
                    )
                ):
                    incoming = other.editions[0]
                    for field in ("narrators", "runtime_minutes", "publisher", "description"):
                        if not getattr(edition, field):
                            setattr(edition, field, getattr(incoming, field))
                            if getattr(incoming, field):
                                edition.field_sources[field] = "audnexus"
        except AdapterError:
            pass  # Useful primary recording metadata remains available.
    return book
