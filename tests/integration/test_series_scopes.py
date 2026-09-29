# ruff: noqa: F401, F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.db.models import AcquisitionIntent, CatalogSeries, Operation, User, Work
from app.domain import series_requests, series_scopes
from tests.integration.test_acquisition import catalog
from tests.integration.test_catalog_series import detail as series_detail
from tests.integration.test_catalog_series import finish, record, service
from tests.integration.test_catalog_series import start as observe
from tests.integration.test_series_requests import BASE as REQUESTS
from tests.integration.test_series_requests import accept, observed
from tests.integration.test_series_requests import body as request_body

pytestmark = pytest.mark.integration
BASE = "/api/catalog/series/hardcover/9/main-books"


async def catalog_detail(client):
    # Explicit scope review can inspect every observation, including entries
    # deliberately excluded from the default English main reading order.
    return await series_detail(client, section="all")


def body(observed, **updates):
    return {
        "work_ids": [r["work"]["id"] for r in observed["items"]],
        "expected_generation": observed["generation"],
        "expected_review_id": None,
        "confirm_main_membership": True,
        **updates,
    }


async def save(client, observed, key=None, **updates):
    response = await client.post(
        BASE, json=body(observed, **updates), headers={"Idempotency-Key": key or str(uuid4())}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def preview(client, observed, review):
    response = await client.post(
        REQUESTS + "/preview",
        json=request_body(observed, scope="complete_series", scope_review_id=review["id"]),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_review_is_explicit_durable_finite_and_does_not_acquire(client, database, observed):
    assert (await client.get(BASE)).json()["state"] == "not-reviewed"
    async with database() as db:
        before = await db.scalar(text("SELECT count(*) FROM book_queue.procrastinate_jobs"))
    review = await save(client, observed, work_ids=[observed["items"][0]["work"]["id"]])
    assert review["state"] == "current" and review["revision"] == 1
    assert len(review["books"]) == 1
    assert (await client.get(BASE)).json() == review
    async with database() as db:
        operation = await db.get(Operation, UUID(review["id"]))
        assert operation.job_id is None
        assert operation.payload["evidence_kind"] == "user-reviewed-main-books"
        assert len(operation.payload["omitted"]) == 1
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
        assert await db.scalar(text("SELECT count(*) FROM book_queue.procrastinate_jobs")) == before


@pytest.mark.parametrize(
    "updates",
    [
        {"confirm_main_membership": False},
        {"work_ids": []},
        {"work_ids": [str(uuid4()), str(uuid4())]},
        {"expected_generation": 200},
    ],
)
async def test_invalid_or_stale_selection_cannot_be_saved(client, observed, updates):
    response = await client.post(
        BASE, json=body(observed, **updates), headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code in {409, 422}, response.text
    assert (await client.get(BASE)).json()["state"] == "not-reviewed"


@pytest.mark.parametrize(
    "change",
    [
        {"release_date": None},
        {"release_date": "2999-01-01"},
        {"compilation": True},
        {"partial": True},
        {"canonical_id": "77"},
    ],
)
async def test_uncertain_or_ineligible_members_cannot_be_confirmed(
    client,
    database,
    service,
    observed,
    change,
):
    service.items = [record(**change)]
    await finish(database, await observe(client, str(uuid4())))
    response = await client.post(
        BASE, json=body(await catalog_detail(client)), headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 409, response.text


async def test_positions_never_automatically_assert_main_membership(
    client,
    database,
    service,
    observed,
):
    service.items = [
        record(position="1"),
        record(2, 43, position="1"),
        record(3, 44, position="2.5"),
        record(4, 45, position=None),
    ]
    await finish(database, await observe(client, str(uuid4())))
    assert (await client.get(BASE)).json()["state"] == "not-reviewed"
    review = await save(client, await catalog_detail(client))
    assert len(review["books"]) == 4
    assert sum(bool(b["warnings"]) for b in review["books"]) == 2


async def test_catalog_refresh_preserves_finite_review_but_not_changed_evidence(
    client,
    database,
    service,
    observed,
):
    review = await save(client, observed)
    service.items = [record(), record(2, 43), record(3, 44)]
    await finish(database, await observe(client, str(uuid4())))
    current = (await client.get(BASE)).json()
    assert current["state"] == "current" and len(current["books"]) == 2
    assert current["id"] == review["id"] and current["catalog_generation"] == 1
    service.items[0]["position"] = "4"
    await finish(database, await observe(client, str(uuid4())))
    assert (await client.get(BASE)).json()["state"] == "changed"


@pytest.mark.parametrize(
    "change", ["removed", "title", "expired", "details", "source_title", "merge"]
)
async def test_review_reports_relevant_staleness(client, database, service, observed, change):
    await save(client, observed)
    if change in {"removed", "details", "source_title"}:
        if change == "removed":
            service.items = [record()]
        elif change == "details":
            service.items[0]["details"] = "Novella"
        else:
            service.items[0]["book"]["title"] = "Changed provider title"
        await finish(database, await observe(client, str(uuid4())))
    else:
        async with database() as db, db.begin():
            if change == "merge":
                (await db.get(Work, UUID(observed["items"][0]["work"]["id"]))).redirect_to = UUID(
                    observed["items"][1]["work"]["id"]
                )
            elif change == "title":
                (await db.get(Work, UUID(observed["items"][0]["work"]["id"]))).title = "Changed"
            else:
                (await db.scalar(select(CatalogSeries))).fetched_at = datetime.now(UTC) - timedelta(
                    days=2
                )
    result = (await client.get(BASE)).json()
    assert result["state"] == ("needs-refresh" if change == "expired" else "changed")


async def test_idempotency_replacement_withdrawal_never_resurrects_review(
    client,
    database,
    observed,
):
    key = str(uuid4())
    first = await save(client, observed, key)
    assert await save(client, observed, key) == first
    second = await save(client, observed, expected_review_id=first["id"])
    assert second["revision"] == 2
    replay = await save(client, observed, key)
    assert replay["id"] == first["id"] and replay["state"] == "superseded"
    assert (await client.delete(BASE + "/" + first["id"])).status_code == 409
    for _ in range(2):
        response = await client.delete(BASE + "/" + second["id"])
        assert response.status_code == 200 and response.json()["state"] == "withdrawn"
    assert (await client.get(BASE)).json()["id"] == second["id"]
    third = await save(client, observed, expected_review_id=second["id"])
    assert third["revision"] == 3


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_review_commands_serialize(client, observed, same_key):
    keys = [str(uuid4()), str(uuid4())]
    if same_key:
        keys[1] = keys[0]
    replies = await asyncio.gather(
        *[client.post(BASE, json=body(observed), headers={"Idempotency-Key": key}) for key in keys]
    )
    assert sorted(r.status_code for r in replies) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert replies[0].json()["id"] == replies[1].json()["id"]


async def test_review_reuse_is_checked_before_acceptance_then_request_is_independent(
    client,
    database,
    observed,
):
    review = await save(client, observed)
    plan = await preview(client, observed, review)
    await accept(client, plan)
    await client.delete(BASE + "/" + review["id"])
    await series_requests.run(UUID(plan["id"]))
    result = (await client.get(REQUESTS + "/" + plan["id"])).json()
    assert result["status"] == "completed" and len(result["receipt"]) == 2
    async with database() as db:
        saved = await db.get(Operation, UUID(plan["id"]))
        assert saved.payload["scope_review"]["id"] == review["id"]


@pytest.mark.parametrize("change", ["withdraw", "replace"])
async def test_stale_scope_proof_cannot_authorize_unaccepted_request(
    client,
    observed,
    change,
):
    review = await save(client, observed)
    plan = await preview(client, observed, review)
    if change == "withdraw":
        await client.delete(BASE + "/" + review["id"])
    else:
        await save(client, observed, expected_review_id=review["id"])
    response = await client.post(REQUESTS + "/" + plan["id"] + "/submit")
    assert response.status_code == 409, response.text


async def test_review_requires_exact_selected_set_and_cannot_be_forged(client, observed):
    review = await save(client, observed)
    for updates in [
        {"scope_review_id": str(uuid4())},
        {"work_ids": [observed["items"][0]["work"]["id"]]},
    ]:
        response = await client.post(
            REQUESTS + "/preview",
            json=request_body(
                observed, scope="complete_series", scope_review_id=review["id"], **updates
            )
            if "scope_review_id" not in updates
            else request_body(observed, scope="complete_series", **updates),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 409, response.text


async def test_viewer_cannot_review_or_use_private_review(client, database, observed, admin):
    review = await save(client, observed)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "viewer"
    assert (await client.get(BASE)).status_code == 403
    assert (await client.delete(BASE + "/" + review["id"])).status_code == 403
    response = await client.post(
        BASE, json=body(observed), headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 403


async def test_other_account_cannot_obtain_or_reuse_review(client, database, observed):
    from app.security import hash_password

    review = await save(client, observed)
    async with database() as db, db.begin():
        db.add(
            User(
                username="other",
                display_name="Other",
                role="member",
                password_hash=hash_password("other long password"),
            )
        )
    response = await client.post(
        "/api/auth/login",
        json={
            "username": "other",
            "password": "other long password",
        },
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    response = await client.get(BASE)
    assert response.status_code == 409 and review["id"] not in response.text
    assert (await client.delete(BASE + "/" + review["id"])).status_code == 409


async def test_unaccepted_withdrawal_and_submit_are_serialized(client, database, observed):
    review = await save(client, observed)
    plan = await preview(client, observed, review)
    submitted, withdrawn = await asyncio.gather(
        client.post(REQUESTS + "/" + plan["id"] + "/submit"),
        client.delete(BASE + "/" + review["id"]),
    )
    assert withdrawn.status_code == 200
    assert submitted.status_code in {202, 409}, submitted.text
    async with database() as db:
        operation = await db.get(Operation, UUID(plan["id"]))
        assert bool(operation.payload.get("accepted_at")) == (submitted.status_code == 202)
        assert bool(operation.job_id) == (submitted.status_code == 202)


async def test_confirmation_validation_and_idempotency_key_conflict(client, observed):
    key = str(uuid4())
    review = await save(client, observed, key)
    response = await client.post(
        BASE, json=body(observed, expected_review_id=review["id"]), headers={"Idempotency-Key": key}
    )
    assert response.status_code == 409
    work = observed["items"][0]["work"]["id"]
    response = await client.post(
        BASE, json=body(observed, work_ids=[work, work]), headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 422


async def test_catalog_publication_serializes_before_review_row_and_graph_locks(
    client,
    database,
    observed,
    admin,
):
    from app.domain.operations import transaction_lock

    task = None
    try:
        async with database() as publisher, publisher.begin():
            await transaction_lock(publisher, f"series-catalog:{admin['id']}:9")
            pid = await publisher.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(save(client, observed))
            async with asyncio.timeout(5):
                while True:
                    assert not task.done(), "Review bypassed the catalog publication lock"
                    blocked = await publisher.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks waiting "
                            "JOIN pg_locks held USING "
                            "(locktype, database, classid, objid, objsubid) "
                            "WHERE held.pid=:pid AND held.granted AND NOT waiting.granted "
                            "AND held.locktype='advisory')"
                        ),
                        {"pid": pid},
                    )
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
            # The waiting review must not retain a row lock needed by publication.
            assert await publisher.scalar(text("SELECT id FROM catalog_series FOR UPDATE NOWAIT"))
        async with asyncio.timeout(5):
            assert (await task)["state"] == "current"
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_restored_scope_requires_fresh_review_and_preserves_old_evidence(
    client, admin, database, observed
):
    from tests.integration.test_recovery_approvals import seal_history

    review = await save(client, observed)
    await seal_history(database, admin)
    historical = (await client.get(BASE)).json()
    assert historical["state"] == "changed" and historical["id"] == review["id"]
    assert "predates restore" in historical["message"]
    rejected = await client.post(
        REQUESTS + "/preview",
        json=request_body(observed, scope="complete_series", scope_review_id=review["id"]),
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert rejected.status_code == 409 and "predates restore" in rejected.text
    fresh = await save(client, observed, expected_review_id=review["id"])
    assert fresh["state"] == "current" and fresh["id"] != review["id"]
    assert (await preview(client, observed, fresh))["id"]
