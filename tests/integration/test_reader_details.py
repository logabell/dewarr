import json

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.hardcover_details import HC_DETAILS, HC_REVIEWS
from app.db.models import Work
from app.domain.catalog_network import CatalogGateway

pytestmark = pytest.mark.integration


async def test_reader_details_require_connection_and_do_not_import(
    client, admin, database, monkeypatch
):
    from app.api import metadata

    url = "/api/metadata/books/hardcover/42/reader-details"
    assert (await client.get(url)).status_code == 409
    assert (
        await client.put("/api/metadata/account", json={"token": "reader-test-token"})
    ).is_success
    calls = []

    def respond(request):
        document = json.loads(request.content)["query"]
        calls.append(document)
        if document == HC_DETAILS:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "books": [
                            {
                                "id": 42,
                                "rating": 4,
                                "ratings_count": 20,
                                "contributions": [],
                            }
                        ]
                    }
                },
            )
        assert document == HC_REVIEWS
        return httpx.Response(200, json={"data": {"user_books": []}})

    class Gateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

        async def reserve(self):
            pass

    monkeypatch.setattr(metadata, "CatalogGateway", Gateway)
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["rating"] == 4
    assert response.json()["reviews"] == []
    assert len(calls) == 2
    assert "reader-test-token" not in response.text
    assert (await client.get(url)).status_code == 200
    assert len(calls) == 2  # Scope-aware provider cache serves the second read.
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Work)) == 0
    assert (
        await client.get("/api/metadata/books/hardcover/invalid/reader-details")
    ).status_code == 502
    assert len(calls) == 2


async def test_reader_details_require_login(client):
    assert (await client.get("/api/metadata/books/hardcover/42/reader-details")).status_code == 401


@pytest.mark.parametrize("review_status", [401, 503])
async def test_review_failure_updates_authentication_but_tolerates_outages(
    client, admin, database, monkeypatch, review_status
):
    from app.api import metadata

    assert (
        await client.put("/api/metadata/account", json={"token": "reader-test-token"})
    ).is_success
    calls = []

    def respond(request):
        document = json.loads(request.content)["query"]
        calls.append(document)
        if document == HC_DETAILS:
            return httpx.Response(200, json={"data": {"books": [{"id": 42, "pages": 123}]}})
        assert document == HC_REVIEWS
        return httpx.Response(review_status, json={})

    class Gateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

        async def reserve(self):
            pass

    monkeypatch.setattr(metadata, "CatalogGateway", Gateway)
    url = "/api/metadata/books/hardcover/42/reader-details"
    response = await client.get(url)
    account = (await client.get("/api/metadata/account")).json()
    if review_status == 401:
        assert response.status_code == 409, response.text
        assert account["status"] == "authentication"
        # The broken credential is suppressed until the user reconnects or tests it.
        count = len(calls)
        assert (await client.get(url)).status_code == 409
        assert len(calls) == count
    else:
        assert response.status_code == 200, response.text
        assert response.json()["pages"] == 123
        assert response.json()["reviews_warning"]
        assert account["status"] != "authentication"
