"""Public list previews. Membership sync remains independently authoritative."""

import json
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from app.adapters.catalog_providers import identifier, parse_failure
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.hardcover_discovery import DiscoveryBook, book
from app.adapters.hardcover_lists import positive

HEADER = "id name description books_count public followers_count updated_at"
BOOK = "id canonical_id title release_year cached_image cached_contributors"
COMMUNITY_SEARCH = """query CommunitySearch($query: String!, $page: Int!) {
 search(query: $query, query_type: "List", per_page: 20, page: $page) { error results }
}"""
COMMUNITY_LISTS = """query CommunityLists($offset: Int!) {
 lists(where: {public: {_eq: true}},
 order_by: [{followers_count: desc_nulls_last}, {id: asc}], limit: 21, offset: $offset) {
 $HEADER
 list_books(order_by: {id: asc}, limit: 4) { id book_id book { $BOOK } }
 }
}""".replace("$HEADER", HEADER).replace("$BOOK", BOOK)
COMMUNITY_MATCHES = """query CommunityMatches($ids: [Int!]!) {
 lists(where: {id: {_in: $ids}, public: {_eq: true}}, limit: 20) {
 $HEADER
 list_books(order_by: {id: asc}, limit: 4) { id book_id book { $BOOK } }
 }
}""".replace("$HEADER", HEADER).replace("$BOOK", BOOK)
COMMUNITY_LIST = """query CommunityList($id: Int!, $after: Int!) {
 lists(where: {id: {_eq: $id}, public: {_eq: true}}, limit: 1) {
 $HEADER
 list_books(where: {id: {_gt: $after}}, order_by: {id: asc}, limit: 21) {
 id book_id book { $BOOK }
 }
 }
}""".replace("$HEADER", HEADER).replace("$BOOK", BOOK)
COMMUNITY_BOOKS = """query CommunityBooks($ids: [Int!]!) {
 books(where: {id: {_in: $ids}}, limit: 80) { $BOOK }
}""".replace("$BOOK", BOOK)


class PublicList(BaseModel):
    external_id: str
    name: str = Field(min_length=1, max_length=600)
    description: str | None = Field(default=None, max_length=30000)
    count: int = Field(ge=0, strict=True)
    followers: int | None = Field(default=None, ge=0, strict=True)
    updated_at: datetime | None = None
    books: list[DiscoveryBook] = Field(default_factory=list)
    next_cursor: int | None = None
    warning: str | None = None


class PublicLists(BaseModel):
    items: list[PublicList]
    has_more: bool
    warning: str | None = None


def header(row):
    if row["public"] is not True or not row["name"].strip():
        raise parse_failure()
    result = PublicList(
        external_id=str(positive(row["id"])),
        name=row["name"],
        description=row.get("description"),
        count=row["books_count"],
        followers=row.get("followers_count"),
        updated_at=row.get("updated_at"),
    )
    if result.updated_at and not result.updated_at.tzinfo:
        raise parse_failure()
    return result


def preview(row, *, limit, cursor=0):
    result = header(row)
    entries = row["list_books"]
    if not isinstance(entries, list) or len(entries) > limit:
        raise parse_failure()
    selected = []
    for entry in entries:
        key = positive(entry["id"])
        if key <= cursor:
            raise parse_failure()
        cursor = key
        selected.append(positive(entry["book_id"]))
    if limit == 21:
        result.next_cursor = entries[19]["id"] if len(entries) > 20 else None
        selected = selected[:20]
    return result, list(dict.fromkeys(selected))


async def hydrate(query, previews, rows):
    selected = list(dict.fromkeys(key for _, keys in previews for key in keys))
    if not selected:
        return
    embedded = {
        entry["book_id"]: entry["book"]
        for row in rows
        for entry in row["list_books"]
        if entry["book_id"] in selected and "book" in entry
    }
    missing = [key for key in selected if key not in embedded]
    if any(value is not None and positive(value["id"]) != key for key, value in embedded.items()):
        raise parse_failure()
    fetched = (await query(COMMUNITY_BOOKS, {"ids": missing}))["books"] if missing else []
    if not isinstance(fetched, list) or len(fetched) > len(missing):
        raise parse_failure()
    records = {}
    for row in [*(value for value in embedded.values() if value is not None), *fetched]:
        key = positive(row["id"])
        if key not in selected or key in records:
            raise parse_failure()
        records[key] = book(row)
    for result, keys in previews:
        result.books = [records[key] for key in keys if key in records]
        if any(key not in records for key in keys):
            result.warning = (
                "Some titles are unavailable in this preview. "
                "Membership is verified separately during sync."
            )


async def browse(query, term, page):
    try:
        warning = None
        if term.strip():
            search = (await query(COMMUNITY_SEARCH, {"query": term.strip(), "page": page}))[
                "search"
            ]
            if search.get("error"):
                raise AdapterError(
                    FailureKind.UNAVAILABLE,
                    "Hardcover list search is unavailable. Try again later.",
                )
            results = search["results"]
            if isinstance(results, str):
                results = json.loads(results)
            hits, count = results["hits"], results["found"]
            if not isinstance(hits, list) or len(hits) > 20 or type(count) is not int or count < 0:
                raise parse_failure()
            selected = [int(identifier("hardcover", str(hit["document"]["id"]))) for hit in hits]
            if len(set(selected)) != len(selected):
                raise parse_failure()
            # Search index content is never trusted as current public-list data.
            rows = (await query(COMMUNITY_MATCHES, {"ids": selected}))["lists"] if selected else []
            if not isinstance(rows, list) or len(rows) > len(selected):
                raise parse_failure()
            by_id = {positive(row["id"]): row for row in rows}
            if len(by_id) != len(rows) or set(by_id) - set(selected):
                raise parse_failure()
            if len(by_id) != len(selected):
                warning = "Some search matches are no longer public or available."
            rows = [by_id[key] for key in selected if key in by_id]
            has_more = page * 20 < count
        else:
            rows = (await query(COMMUNITY_LISTS, {"offset": (page - 1) * 20}))["lists"]
            if not isinstance(rows, list) or len(rows) > 21:
                raise parse_failure()
            # Validate even the lookahead without spending a book-hydration request on it.
            if len({header(row).external_id for row in rows}) != len(rows):
                raise parse_failure()
            has_more = len(rows) > 20
            rows = rows[:20]
        previews = [preview(row, limit=4) for row in rows]
        await hydrate(query, previews, rows)
        return PublicLists(
            items=[value for value, _ in previews], has_more=has_more, warning=warning
        )
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as error:
        raise parse_failure() from error


async def detail(query, external_id, cursor):
    data = await query(
        COMMUNITY_LIST, {"id": int(identifier("hardcover", external_id)), "after": cursor}
    )
    try:
        rows = data["lists"]
        if rows == []:
            raise AdapterError(
                FailureKind.NOT_FOUND, "This public Hardcover list is no longer available."
            )
        if not isinstance(rows, list) or len(rows) != 1:
            raise parse_failure()
        value, keys = preview(rows[0], limit=21, cursor=cursor)
        if value.external_id != external_id:
            raise parse_failure()
        await hydrate(query, [(value, keys)], rows)
        return value
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as error:
        raise parse_failure() from error
