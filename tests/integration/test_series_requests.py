# ruff: noqa: F401, F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionReservation,
    CatalogSeries,
    Operation,
    User,
    Work,
)
from app.domain import series_requests
from tests.integration.test_acquisition import catalog, request  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_catalog_series import (
    detail as catalog_detail,
)
from tests.integration.test_catalog_series import (
    finish,
    record,
    service,
)  # noqa: F401
from tests.integration.test_catalog_series import (
    start as observe,
)
from tests.integration.test_correction_migration import migrate
from tests.integration.test_download_attempts import selected

pytestmark = pytest.mark.integration
BASE = "/api/catalog/series/hardcover/9/requests"


@pytest.fixture
async def observed(client, database, service, catalog):
    await finish(database, await observe(client))
    return await catalog_detail(client)


def body(observed, **updates):
    return {
        "work_ids": [r["work"]["id"] for r in observed["items"]],
        "specification": {"mode": "both"},
        "expected_generation": observed["generation"],
        **updates,
    }


async def preview(client, observed, **updates):
    response = await client.post(
        BASE + "/preview", json=body(observed, **updates), headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def accept(client, plan):
    response = await client.post(BASE + f"/{plan['id']}/submit")
    assert response.status_code == 202, response.text
    return response.json()


async def detail(client, plan):
    response = await client.get(BASE + f"/{plan['id']}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_finite_preview_is_read_only_then_preserves_owned_medium(client, database, observed):
    plan = await preview(client, observed)
    assert plan["counts"]["satisfied"] == 1 and plan["counts"]["wanted"] == 3
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
    await accept(client, plan)
    await series_requests.run(UUID(plan["id"]))
    result = await detail(client, plan)
    assert result["status"] == "completed" and len(result["receipt"]) == 2
    assert result["counts"]["satisfied"] == 1 and result["counts"]["pending"] == 3
    async with database() as db:
        reasons = list(await db.scalars(select(AcquisitionReason)))
        assert len(reasons) == 2 and all(
            r.kind == "series" and r.reference == plan["id"] for r in reasons
        )
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 3
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name='acquisition.download'"
                )
            )
            == 0
        )
    request_view = (await client.get("/api/requests/" + result["receipt"][0]["request_id"])).json()
    assert request_view["reasons"][0]["label"] == "Series: Series"


