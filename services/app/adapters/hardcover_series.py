"""Bounded series observations; provider positions are not work identity."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.adapters.catalog_providers import contributors, identifier
from app.adapters.catalog_types import BookData, cover_url, year
from app.adapters.contracts import AdapterError, FailureKind

PAGE_SIZE = 100
MAX_MEMBERS = 1000
QUERY = """query CatalogSeriesPage($id: Int!, $after: bigint!, $limit: Int!) {
 series(where: {id: {_eq: $id}}, limit: 1) {
  id canonical_id name description primary_books_count
  book_series_aggregate { aggregate { count } }
  book_series(where: {id: {_gt: $after}}, order_by: {id: asc}, limit: $limit) {
   id position details compilation featured
   book { id canonical_id title cached_contributors cached_image
    release_year release_date is_partial_book }
  }
 }
}"""


def invalid():
    return AdapterError(
        FailureKind.PARSER,
        "Series membership is incomplete or changed; the previous catalog is preserved",
    )


def position(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise invalid()
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > 10**8:
            raise invalid()
        return format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        raise invalid() from None


@dataclass
class SeriesPage:
    info: dict
    items: list[dict]
    cursor: int


async def page(query, external_id, cursor=0):
    identifier("hardcover", external_id)
    data = await query(QUERY, {"id": int(external_id), "after": cursor, "limit": PAGE_SIZE})
    try:
        rows = data["series"]
        if rows == []:
            raise AdapterError(
                FailureKind.NOT_FOUND, "Series not found or not accessible on Hardcover"
            )
        if not isinstance(rows, list) or len(rows) != 1:
            raise invalid()
        row = rows[0]
        if str(row["id"]) != external_id:
            raise invalid()
        if row.get("canonical_id"):
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "This series was merged on Hardcover; select its current series record",
            )
        count = row["book_series_aggregate"]["aggregate"]["count"]
        if type(count) is not int or not 0 <= count <= MAX_MEMBERS:
            raise invalid()
        name, description = row["name"], row.get("description")
        if not isinstance(name, str) or not name.strip() or len(name) > 600:
            raise invalid()
        if description is not None and (
            not isinstance(description, str) or len(description) > 50000
        ):
            raise invalid()
        info = {
            "external_id": external_id,
            "name": name,
            "description": description,
            "count": count,
        }
        members = row["book_series"]
        if not isinstance(members, list) or len(members) > PAGE_SIZE:
            raise invalid()
        items = []
        for member in members:
            key = member["id"]
            if type(key) is not int or not cursor < key <= 2**63 - 1:
                raise invalid()
            cursor = key
            raw = member["book"]
            book = BookData(
                provider="hardcover",
                external_id=identifier("hardcover", str(raw["id"])),
                title=raw["title"],
                authors=contributors(raw.get("cached_contributors"), "Author"),
                publication_year=year(raw.get("release_year")),
                cover_url=cover_url((raw.get("cached_image") or {}).get("url")),
            )
            compilation, partial = member["compilation"], raw["is_partial_book"]
            if type(compilation) is not bool or type(partial) is not bool:
                raise invalid()
            released = raw.get("release_date")
            if released is not None:
                released = date.fromisoformat(released).isoformat()
            details = member.get("details")
            if details is not None and (not isinstance(details, str) or len(details) > 1000):
                raise invalid()
            canonical = raw.get("canonical_id")
            if canonical is not None:
                canonical = identifier("hardcover", str(canonical))
            items.append(
                {
                    "entry_id": str(key),
                    "book": book.model_dump(mode="json"),
                    "position": position(member["position"]),
                    "details": details,
                    "compilation": compilation,
                    "partial": partial,
                    "canonical_id": canonical,
                    "release_date": released,
                }
            )
        return SeriesPage(info, items, cursor)
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise invalid() from None


def advance(stage, page):
    if not stage:
        stage = {"info": page.info, "items": [], "cursor": 0, "phase": "collect", "verified": 0}
    if page.info != stage["info"]:
        raise invalid()
    if stage["phase"] == "collect":
        stage = {**stage, "items": [*stage["items"], *page.items], "cursor": page.cursor}
        if len(stage["items"]) > page.info["count"]:
            raise invalid()
        if not page.items and len(stage["items"]) != page.info["count"]:
            raise invalid()
        # The aggregate count is read in the same query as every page. Once all
        # entries are collected, verify immediately instead of fetching an empty
        # terminator page. The second pass still detects same-size replacements.
        if len(stage["items"]) == page.info["count"]:
            stage.update(phase="verify", cursor=0)
        return stage, False
    offset = stage["verified"]
    if page.items != stage["items"][offset : offset + len(page.items)]:
        raise invalid()
    verified = offset + len(page.items)
    if not page.items and verified != len(stage["items"]):
        raise invalid()
    if verified == len(stage["items"]):
        return stage, True
    return {**stage, "verified": verified, "cursor": page.cursor}, False
