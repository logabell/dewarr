"""Independent adapters using documented public catalog operation shapes."""

import json
import re

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

HC_SEARCH = """query CatalogSearch($query: String!, $page: Int!) {
 search(query: $query, query_type: "Book", per_page: 20, page: $page) { error results }
}"""
_HC_BOOK_FIELDS = """id canonical_id rating title description release_year cached_image
  cached_contributors
  book_series(limit: 100, order_by: {id: asc}) { position compilation series { id name } }"""
_HC_EDITION_FIELDS = """id title book_id canonical_id release_year isbn_10 isbn_13 asin cached_image
  cached_contributors edition_information reading_format { format } language { code2 code3 }
  publisher { name }"""
# One request per book: Hardcover's rate limit counts requests, not fields.
HC_BOOK = f"""query CatalogBook($id: Int!, $offset: Int!) {{
 books(where: {{id: {{_eq: $id}}}}, limit: 1) {{
  {_HC_BOOK_FIELDS}
 }}
 editions(where: {{book_id: {{_eq: $id}}}}, order_by: {{id: asc}}, limit: 51, offset: $offset) {{
  {_HC_EDITION_FIELDS}
 }}
}}"""
# Several candidates in one request, each with its first page of editions.
HC_BOOKS = f"""query CatalogBooks($ids: [Int!]!) {{
 books(where: {{id: {{_in: $ids}}}}, limit: 10) {{
  {_HC_BOOK_FIELDS}
  editions(order_by: {{id: asc}}, limit: 51) {{
   {_HC_EDITION_FIELDS}
  }}
 }}
}}"""


def parse_failure():
    return AdapterError(FailureKind.PARSER, "The catalog provider returned an unexpected response.")


def identifier(provider: str, value: str) -> str:
    pattern = r"[1-9]\d{0,9}" if provider == "hardcover" else r"OL\d{1,15}W"
    if not re.fullmatch(pattern, value):
        raise AdapterError(FailureKind.PARSER, "Invalid catalog identifier.")
    if provider == "hardcover" and int(value) > 2147483647:
        raise AdapterError(FailureKind.PARSER, "Invalid catalog identifier.")
    return value


def contributors(records, role: str) -> list[str]:
    if records is None:
        return []
    if not isinstance(records, list):
        raise parse_failure()
    names = []
    for record in records:
        if not isinstance(record, dict):
            raise parse_failure()
        kind = record.get("contribution")
        if kind == role or (role == "Author" and kind is None):
            name = (record.get("author") or {}).get("name")
            if isinstance(name, str) and name.strip():
                names.append(name)
    return list(dict.fromkeys(names))


