# ruff: noqa: F811
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.db.models import AcquisitionSelection, DownloadAttempt, LibraryAsset, User, Work
from app.domain import download_attempts as downloads
from tests.integration.test_acquisition import body, catalog, request  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_discovery import login_member
from tests.integration.test_download_attempts import downloader, selected, start  # noqa: F401
from tests.integration.test_request_approvals import session_for

pytestmark = pytest.mark.integration


async def test_targets_offer_only_appropriate_next_actions(client, admin, catalog, database):
    saved = (await request(client, body(catalog)))["request"]
    assert saved["can_open_book"]
    assert [(t["state"], t["next_action"]) for t in saved["targets"]] == [
        ("satisfied", "book"),
        ("wanted", "search"),
    ]
    async with database() as db, db.begin():
        await db.execute(update(LibraryAsset).values(state="stale"))
    current = (await client.get("/api/requests/" + saved["id"])).json()
    assert current["targets"][0]["next_action"] == "book"
    assert current["targets"][0]["state"] == "awaiting-inventory"
    reason = current["reasons"][0]
    cancelled = (await client.delete(f"/api/requests/{saved['id']}/reasons/{reason['id']}")).json()
    assert all(
        t["state"] == "cancelled" and t["next_action"] == "none" for t in cancelled["targets"]
    )
    assert cancelled["can_open_book"]


async def test_active_filter_counts_before_paging_and_keeps_satisfied_requests(
    client, admin, catalog
):
    satisfied = (await request(client, body(catalog, "ebook")))["request"]
    mixed = (await request(client, body(catalog, "both")))["request"]
    assert not satisfied["can_withdraw"]
    assert mixed["can_withdraw"]
    newest = (await request(client, body(catalog, "audio")))["request"]
    await client.delete(f"/api/requests/{newest['id']}/reasons/{newest['reasons'][0]['id']}")
    first = (await client.get("/api/requests?active_only=true&limit=1")).json()
    second = (await client.get("/api/requests?active_only=true&limit=1&offset=1")).json()
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["id"] == mixed["id"]
    assert second["items"][0]["id"] == satisfied["id"]
    assert (await client.get("/api/requests")).json()["total"] == 3
    await login_member(client)
    assert (await client.get("/api/requests?active_only=true")).json()["total"] == 0


async def test_one_withdrawn_reason_does_not_hide_independent_request(client, admin, catalog):
    saved = (await request(client, body(catalog, "audio")))["request"]
    book_list = (await client.post("/api/lists", json={"name": "Still following"})).json()
    await client.post(
        f"/api/lists/{book_list['id']}/entries", json={"work_id": str(catalog["work"])}
    )
    listed = (
        await request(client, {**body(catalog, "audio"), "reason": {"list_id": book_list["id"]}})
    )["request"]
    assert listed["id"] == saved["id"]
    response = await client.delete(
        f"/api/requests/{saved['id']}/reasons/{saved['reasons'][0]['id']}"
    )
    assert response.status_code == 200
    active = (await client.get("/api/requests?active_only=true")).json()
    assert active["total"] == 1
    assert sum(r["active"] for r in active["items"][0]["reasons"]) == 1
    assert active["items"][0]["targets"][0]["next_action"] == "search"


async def test_revoked_request_authority_has_no_action_links(client, admin, catalog, database):
    saved = (await request(client, body(catalog, "audio")))["request"]
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "viewer"
    current = (await client.get("/api/requests/" + saved["id"])).json()
    assert not current["can_open_book"]
    assert current["work_title"] == "Unavailable book"
    assert all(t["next_action"] == "none" for t in current["targets"])
    assert all(t["state"] == "paused" for t in current["targets"])


async def test_prepared_release_and_private_shared_selection_do_not_invite_another_search(
    client, admin, database, selection_route
):
    response = await prepare(client, selection_route)
    assert response.status_code == 201, response.text
    intent_id = selection_route["intent_id"]
    target = (await client.get("/api/requests/" + intent_id)).json()["targets"][0]
    assert target["next_action"] == "selected-release"
    assert target["source_artifact_id"] == selection_route["artifact_id"]
    async with database() as db, db.begin():
        other = User(
            username="private-selection-owner",
            display_name="Private owner",
            password_hash="unused",
            role="member",
        )
        db.add(other)
        await db.flush()
        selection = await db.get(AcquisitionSelection, UUID(response.json()["id"]))
        selection.owner_id = other.id
    # A shared reservation can carry another owner's private selection. The
    # response must not reveal its artifact or offer a fresh source search.
    target = (await client.get("/api/requests/" + intent_id)).json()["targets"][0]
    assert target["next_action"] == "none" and target["source_artifact_id"] is None
    assert "Release selected" in target["message"]


async def test_committed_transfer_links_activity_without_any_read_side_effect(
    client, database, selected, downloader, selection_route
):
    saved = (await start(client, selected)).json()
    await downloads.run(UUID(saved["id"]))
    before = list(downloader.calls)
    for endpoint in [
        "/api/requests/" + selection_route["intent_id"],
        "/api/requests?active_only=true",
    ]:
        result = await client.get(endpoint)
        assert result.status_code == 200, result.text
        request = result.json() if "items" not in result.json() else result.json()["items"][0]
        assert request["targets"][0]["next_action"] == "downloads"
        assert request["targets"][0]["state"] == "wanted"
    assert downloader.calls == before
    async with database() as db, db.begin():
        attempt = await db.scalar(select(DownloadAttempt))
        attempt.observation = {"progress": 0.42}
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
    current = (await client.get("/api/requests/" + selection_route["intent_id"])).json()
    target = current["targets"][0]
    assert target["attempt_state"]
    assert target["progress"] == 0.42
    downloading = (await client.get("/api/requests?status=downloading")).json()
    assert downloading["total"] >= 1
    assert any(item["id"] == selection_route["intent_id"] for item in downloading["items"])


