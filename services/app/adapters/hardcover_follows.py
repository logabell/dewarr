"""Complete, keyset-paged author/series catalogs for the standing list engine."""

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.adapters.catalog_providers import contributors, identifier
from app.adapters.catalog_types import BookData, cover_url, year
from app.adapters.hardcover_lists import MAX_MEMBERS, ListPage, invalid


class FollowFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    compilations: bool = False
    box_sets: bool = False
    anthologies: bool = False
    non_main_series: bool = False
    coauthored: bool = True
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")


AUTHOR = """contributions: {author_id: {_eq: $id}, _or: [
 {contribution: {_eq: "Author"}}, {contribution: {_is_null: true}}]}"""
SERIES = "book_series: {series_id: {_eq: $id}}"
QUERY = """query FollowCatalog($id: Int!, $after: Int!, $language: String!) {
 source: SOURCE(where: {id: {_eq: $id}}, limit: 1) { id name SOURCE_EXTRA }
 books_aggregate(where: {canonical_id: {_is_null: true}, PREDICATE}) {
  aggregate { count }
 }
 books(where: {id: {_gt: $after}, canonical_id: {_is_null: true}, PREDICATE},
  order_by: {id: asc}, limit: 100) {
  id title cached_contributors cached_image cached_tags release_date release_year is_partial_book
  book_series(limit: 101, order_by: {id: asc}) { series_id position compilation }
  matching_editions: editions_aggregate(where: {language: {code2: {_eq: $language}}}) {
   aggregate { count }
  }
 }
}"""


def filter_reason(record, filters, kind):
    text = record["classification"]
    if not filters.box_sets and re.search(r"\bbox[ -]?set\b|\bboxed\b", text):
        return "Box set"
    if not filters.anthologies and re.search(r"\bantholog(?:y|ies)\b", text):
        return "Anthology"
    if not filters.compilations and (
        record["compilation"] or re.search(r"\bomnibus\b|\bcompilation\b|\bcollected works\b", text)
    ):
        return "Compilation"
    if not filters.non_main_series and not record["main_series"]:
        return "Non-main-series title"
    if not filters.coauthored and len(record["authors"]) > 1:
        return "Co-authored book"
    if filters.language and not record["language_match"]:
        return "No matching language edition"
    return None


async def page(query, kind, external_id, cursor, filters):
    identifier("hardcover", external_id)
    if kind not in {"author", "series"}:
        raise invalid()
    statement = (
        QUERY.replace("SOURCE_EXTRA", "canonical_id" if kind == "series" else "")
        .replace("SOURCE", "authors" if kind == "author" else "series")
        .replace("PREDICATE", AUTHOR if kind == "author" else SERIES)
    )
    data = await query(
        statement, {"id": int(external_id), "after": cursor, "language": filters.language or ""}
    )
    try:
        sources = data["source"]
        if not isinstance(sources, list) or len(sources) != 1:
            raise invalid()
        source = sources[0]
        if (
            str(source["id"]) != external_id
            or not isinstance(source["name"], str)
            or not source["name"].strip()
            or len(source["name"]) > 600
            or source.get("canonical_id")
        ):
            raise invalid()
        count = data["books_aggregate"]["aggregate"]["count"]
        if type(count) is not int or not 0 <= count <= MAX_MEMBERS:
            raise invalid()
        rows = data["books"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise invalid()
        records = []
        for raw in rows:
            key = raw["id"]
            if type(key) is not int or key <= cursor:
                raise invalid()
            cursor = key
            book = BookData(
                provider="hardcover",
                external_id=identifier("hardcover", str(key)),
                title=raw["title"],
                authors=contributors(raw["cached_contributors"], "Author"),
            )
            memberships = raw["book_series"]
            if not isinstance(memberships, list) or len(memberships) > 100:
                raise invalid()
            main, compilation = False, False
            for membership in memberships:
                if type(membership["compilation"]) is not bool:
                    raise invalid()
                compilation |= membership["compilation"]
                if kind == "author" or str(membership["series_id"]) == external_id:
                    number = (
                        Decimal(str(membership["position"]))
                        if membership["position"] is not None
                        else None
                    )
                    main |= bool(
                        number is not None
                        and number.is_finite()
                        and number > 0
                        and number == number.to_integral_value()
                        and not membership["compilation"]
                    )
            if type(raw["is_partial_book"]) is not bool:
                raise invalid()
            day = (
                date.fromisoformat(raw["release_date"]).isoformat() if raw["release_date"] else None
            )
            matches = raw["matching_editions"]["aggregate"]["count"]
            if type(matches) is not int or matches < 0:
                raise invalid()
            record = {
                "source_kind": kind,
                "external_id": book.external_id,
                "title": book.title,
                "authors": book.authors,
                "isbn": None,
                "isbn13": None,
                "release_date": day,
                "coming_soon": not day and (year(raw.get("release_year")) or 0) > date.today().year,
                "compilation": compilation,
                "main_series": (main or (kind == "author" and not memberships))
                and not raw["is_partial_book"],
                "classification": (book.title + " " + str(raw.get("cached_tags") or "")).casefold(),
                "language_match": matches > 0,
                "cover_url": cover_url((raw.get("cached_image") or {}).get("url")),
            }
            record["filter_reason"] = filter_reason(record, filters, kind)
            records.append(record)
        return ListPage(
            {"external_id": external_id, "name": source["name"], "count": count}, records, cursor
        )
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, ValidationError):
        raise invalid() from None
