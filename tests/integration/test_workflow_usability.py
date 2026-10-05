# ruff: noqa: F811
"""Focused API boundaries behind the discovery/request/import UI workflows."""

import pytest

from tests.integration.test_acquisition import body, catalog, request  # noqa: F401
from tests.integration.test_discovery import login_member
from tests.integration.test_import_inspections import mounted_download  # noqa: F401

pytestmark = pytest.mark.integration


async def test_request_search_filters_before_pagination_and_respects_ownership(
    client, admin, catalog
):  # noqa: F811
    saved = (await request(client, body(catalog, "audio")))["request"]
    for query in ("harbor", "wRiTeR"):
        response = await client.get(
            "/api/requests", params={"q": query, "limit": 1, "active_only": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["id"] == saved["id"]
    for query in ("not a book", "%", "_"):
        response = await client.get("/api/requests", params={"q": query})
        assert response.json()["total"] == 0
    library = await client.get("/api/requests", params={"q": "Harbor", "status": "library"})
    assert library.status_code == 200
    await login_member(client)
    assert (await client.get("/api/requests", params={"q": "Harbor"})).json()["total"] == 0


async def test_download_picker_is_admin_only_and_root_scoped(client, admin, mounted_download):  # noqa: F811
    roots = await client.get("/api/organization/download-files", params={"source_key": "fixture"})
    assert roots.status_code == 200, roots.text
    assert roots.json()["entries"][0]["path"] == "pack"
    files = await client.get(
        "/api/organization/download-files", params={"source_key": "fixture", "path": "pack"}
    )
    assert files.json()["entries"][0]["path"] == "pack/book.epub"
    assert files.json()["entries"][0]["size"] > 0
    assert (
        await client.get("/api/organization/download-files", params={"source_key": "unknown"})
    ).status_code == 404
    for path in ("../outside", "/etc"):
        response = await client.get(
            "/api/organization/download-files", params={"source_key": "fixture", "path": path}
        )
        assert response.status_code == 422
    await login_member(client)
    assert (
        await client.get("/api/organization/download-files", params={"source_key": "fixture"})
    ).status_code == 403


async def test_discovery_fetches_only_requested_collection_cards(client, admin):
    index = (await client.get("/api/discovery/collections", params={"limit": 2})).json()
    assert len(index["items"]) == 2
    chosen = index["items"][1]["id"]
    selected = await client.get("/api/discovery/collections", params={"ids": chosen})
    assert selected.status_code == 200, selected.text
    assert selected.json()["total"] == 1
    assert [item["id"] for item in selected.json()["items"]] == [chosen]
