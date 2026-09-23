"""Synthetic provider HTTP, including an edition beyond the first catalog page."""

import json

import httpx
import pytest

from app.domain.catalog_network import CatalogGateway
from app.importing import catalog_resolution


@pytest.fixture
def resolution_provider(monkeypatch):
    state = {"calls": [], "pages": 2, "medium": "ebook", "fault": None, "hook": None}

    async def respond(request):
        state["calls"].append((request.url.path, request.headers.get("authorization")))
        if state["hook"]:
            hook, state["hook"] = state["hook"], None
            await hook()
        if state["fault"] in {"quota", "outage"}:
            return httpx.Response(
                429 if state["fault"] == "quota" else 503,
                headers={"Retry-After": "3600"},
                json={"error": "private-provider-response"},
            )
        title = "Wrong title" if state["fault"] == "title" else "First Harbor"
        if request.url.path == "/v1/graphql":
            body = json.loads(request.content)
            query = body["query"]
            if "CatalogSearch" in query:
                count = 21 if state["fault"] == "truncated" else 1
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "search": {
                                "results": {
                                    "found": count,
                                    "hits": [
                                        {
                                            "document": {
                                                "id": 42,
                                                "title": "First Harbor",
                                                "author_names": ["Alex Morgan"],
                                            }
                                        }
                                    ],
                                }
                            }
                        }
                    },
                )
            assert "CatalogBook" in query
            books = [
                {
                    "id": 42,
                    "title": title,
                    "cached_contributors": [{"author": {"name": "Alex Morgan"}}],
                }
            ]
            offset = body["variables"]["offset"]
            more = offset < (state["pages"] - 1) * 50
            rows = [
                {
                    "id": offset + index + 1,
                    "book_id": 42,
                    "title": "First Harbor",
                    "reading_format": {
                        "format": "Audio" if state["medium"] == "audio" else "Ebook"
                    },
                    "language": {"code2": "en"},
                    "cached_contributors": [
                        {
                            "contribution": "Narrator",
                            "author": {
                                "name": "Wrong narrator"
                                if state["fault"] == "narrator"
                                else "Jordan Lee"
                            },
                        }
                    ],
                    "isbn_13": None if more else "9781234567897",
                }
                for index in range(51 if more else 2 if state["fault"] == "ambiguous" else 1)
            ]
            if more and offset + 50 == (state["pages"] - 1) * 50:
                rows[-1]["isbn_13"] = "9781234567897"
            if offset and state["fault"] == "overlap":
                rows.insert(0, {**rows[0], "id": 1, "isbn_13": None})
            return httpx.Response(200, json={"data": {"books": books, "editions": rows}})
        if request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "numFound": 1,
                    "docs": [
                        {
                            "key": "/works/OL1W",
                            "title": "First Harbor",
                            "author_name": ["Alex Morgan"],
                        }
                    ],
                },
            )
        if request.url.path == "/works/OL1W.json":
            return httpx.Response(
                200,
                json={
                    "key": "/works/OL1W",
                    "title": title,
                    "authors": [{"author": {"key": "/authors/OL1A"}}],
                },
            )
        if request.url.path == "/authors/OL1A.json":
            return httpx.Response(200, json={"name": "Alex Morgan"})
        if request.url.path == "/works/OL1W/editions.json":
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "key": "/books/OL1M",
                            "title": "First Harbor",
                            "physical_format": "ebook",
                            "languages": [{"key": "/languages/eng"}],
                            "isbn_13": ["9781234567897"],
                        }
                    ]
                },
            )
        raise AssertionError(request.url.path)

    class Gateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))
            self.stale = state["fault"] == "stale"

        async def reserve(self):
            pass  # Quotas/caches have dedicated tests; keep these fixtures fast.

    monkeypatch.setattr(catalog_resolution, "CatalogGateway", Gateway)
    return state
