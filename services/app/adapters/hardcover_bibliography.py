"""Bounded author candidates for collection review, not a complete-novel count."""

from app.adapters.catalog_providers import identifier, parse_failure
from app.adapters.hardcover_discovery import book

QUERY = """query CollectionBibliography($id: Int!) {
 books(where: {id: {_eq: $id}}, limit: 1) {
  id contributions(where: {
   _or: [{contribution: {_eq: "Author"}}, {contribution: {_is_null: true}}]}, limit: 5) {
   author { id name }
  }
 }
}"""
BOOKS = """query CollectionAuthorBooks($ids: [Int!]!) {
 books(where: {canonical_id: {_is_null: true}, is_partial_book: {_eq: false},
 contributions: {author_id: {_in: $ids},
 _or: [{contribution: {_eq: "Author"}}, {contribution: {_is_null: true}}]}},
 order_by: {id: asc}, limit: 1001) {
 id canonical_id title users_count release_year release_date cached_image cached_contributors
 english: editions(where: {language: {code2: {_eq: "en"}}},
 order_by: [{users_count: desc}, {id: asc}], limit: 5) { title }
 book_series(limit: 20) { position details series { id name } }
 }
}"""


async def bibliography(query, external_id):
    identifier("hardcover", external_id)
    data = await query(QUERY, {"id": int(external_id)})
    try:
        rows = data["books"]
        if len(rows) != 1 or str(rows[0]["id"]) != external_id:
            raise parse_failure()
        authors = [c["author"] for c in rows[0]["contributions"]]
        if not authors:
            return {"books": [], "authors": [], "truncated": False}
        raw = (
            await query(
                BOOKS, {"ids": [int(identifier("hardcover", str(a["id"]))) for a in authors]}
            )
        )["books"]
        if not isinstance(raw, list) or len(raw) > 1001:
            raise parse_failure()
        parsed = []
        for value in raw[:1000]:
            if not value["english"]:
                continue
            item = book(value).model_dump(mode="json")
            item["users_count"] = value.get("users_count", 0) or 0
            item["aliases"] = [e["title"] for e in value["english"]]
            item["series"] = [
                {
                    "external_id": str(s["series"]["id"]),
                    "name": s["series"]["name"],
                    "position": str(s["position"]) if s["position"] is not None else None,
                }
                for s in value["book_series"]
            ]
            parsed.append(item)
        return {"books": parsed, "authors": authors, "truncated": len(raw) > 1000}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise parse_failure() from None
