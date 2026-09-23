from datetime import UTC, datetime

import pytest

from app.adapters.catalog_providers import HC_BOOK, Hardcover, OpenLibrary
from app.adapters.contracts import AdapterError, FailureKind
from app.domain.catalog_network import retry_delay


@pytest.mark.asyncio
async def test_hardcover_separates_authors_narrators_and_catalog_formats():
    async def request(method, path, *, json):
        assert method == "POST" and path == "v1/graphql"
        if json["query"] == HC_BOOK:
            assert json["variables"] == {"id": 42, "offset": 0}
            return {
                "data": {
                    "books": [
                        {
                            "id": 42,
                            "title": "A Book",
                            "release_year": 1999,
                            "cached_contributors": [
                                {"author": {"name": "Author One"}},
                                {"contribution": "Narrator", "author": {"name": "Reader One"}},
                            ],
                            "book_series": [
                                {
                                    "position": 1.5,
                                    "compilation": False,
                                    "series": {"id": 7, "name": "Series"},
                                }
                            ],
                        }
                    ],
                    "editions": [
                        {
                            "id": i,
                            "book_id": 42,
                            "title": "A Book",
                            "reading_format": {"format": format_name},
                            "cached_contributors": [
                                {"contribution": "Narrator", "author": {"name": "Reader One"}}
                            ],
                            "language": {"code2": "en"},
                            "release_year": 2001,
                        }
                        for i, format_name in enumerate(
                            ["Audio", "Ebook", "Physical", "Unspecified"], 1
                        )
                    ],
                }
            }
        return {
            "data": {
                "search": {
                    "results": (
                        '{"found": 1, "hits": [{"document": {"id": 42, "title": "A Book", '
                        '"author_names": ["Author One", "Reader One"], '
                        '"contribution_types": ["Author", "Narrator"], '
                        '"contributions": ['
                        '{"author": {"name": "Author One"}, "contribution": "Author"}, '
                        '{"author": {"name": "Reader One"}, "contribution": "Narrator"}]}}]}'
                    )
                }
            }
        }

    adapter = Hardcover(request)
    result = await adapter.search("A Book", 1)
    assert result.items[0].authors == ["Author One"]
    book = await adapter.fetch("42")
    assert book.authors == ["Author One"]
    assert book.series[0].position == "1.5"
    assert [edition.medium for edition in book.editions] == ["audio", "ebook", "print", "unknown"]
    assert book.editions[0].narrators == ["Reader One"]
    assert all(not edition.narrators for edition in book.editions[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize(
    "extra, expected",
    [
        ({"contribution_types": ["Author"]}, ["Writer One", "Writer Two"]),
        (
            {
                "contribution_types": ["Narrator", "Author"],
                "contributions": [
                    {"author": {"name": "Writer One"}, "contribution": "Author"},
                    {"author": {"name": "Writer Two"}, "contribution": None},
                    {"author": {"name": "Reader"}, "contribution": "Narrator"},
                    {"author": {"name": "Writer One"}, "contribution": "Author"},
                ],
            },
            ["Writer One", "Writer Two"],
        ),
        ({"contributions": []}, []),
        ({"contributions": None}, ["Writer One", "Writer Two"]),
        ({"author_names": "Writer One"}, ["Writer One"]),
    ],
)
async def test_hardcover_search_accepts_independent_contributor_facets(encoded, extra, expected):
    import json

    results = {
        "found": 21,
        "hits": [
            {
                "document": {
                    "id": "42",
                    "title": "Book",
                    "author_names": ["Writer One", "Writer Two"],
                    **extra,
                }
            }
        ],
    }

    async def request(*args, **kwargs):
        return {"data": {"search": {"results": json.dumps(results) if encoded else results}}}

    adapter = Hardcover(request)
    page = await adapter.search("Book", 1)
    assert page.items[0].authors == expected
    assert page.has_more
    await adapter.test()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"data": None},
        {"data": {"search": {"results": {"hits": [], "found": "bad"}}}},
        {"data": {"search": {"results": "not-json"}}},
        {
            "data": {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": 4,
                                    "title": "x",
                                    "image": {"url": "https://127.0.0.1/private"},
                                }
                            }
                        ],
                        "found": 1,
                    }
                }
            },
            "errors": [{"message": "secret upstream failure"}],
        },
    ],
)
async def test_malformed_and_partial_graphql_never_becomes_empty_success(value):
    async def request(*args, **kwargs):
        return value

    with pytest.raises(AdapterError) as caught:
        await Hardcover(request).search("query", 1)
    assert caught.value.kind == FailureKind.PARSER
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_openlibrary_does_not_infer_ebook_from_print_scan():
    async def request(method, path, **kwargs):
        if path == "works/OL1W.json":
            return {
                "key": "/works/OL1W",
                "title": "Book",
                "authors": [{"author": {"key": "/authors/OL2A"}}],
                "description": {"value": "Text"},
            }
        if path == "authors/OL2A.json":
            return {"name": "Writer"}
        if path.endswith("editions.json"):
            return {
                "entries": [
                    {
                        "key": "/books/OL3M",
                        "physical_format": "Paperback",
                        "ocaid": "scan",
                        "publish_date": "June 2000",
                    }
                ]
            }
        return {
            "docs": [{"key": "/works/OL1W", "title": "Book", "author_name": ["Writer"]}],
            "numFound": 1,
        }

    adapter = OpenLibrary(request)
    assert (await adapter.search("Book", 1)).items[0].external_id == "OL1W"
    book = await adapter.fetch("OL1W")
    assert book.editions[0].medium == "unknown"
    assert book.authors == ["Writer"]
    assert book.description == "Text"