class Hardcover:
    def __init__(self, request):
        self.request = request

    async def author_details(self, external_id, page):
        from app.adapters.hardcover_authors import detail

        return await detail(self.query, external_id, page)

    async def title_search(self, title):
        from app.adapters.hardcover_identifiers import title_search

        return await title_search(self.query, title)

    async def identifier_search(self, identifiers):
        from app.adapters.hardcover_identifiers import search

        return await search(self.query, identifiers)

    async def reader_details(self, external_id):
        from app.adapters.hardcover_details import details

        return await details(self.query, external_id)

    async def discovery(self, shelf, page, today):
        from app.adapters.hardcover_discovery import browse

        return await browse(self.query, shelf, page, today)

    async def upcoming(self, start, end, page):
        from app.adapters.hardcover_discovery import upcoming

        return await upcoming(self.query, start, end, page)

    async def upcoming_month(self, start, end):
        from app.adapters.hardcover_discovery import upcoming_month

        return await upcoming_month(self.query, start, end)

    async def related(self, external_id):
        from app.adapters.hardcover_discovery import related

        return await related(self.query, external_id)

    async def community_lists(self, term, page):
        from app.adapters.hardcover_community import browse

        return await browse(self.query, term, page)

    async def community_list(self, external_id, cursor=0):
        from app.adapters.hardcover_community import detail

        return await detail(self.query, external_id, cursor)

    async def query(self, query, variables):
        value = await self.request(
            "POST", "v1/graphql", json={"query": query, "variables": variables}
        )
        if value.get("errors") or value.get("error"):
            errors = value.get("errors") or []
            codes = set()
            for error in errors if isinstance(errors, list) else []:
                extensions = error.get("extensions") if isinstance(error, dict) else None
                if isinstance(extensions, dict) and isinstance(extensions.get("code"), str):
                    codes.add(extensions["code"])
            kind = (
                FailureKind.PERMISSION
                if codes & {"access-denied", "permission-error", "insufficient_scope"}
                else FailureKind.PARSER
            )
            raise AdapterError(
                kind,
                "Hardcover could not complete this query. "
                "Check token scopes and provider compatibility.",
            )
        if not isinstance(value.get("data"), dict):
            raise parse_failure()
        return value["data"]

    async def list_page(self, external_id, cursor=0):
        from app.adapters.hardcover_lists import page

        return await page(self.query, external_id, cursor)

    async def list_choices(self, mode="owned", cursor=0):
        from app.adapters.hardcover_lists import choices

        return await choices(self.query, mode, cursor)

    async def test(self):
        # Catalog-only token does not require access to private profile fields.
        await self.search("Dune", 1)

    async def search(self, query: str, page: int, language: str | None = None) -> SearchPage:
        data = await self.query(HC_SEARCH, {"query": query, "page": page})
        try:
            search = data["search"]
            if search.get("error"):
                raise AdapterError(
                    FailureKind.UNAVAILABLE, "Hardcover search is unavailable. Try again later."
                )
            results = search["results"]
            if isinstance(results, str):
                results = json.loads(results)
            hits, count = results["hits"], results["found"]
            if not isinstance(hits, list) or type(count) is not int or count < 0:
                raise parse_failure()
            books = []
            for hit in hits:
                record = hit["document"]
                # Search facets are independently deduplicated, not parallel arrays.
                # Only contribution records associate a person with their role.
                if record.get("contributions") is not None:
                    names = contributors(record["contributions"], "Author")
                else:
                    names = record.get("author_names") or []
                    if isinstance(names, str):
                        names = [names]
                books.append(
                    BookData(
                        provider="hardcover",
                        external_id=identifier("hardcover", str(record["id"])),
                        title=record["title"],
                        rating=record.get("rating"),
                        authors=names,
                        publication_year=year(record.get("release_year")),
                        cover_url=cover_url((record.get("image") or {}).get("url")),
                    )
                )
            if language and books:
                matching = await self.query(
                    """query SearchLanguage($ids: [Int!]!, $language: String!) {
                  books(where: {id: {_in: $ids},
                    editions: {language: {code2: {_eq: $language}}}}) { id }
                }""",
                    {"ids": [int(book.external_id) for book in books], "language": language},
                )
                allowed = {str(book["id"]) for book in matching["books"]}
                books = [book for book in books if book.external_id in allowed]
            return SearchPage(
                provider="hardcover", items=books, page=page, has_more=page * 20 < count
            )
        except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
            raise parse_failure() from error

    async def fetch(self, external_id: str, edition_offset: int = 0) -> BookData:
        key = int(identifier("hardcover", external_id))
        data = await self.query(HC_BOOK, {"id": key, "offset": edition_offset})
        try:
            rows = data["books"]
            if rows == []:
                raise AdapterError(
                    FailureKind.NOT_FOUND, "This Hardcover book is no longer available."
                )
            record = rows[0]
            if str(record["id"]) != external_id:
                raise parse_failure()
            return self.book(record, data["editions"], edition_offset)
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            ValidationError,
        ) as error:
            raise parse_failure() from error

    async def fetch_many(self, external_ids: list[str]) -> dict[str, BookData]:
        """Full records keyed by id. A missing id is a book Hardcover no longer has."""
        keys = [int(identifier("hardcover", value)) for value in external_ids]
        if not keys or len(keys) > 10:
            raise parse_failure()
        data = await self.query(HC_BOOKS, {"ids": keys})
        try:
            books = {}
            for record in data["books"]:
                external_id = str(record["id"])
                if external_id not in external_ids or external_id in books:
                    raise parse_failure()
                books[external_id] = self.book(record, record["editions"], 0)
            return books
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            ValidationError,
        ) as error:
            raise parse_failure() from error

    def book(self, record, edition_rows, edition_offset):
        external_id = str(record["id"])
        book = BookData(
            provider="hardcover",
            external_id=external_id,
            title=record["title"],
            rating=record.get("rating"),
            authors=contributors(record.get("cached_contributors", []), "Author"),
            description=record.get("description"),
            publication_year=year(record.get("release_year")),
            cover_url=cover_url((record.get("cached_image") or {}).get("url")),
            canonical_id=str(record["canonical_id"]) if record.get("canonical_id") else None,
            series=[
                SeriesData(
                    external_id=str(row["series"]["id"]),
                    name=row["series"]["name"],
                    position=str(row["position"]) if row.get("position") is not None else None,
                    compilation=row.get("compilation") or False,
                )
                for row in record.get("book_series", [])
                if row.get("series")
            ],
        )
        book.editions = [self.edition(row, external_id) for row in edition_rows[:50]]
        book.editions_more = len(edition_rows) > 50
        book.editions_offset = edition_offset
        return book

    @staticmethod
    def edition(row, work_id):
        if str(row["book_id"]) != work_id:
            raise parse_failure()
        format_name = ((row.get("reading_format") or {}).get("format") or "").casefold()
        medium = {
            "ebook": "ebook",
            "e-book": "ebook",
            "audio": "audio",
            "audiobook": "audio",
            "physical": "print",
            "print": "print",
        }.get(format_name, "unknown")
        language = row.get("language") or {}
        return EditionData(
            external_id=str(row["id"]),
            title=row.get("title"),
            medium=medium,
            language=language.get("code2") or language.get("code3"),
            narrators=contributors(row.get("cached_contributors", []), "Narrator")
            if medium == "audio"
            else [],
            publication_year=year(row.get("release_year")),
            publisher=(row.get("publisher") or {}).get("name"),
            description=row.get("edition_information"),
            identifiers={
                name: row[name] for name in ("isbn_10", "isbn_13", "asin") if row.get(name)
            },
            cover_url=cover_url((row.get("cached_image") or {}).get("url")),
        )