async def test_request_cards_filter_by_status_and_role(client, admin, catalog, database):
    async with database() as db, db.begin():
        work = await db.get(Work, catalog["work"])
        work.cover_url = "https://covers.example/harbor.jpg"
    saved = (await request(client, body(catalog, "both")))["request"]
    assert saved["authors"] == ["Writer"]
    assert saved["cover_url"] == "https://covers.example/harbor.jpg"
    assert saved["created_at"]
    assert saved["can_withdraw"] is True
    assert saved["targets"][0]["progress"] is None

    created = await client.post(
        "/api/auth/users",
        json={
            "username": "patron",
            "display_name": "Patron Reader",
            "password": "a long patron password",
            "role": "requester",
        },
    )
    assert created.status_code == 201, created.text
    patron = await session_for("patron", "a long patron password")
    try:
        waiting = (await request(patron, body(catalog, "audio")))["request"]
        assert waiting["approval_status"] == "pending"
        assert waiting["can_decide"] is False
        own = (await patron.get("/api/requests?active_only=true")).json()
        assert [item["id"] for item in own["items"]] == [waiting["id"]]
        assert (await patron.get("/api/requests?status=review")).status_code == 403
        pending = (await patron.get("/api/requests?status=pending")).json()
        assert pending["total"] == 1
    finally:
        await patron.aclose()

    visible = (await client.get("/api/requests?active_only=true&sort=newest")).json()
    assert {item["id"] for item in visible["items"]} == {saved["id"], waiting["id"]}
    queue = (await client.get("/api/requests?status=pending")).json()
    assert [item["id"] for item in queue["items"]] == [waiting["id"]]
    library = (await client.get("/api/requests?status=library")).json()
    assert saved["id"] in {item["id"] for item in library["items"]}

    alpha = (
        await client.post("/api/catalog/works", json={"title": "Alpha Tale", "authors": ["A"]})
    ).json()
    zebra = (
        await client.post("/api/catalog/works", json={"title": "Zebra Tale", "authors": ["Z"]})
    ).json()
    await request(client, {"work_id": zebra["id"], "specification": {"mode": "audio"}})
    await request(client, {"work_id": alpha["id"], "specification": {"mode": "audio"}})
    titles = [
        item["work_title"]
        for item in (await client.get("/api/requests?sort=title&active_only=true")).json()["items"]
        if item["work_title"] in {"Alpha Tale", "Zebra Tale"}
    ]
    assert titles == ["Alpha Tale", "Zebra Tale"]

    declined = await client.post(
        f"/api/requests/{waiting['id']}/decision",
        json={"status": "declined", "expected_status": "pending"},
        headers={"Idempotency-Key": "decline-patron-card"},
    )
    assert declined.status_code == 200, declined.text
    declined_list = (await client.get("/api/requests?status=declined")).json()
    assert waiting["id"] in {item["id"] for item in declined_list["items"]}
    await client.delete(f"/api/requests/{saved['id']}/reasons/{saved['reasons'][0]['id']}")
    withdrawn = (await client.get("/api/requests?status=withdrawn")).json()
    assert saved["id"] in {item["id"] for item in withdrawn["items"]}
    assert saved["id"] not in {
        item["id"] for item in (await client.get("/api/requests?active_only=true")).json()["items"]
    }


async def test_approver_status_follows_the_requesters_library(client, admin, catalog):
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "shelf",
            "display_name": "Shelf Reader",
            "password": "a long shelf password",
            "role": "requester",
        },
    )
    assert created.status_code == 201, created.text
    patron = await session_for("shelf", "a long shelf password")
    try:
        waiting = (await request(patron, body(catalog, "ebook")))["request"]
        decided = await client.post(
            f"/api/requests/{waiting['id']}/decision",
            json={"status": "approved", "download": False, "expected_status": "pending"},
            headers={"Idempotency-Key": "approve-shelf-ebook"},
        )
        assert decided.status_code == 200, decided.text
        seen = decided.json()["request"]["targets"][0]
        own = (await patron.get("/api/requests/" + waiting["id"])).json()["targets"][0]
        assert seen["state"] == own["state"] == "wanted"
        assert seen["message"] != "Already available in your library"
    finally:
        await patron.aclose()


async def test_request_counts_are_scoped_and_include_pending_work(client, admin, catalog):
    saved = (await request(client, body(catalog, "audio")))["request"]
    counts = (await client.get("/api/requests/counts")).json()
    assert counts == {"pending": 0, "downloading": 0, "review": 0, "active": 1}
    await client.delete(f"/api/requests/{saved['id']}/reasons/{saved['reasons'][0]['id']}")
    assert (await client.get("/api/requests/counts")).json()["active"] == 0
    await login_member(client)
    assert (await client.get("/api/requests/counts")).json()["active"] == 0


async def test_old_quick_add_failure_is_hidden_after_a_source_is_started(
    client, admin, database, selected, downloader
):
    from app.db.models import Operation
    from app.domain.quick_add import KIND

    response = await start(client, selected)
    assert response.status_code == 202
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        work_id = selection.frozen["origin_work_id"]
        db.add(
            Operation(
                owner_id=UUID(admin["id"]),
                kind=KIND,
                status="held",
                idempotency_key="old-quick-add-failure",
                message="No download route",
                payload={"intent_id": str(selection.intent_id), "command": {"work_id": work_id}},
            )
        )
    assert (await client.get(f"/api/requests/quick-add/latest/{work_id}")).json() is None