def test_quota_headers_respect_all_exhausted_buckets():
    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert retry_delay({"ratelimit": '"Free";r=0;t=40, "daily";r=0;t=3600'}, now) == 3600
    assert retry_delay({"retry-after": "120"}, now) == 120
    assert (
        retry_delay(
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(now.timestamp() + 80)}, now
        )
        == 80
    )
    assert retry_delay({"ratelimit": '"Free";r=5;t=40'}, now) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("count, has_more", [(50, False), (51, True)])
async def test_editions_use_lookahead_before_claiming_another_page(count, has_more):
    async def request(method, path, *, json):
        assert json["query"] == HC_BOOK and json["variables"]["offset"] == 50
        return {
            "data": {
                "books": [{"id": 42, "title": "Book", "cached_contributors": None}],
                "editions": [
                    {"id": index + 51, "book_id": 42, "cached_contributors": None}
                    for index in range(count)
                ],
            }
        }

    book = await Hardcover(request).fetch("42", 50)
    assert len(book.editions) == 50
    assert book.editions_more is has_more
    assert book.editions_offset == 50


@pytest.mark.asyncio
async def test_candidates_are_fetched_together_with_their_editions():
    from app.adapters.catalog_providers import HC_BOOKS

    requests = []

    async def request(method, path, *, json):
        requests.append(json)
        assert json["query"] == HC_BOOKS and json["variables"] == {"ids": [42, 43]}
        return {
            "data": {
                "books": [
                    {
                        "id": key,
                        "title": f"Book {key}",
                        "cached_contributors": None,
                        "editions": [{"id": key * 10, "book_id": key, "asin": f"B0000000{key}"}],
                    }
                    for key in (42, 43)
                ]
            }
        }

    books = await Hardcover(request).fetch_many(["42", "43"])
    assert len(requests) == 1
    assert books["43"].editions[0].identifiers == {"asin": "B000000043"}
    assert not books["42"].editions_more


@pytest.mark.asyncio
async def test_batched_fetch_rejects_a_book_that_was_not_requested():
    async def request(method, path, *, json):
        return {"data": {"books": [{"id": 9, "title": "Other", "editions": []}]}}

    with pytest.raises(AdapterError) as caught:
        await Hardcover(request).fetch_many(["42"])
    assert caught.value.kind == FailureKind.PARSER


@pytest.mark.asyncio
async def test_search_language_filters_provider_results():
    async def open_request(method, path, *, params):
        assert params["q"] == "(Dune) AND language:eng"
        return {"docs": [], "numFound": 0}

    assert not (await OpenLibrary(open_request).search("Dune", 1, "en")).items

    async def hardcover_request(method, path, *, json):
        if "SearchLanguage" in json["query"]:
            assert json["variables"] == {"ids": [1, 2], "language": "en"}
            return {"data": {"books": [{"id": 2}]}}
        return {
            "data": {
                "search": {
                    "results": {
                        "found": 2,
                        "hits": [
                            {"document": {"id": i, "title": f"Book {i}", "author_names": []}}
                            for i in [1, 2]
                        ],
                    }
                }
            }
        }

    result = await Hardcover(hardcover_request).search("Book", 1, "en")
    assert [book.external_id for book in result.items] == ["2"]