async def test_complete_series_requires_reviewed_published_main_set(
    client, database, service, observed
):
    response = await client.post(
        BASE + "/preview",
        json=body(observed, scope="complete_series"),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422
    plan = await preview(
        client,
        observed,
        scope="complete_series",
        confirm_main_membership=True,
        work_ids=[observed["items"][0]["work"]["id"]],
    )
    assert plan["main_membership"] == "user-confirmed" and len(plan["omitted"]) == 1
    service.items = [record(release_date=None), record(2, 43, compilation=True)]
    await finish(database, await observe(client, "changed-publication"))
    latest = await catalog_detail(client, section="all")
    response = await client.post(
        BASE + "/preview",
        json=body(latest, scope="complete_series", confirm_main_membership=True),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 409
    manual = await preview(client, latest)
    assert all(r["warnings"] for r in manual["records"])


async def test_preview_sorts_decimal_positions_and_preserves_duplicate_warnings(
    client, database, service, observed
):
    service.items = [
        record(position="10"),
        record(2, 43, position="2.5"),
        record(3, 44, position="2.5"),
        record(4, 45, position=None),
    ]
    await finish(database, await observe(client, "position-evidence"))
    latest = await catalog_detail(client, section="all")
    plan = await preview(client, latest)
    assert [r["position"] for r in plan["records"]] == ["2.5", "2.5", "10", None]
    assert all(
        "Multiple works at this position; verify membership" in r["warnings"]
        for r in plan["records"][:2]
    )
    assert not plan["records"][2]["warnings"]


async def test_accepted_scope_survives_catalog_refresh_without_expanding(
    client, database, service, observed
):
    plan = await preview(client, observed)
    await accept(client, plan)
    service.items = [record(3, 44)]
    await finish(database, await observe(client, "new-series-membership"))
    await series_requests.run(UUID(plan["id"]))
    saved = await detail(client, plan)
    assert saved["status"] == "completed" and len(saved["receipt"]) == 2
    assert {r["work_id"] for r in saved["receipt"]} == {r["work"]["id"] for r in observed["items"]}
    assert saved["catalog_generation"] == 1


@pytest.mark.parametrize("change", ["generation", "identity", "expiration", "preferences"])
async def test_unaccepted_stale_preview_cannot_submit(client, database, observed, change):
    plan = await preview(client, observed)
    async with database() as db, db.begin():
        if change == "generation":
            (await db.scalar(select(CatalogSeries))).generation += 1
        elif change == "identity":
            (
                await db.get(Work, UUID(observed["items"][0]["work"]["id"]))
            ).title = "Changed identity"
        elif change == "expiration":
            op = await db.get(Operation, UUID(plan["id"]))
            op.payload = {
                **op.payload,
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        else:
            op = await db.get(Operation, UUID(plan["id"]))
            profile = {**op.payload["release_policy"], "effective_revision": "0" * 64}
            op.payload = {**op.payload, "release_policy": profile}
    response = await client.post(BASE + f"/{plan['id']}/submit")
    assert response.status_code == 409, response.text
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0


async def test_idempotency_concurrent_workers_and_two_series_reasons(client, database, observed):
    key = str(uuid4())
    responses = await asyncio.gather(
        *(
            client.post(BASE + "/preview", json=body(observed), headers={"Idempotency-Key": key})
            for _ in range(3)
        )
    )
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    plan = responses[0].json()
    await accept(client, plan)
    await asyncio.gather(*(series_requests.run(UUID(plan["id"])) for _ in range(3)))
    second = await preview(client, observed)
    await accept(client, second)
    await series_requests.run(UUID(second["id"]))
    await client.post(BASE + f"/{plan['id']}/cancel")
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 2
        reasons = list(await db.scalars(select(AcquisitionReason)))
        assert len(reasons) == 4
        assert all(r.active == (r.reference == second["id"]) for r in reasons)
    replay = await client.post(BASE + f"/{plan['id']}/submit")
    assert replay.status_code == 409
    assert (await detail(client, second))["counts"]["pending"] == 3


async def test_cancel_preserves_independent_manual_request_and_never_reactivates(
    client, database, observed
):
    work = observed["items"][0]["work"]["id"]
    manual = await request(client, {"work_id": work, "specification": {"mode": "both"}})
    plan = await preview(client, observed, work_ids=[work])
    await accept(client, plan)
    await series_requests.run(UUID(plan["id"]))
    cancelled = await client.post(BASE + f"/{plan['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["counts"]["cancelled"] == 1
    await series_requests.run(UUID(plan["id"]))
    current = (await client.get("/api/requests/" + manual["request"]["id"])).json()
    assert {r["kind"]: r["active"] for r in current["reasons"]} == {"manual": True, "series": False}
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 1
        assert (await db.scalar(select(AcquisitionReservation))).state == "planned"


@pytest.mark.parametrize("change", ["viewer", "inactive", "identity"])
async def test_worker_fences_authority_and_identity_changes(
    client, database, observed, admin, change
):
    plan = await preview(client, observed)
    await accept(client, plan)
    async with database() as db, db.begin():
        if change == "identity":
            (await db.get(Work, UUID(observed["items"][0]["work"]["id"]))).title = "Different book"
        else:
            user = await db.get(User, UUID(admin["id"]))
            if change == "viewer":
                user.role = "viewer"
            else:
                user.active = False
    await series_requests.run(UUID(plan["id"]))
    async with database() as db:
        assert (await db.get(Operation, UUID(plan["id"]))).status == "failed"
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0


async def test_mid_batch_failure_rolls_back_all_requests_then_retry_succeeds(
    client, database, observed, monkeypatch
):
    plan = await preview(client, observed)
    await accept(client, plan)
    original = series_requests.submit
    calls = 0

    async def broken(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected batch failure")
        return result

    monkeypatch.setattr(series_requests, "submit", broken)
    with pytest.raises(RuntimeError, match="injected"):
        await series_requests.run(UUID(plan["id"]))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
        assert await db.scalar(select(func.count()).select_from(AcquisitionReason)) == 0
    monkeypatch.setattr(series_requests, "submit", original)
    await series_requests.run(UUID(plan["id"]))
    assert (await detail(client, plan))["status"] == "completed"


async def test_populated_downgrade_guard(client, database, observed):
    await preview(client, observed)
    response = await migrate("downgrade", "0033_catalog_series")
    assert response.returncode != 0 and "pre-upgrade backup" in response.stderr


async def test_real_preference_change_requires_new_preview_but_accepted_scope_is_frozen(
    client, database, observed
):
    from tests.integration.test_acquisition_defaults import save

    await save(client, {"desired_media": "audio", "required_narrators": ["Reader A"]})
    first = await preview(client, observed, specification={})
    assert first["specification"]["mode"] == "audio"
    assert first["specification"]["required_narrators"] == ["Reader A"]
    await save(client, {"desired_media": "ebook"})
    assert (await client.post(BASE + f"/{first['id']}/submit")).status_code == 409
    second = await preview(client, observed, specification={})
    await accept(client, second)
    await save(client, {"desired_media": "both"})
    await series_requests.run(UUID(second["id"]))
    saved = await detail(client, second)
    assert saved["status"] == "completed" and saved["specification"]["mode"] == "ebook"
    assert saved["counts"]["satisfied"] == 1 and saved["counts"]["pending"] == 1


async def test_individual_series_reason_withdrawal_changes_parent_status_projection(
    client, database, observed
):
    plan = await preview(client, observed)
    await accept(client, plan)
    await series_requests.run(UUID(plan["id"]))
    saved = await detail(client, plan)
    receipt = saved["receipt"][0]
    intent = (await client.get("/api/requests/" + receipt["request_id"])).json()
    reason = next(r for r in intent["reasons"] if r["kind"] == "series")
    response = await client.delete(f"/api/requests/{receipt['request_id']}/reasons/{reason['id']}")
    assert response.status_code == 200, response.text
    refreshed = await detail(client, plan)
    row = next(r for r in refreshed["records"] if r["work_id"] == receipt["work_id"])
    assert all(t["state"] in {"satisfied", "cancelled"} for t in row["targets"])
    await accept(client, plan)  # Replay never reactivates a withdrawn reason.
    assert not next(
        r
        for r in (await client.get("/api/requests/" + receipt["request_id"])).json()["reasons"]
        if r["id"] == reason["id"]
    )["active"]


async def test_cancel_queued_request_wins_without_creating_children(client, database, observed):
    plan = await preview(client, observed)
    await accept(client, plan)
    assert (await client.post(BASE + f"/{plan['id']}/cancel")).status_code == 200
    await series_requests.run(UUID(plan["id"]))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0


async def test_failed_enqueue_does_not_accept_preview(client, database, observed, monkeypatch):
    plan = await preview(client, observed)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected queue failure")

    monkeypatch.setattr(series_requests, "enqueue", fail)
    with pytest.raises(RuntimeError, match="queue failure"):
        await client.post(BASE + f"/{plan['id']}/submit")
    saved = await detail(client, plan)
    assert saved["status"] == "preview" and saved["accepted_at"] is None


async def test_private_series_requests_cannot_be_read_or_changed_by_another_owner(
    client, database, observed
):
    import httpx

    from app.db.models import CatalogAccount
    from app.main import create_app
    from app.security import encrypt_secrets

    plan = await preview(client, observed)
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "other",
            "password": "a long other password",
            "display_name": "Other",
            "role": "member",
        },
    )
    assert created.status_code == 201, created.text
    user = created.json()
    async with database() as db, db.begin():
        db.add(
            CatalogAccount(
                user_id=UUID(user["id"]),
                generation=1,
                enabled=True,
                encrypted_token=encrypt_secrets({"token": "other-fixture-token"}),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        signed = await other.post(
            "/api/auth/login", json={"username": "other", "password": "a long other password"}
        )
        other.headers["X-CSRF-Token"] = signed.json()["csrf_token"]
        await finish(database, await observe(other))
        assert (await other.get(BASE)).json()["total"] == 0
        assert (await other.get(BASE + f"/{plan['id']}")).status_code == 404
        for suffix in ("submit", "cancel"):
            assert (await other.post(BASE + f"/{plan['id']}/{suffix}")).status_code == 404


async def test_invalid_selection_and_other_preview_key_do_not_create_requests(
    client, database, observed
):
    for selection in ([], [observed["items"][0]["work"]["id"]] * 2, [str(uuid4())]):
        response = await client.post(
            BASE + "/preview",
            json=body(observed, work_ids=selection),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code in {409, 422}
    key = str(uuid4())
    assert (
        await client.post(BASE + "/preview", json=body(observed), headers={"Idempotency-Key": key})
    ).status_code == 201
    conflict = await client.post(
        BASE + "/preview",
        json=body(observed, specification={"mode": "audio"}),
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409


async def test_series_reason_survives_list_removal(client, database, observed):
    work = observed["items"][0]["work"]["id"]
    shelf = (await client.post("/api/lists", json={"name": "Another request reason"})).json()["id"]
    await client.post(f"/api/lists/{shelf}/entries", json={"work_id": work})
    wanted = await request(
        client, {"work_id": work, "specification": {"mode": "both"}, "reason": {"list_id": shelf}}
    )
    plan = await preview(client, observed, work_ids=[work])
    await accept(client, plan)
    await series_requests.run(UUID(plan["id"]))
    assert (await client.delete(f"/api/lists/{shelf}/entries/{work}")).status_code == 204
    reasons = (await client.get("/api/requests/" + wanted["request"]["id"])).json()["reasons"]
    assert {r["kind"]: r["active"] for r in reasons} == {"list": False, "series": True}


async def test_series_only_reason_authorizes_existing_import_review_contract(
    client, database, observed, catalog, selected
):
    from fastapi import HTTPException

    from app.db.models import AcquisitionSelection
    from app.domain.download_reviews import requester_authority

    plan = await preview(
        client,
        observed,
        work_ids=[str(catalog["work"])],
        specification={"mode": "audio", "audio_library_id": str(catalog["library"])},
    )
    await accept(client, plan)
    await series_requests.run(UUID(plan["id"]))
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        intent_id = str(selection.intent_id)
    intent = (await client.get("/api/requests/" + intent_id)).json()
    manual = next(r for r in intent["reasons"] if r["kind"] == "manual")
    assert (
        await client.delete(f"/api/requests/{intent_id}/reasons/{manual['id']}")
    ).status_code == 200
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        owner, intent, destination = await requester_authority(db, selection, lock=True)
        assert str(intent.id) == intent_id and owner.id == selection.owner_id
    assert (await client.post(BASE + f"/{plan['id']}/cancel")).status_code == 200
    async with database() as db:
        with pytest.raises(HTTPException, match="withdrawn"):
            await requester_authority(db, await db.get(AcquisitionSelection, UUID(selected["id"])))