class OpenLibrary:
    def __init__(self, request):
        self.request = request

    async def search(self, query: str, page: int, language: str | None = None) -> SearchPage:
        data = await self.request(
            "GET",
            "search.json",
            params={
                "q": f"({query}) AND language:{language_code(language)}" if language else query,
                "page": page,
                "limit": 20,
                "fields": "key,title,author_name,first_publish_year,cover_i",
            },
        )
        try:
            docs, count = data["docs"], data.get("numFound", data.get("num_found"))
            if not isinstance(docs, list) or type(count) is not int or count < 0:
                raise parse_failure()
            items = [
                BookData(
                    provider="openlibrary",
                    external_id=identifier("openlibrary", row["key"].removeprefix("/works/")),
                    title=row["title"],
                    authors=row.get("author_name", []),
                    publication_year=year(row.get("first_publish_year")),
                    cover_url=self.cover(row.get("cover_i")),
                )
                for row in docs
            ]
            return SearchPage(
                provider="openlibrary", items=items, page=page, has_more=page * 20 < count
            )
        except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
            raise parse_failure() from error

    @staticmethod
    def cover(value):
        return (
            f"https://covers.openlibrary.org/b/id/{value}-L.jpg"
            if type(value) is int and value > 0
            else None
        )

    async def fetch(self, external_id: str, edition_offset: int = 0) -> BookData:
        identifier("openlibrary", external_id)
        value = await self.request("GET", f"works/{external_id}.json")
        try:
            if value["key"] != f"/works/{external_id}":
                raise parse_failure()
            authors = []
            for entry in value.get("authors", [])[:30]:
                author_id = entry["author"]["key"]
                if not re.fullmatch(r"/authors/OL\d+A", author_id):
                    raise parse_failure()
                author = await self.request("GET", author_id.lstrip("/") + ".json")
                authors.append(author["name"])
            description = value.get("description")
            if isinstance(description, dict):
                description = description.get("value")
            book = BookData(
                provider="openlibrary",
                external_id=external_id,
                title=value["title"],
                authors=authors,
                description=description,
                publication_year=year(value.get("first_publish_date")),
                subjects=value.get("subjects", [])[:100],
                cover_url=self.cover(next(iter(value.get("covers", [])), None)),
            )
            editions = await self.request(
                "GET",
                f"works/{external_id}/editions.json",
                params={"limit": 51, "offset": edition_offset},
            )
            entries = editions["entries"]
            for row in entries[:50]:
                key = row["key"]
                if not re.fullmatch(r"/books/OL\d+M", key):
                    raise parse_failure()
                physical = (row.get("physical_format") or "").casefold()
                # A downloadable scan does not make a print edition an ebook edition.
                medium = (
                    "ebook" if physical in {"ebook", "electronic resource", "e-book"} else "unknown"
                )
                languages = row.get("languages", [])
                language = (
                    languages[0].get("key", "").removeprefix("/languages/")
                    if len(languages) == 1
                    else None
                )
                book.editions.append(
                    EditionData(
                        external_id=key.removeprefix("/books/"),
                        title=row.get("title"),
                        medium=medium,
                        language=language,
                        publication_year=year(row.get("publish_date")),
                        publisher=", ".join(row.get("publishers", [])) or None,
                        identifiers={
                            field: row[field][0]
                            for field in ("isbn_10", "isbn_13")
                            if row.get(field)
                        },
                        cover_url=self.cover(next(iter(row.get("covers", [])), None)),
                    )
                )
            book.editions_more = len(entries) > 50
            book.editions_offset = edition_offset
            return book
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            ValidationError,
        ) as error:
            raise parse_failure() from error


def language_code(value: str) -> str:
    # Open Library uses ISO 639-2 bibliographic codes for its language facet.
    pairs = (
        "en:eng es:spa fr:fre de:ger it:ita pt:por nl:dut da:dan sv:swe no:nor fi:fin "
        "is:ice pl:pol cs:cze sk:slo hu:hun ro:rum bg:bul el:gre uk:ukr ru:rus tr:tur "
        "ar:ara he:heb fa:per hi:hin bn:ben ta:tam te:tel ur:urd id:ind ms:may vi:vie "
        "th:tha ko:kor ja:jpn zh:chi sw:swa af:afr sq:alb am:amh hy:arm az:aze eu:baq "
        "be:bel bs:bos ca:cat et:est fil:fil gl:glg ka:geo gu:guj hr:hrv kk:kaz km:khm "
        "kn:kan ky:kir lo:lao lt:lit lv:lav mk:mac ml:mal mn:mon mr:mar my:bur ne:nep "
        "pa:pan si:sin sl:slv sr:srp so:som uz:uzb zu:zul "
    )
    code = value.casefold().split("-")[0]
    if not re.fullmatch(r"[a-z]{2,3}", code):
        raise AdapterError(
            FailureKind.PARSER, "Choose a supported search language in Metadata settings."
        )
    return dict(pair.split(":") for pair in pairs.split()).get(code, code)
