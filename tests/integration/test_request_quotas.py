import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.db.models import (
    AcquisitionIntent,
    AcquisitionTarget,
    RequestQuotaCharge,
    RequestQuotaPolicy,
    User,
    Work,
    WorkMetadataSource,
)
from app.domain.acquisition import RequestReason, RequestSpec, evaluate, submit
from app.domain.permissions import BYPASS_QUOTAS, MEMBER, REQUESTER
from app.domain.request_quotas import QuotaExceeded, effective, reserve_size, usage
from app.domain.work_graph import acquisition_lock

pytestmark = pytest.mark.integration


@pytest.fixture
async def quota_setup(database):
    async with database() as db, db.begin():
        user = User(username="quota-reader", display_name="Reader", permissions=MEMBER)
        works = [Work(title=f"Book {index}", authors=["Writer"]) for index in range(20)]
        db.add_all([user, *works])
        await db.flush()
        for work in works:
            db.add(
                WorkMetadataSource(
                    work_id=work.id,
                    provider="hardcover",
                    external_id=str(work.id),
                    fetched_at=datetime.now(UTC),
                    snapshot={},
                )
            )
        db.add(
            RequestQuotaPolicy(
                scope="installation",
                configuration={"windows": [{"medium": "audio", "window": "week", "books": 5}]},
            )
        )
        return user.id, [work.id for work in works]


async def add(database, owner, work, *, automatic=False, key=None, mode="audio"):
    async with database() as db, db.begin():
        user = await db.get(User, owner)
        intent, _ = await submit(
            db,
            user,
            work,
            RequestSpec(mode=mode),
            RequestReason(),
            key or str(uuid4()),
            automatic=automatic,
        )
        target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == intent.id)
        )
        return intent.id, target.quota_waiting


async def test_sixth_manual_request_rejected_and_idempotent(database, quota_setup):
    owner, works = quota_setup
    for work in works[:5]:
        assert not (await add(database, owner, work))[1]
    assert not (await add(database, owner, works[0]))[1]
    with pytest.raises(QuotaExceeded) as failure:
        await add(database, owner, works[5])
    assert failure.value.status_code == 429
    assert "Capacity returns at" in failure.value.detail
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 5
        summary = await usage(db, await db.get(User, owner))
        assert summary.windows[0].remaining_books == 0


async def test_twenty_automatic_requests_resume_five_per_week(database, quota_setup):
    owner, works = quota_setup
    results = [await add(database, owner, work, automatic=True) for work in works]
    assert sum(waiting for _, waiting in results) == 15
    for remaining in (10, 5, 0):
        async with database() as db, db.begin():
            await db.execute(
                update(RequestQuotaCharge).values(
                    admitted_at=RequestQuotaCharge.admitted_at - timedelta(days=8)
                )
            )
        async with database() as db, db.begin():
            user = await db.get(User, owner)
            for intent_id, _ in results:
                intent = await db.get(AcquisitionIntent, intent_id)
                await acquisition_lock(db, intent.work_id)
                await evaluate(db, user, intent)
        async with database() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AcquisitionTarget)
                    .where(AcquisitionTarget.quota_waiting.is_(True))
                )
                == remaining
            )
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(RequestQuotaCharge)) == 20


async def test_parallel_requests_cannot_overdraw_and_rollback_releases_capacity(
    database, quota_setup
):
    owner, works = quota_setup
    results = await asyncio.gather(
        *(add(database, owner, work) for work in works[:10]), return_exceptions=True
    )
    assert sum(isinstance(item, QuotaExceeded) for item in results) == 5
    assert all(isinstance(item, (tuple, QuotaExceeded)) for item in results)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(RequestQuotaCharge)) == 5


async def test_overrides_bypass_and_pending_cap(database, quota_setup):
    owner, works = quota_setup
    async with database() as db, db.begin():
        db.add(RequestQuotaPolicy(scope="role:member", configuration={"windows": [{"books": 1}]}))
        db.add(RequestQuotaPolicy(scope=f"user:{owner}", configuration={"pending_cap": 1}))
        user = await db.get(User, owner)
        user.permissions = REQUESTER
        assert (await effective(db, user))[1] == f"user:{owner}"
    await add(database, owner, works[0])
    with pytest.raises(QuotaExceeded) as failure:
        await add(database, owner, works[1])
    assert "approver decides" in failure.value.detail
    async with database() as db, db.begin():
        user = await db.get(User, owner)
        user.permissions |= BYPASS_QUOTAS
    await add(database, owner, works[1])


async def test_size_limits_and_replacement_admission(database, quota_setup):
    owner, works = quota_setup
    async with database() as db, db.begin():
        row = await db.get(RequestQuotaPolicy, "installation")
        row.configuration = {
            "windows": [{"medium": "combined", "window": "day", "size_bytes": 100}]
        }
    first, _ = await add(database, owner, works[0])
    second, _ = await add(database, owner, works[1])
    async with database() as db, db.begin():
        user = await db.get(User, owner)
        target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == first)
        )
        await acquisition_lock(db, works[0])
        await reserve_size(db, user, target, "audio", 70)
        await reserve_size(db, user, target, "audio", 70)
    with pytest.raises(QuotaExceeded):
        async with database() as db, db.begin():
            user = await db.get(User, owner)
            target = await db.scalar(
                select(AcquisitionTarget).where(AcquisitionTarget.intent_id == second)
            )
            await acquisition_lock(db, works[1])
            await reserve_size(db, user, target, "audio", 40)


async def test_named_role_user_override_and_admin_bypass(database, quota_setup):
    from app.db.models import PermissionRole

    owner, works = quota_setup
    async with database() as db, db.begin():
        role = PermissionRole(name="Readers", permissions=MEMBER)
        db.add(role)
        await db.flush()
        user = await db.get(User, owner)
        user.permission_role_id = role.id
        db.add(
            RequestQuotaPolicy(scope=f"role:{role.id}", configuration={"windows": [{"books": 1}]})
        )
        await db.flush()
        assert (await effective(db, user))[1] == f"role:{role.id}"
    await add(database, owner, works[0])
    with pytest.raises(QuotaExceeded):
        await add(database, owner, works[1])
    async with database() as db, db.begin():
        db.add(RequestQuotaPolicy(scope=f"user:{owner}", configuration={"windows": [{"books": 2}]}))
    await add(database, owner, works[1])
    with pytest.raises(QuotaExceeded):
        await add(database, owner, works[2])
    async with database() as db, db.begin():
        (await db.get(User, owner)).role = "admin"
    await add(database, owner, works[2])


async def test_admin_approval_exemption_releases_usage(database, quota_setup):
    from app.db.models import AcquisitionReason

    owner, works = quota_setup
    async with database() as db, db.begin():
        approver = User(username="approver", display_name="Admin", role="admin")
        db.add(approver)
        await db.flush()
        approver_id = approver.id
        (await db.get(User, owner)).permissions = REQUESTER
        row = await db.get(RequestQuotaPolicy, "installation")
        row.configuration = {"exempt_admin_approved": True, "windows": [{"books": 1}]}
    intent_id, _ = await add(database, owner, works[0])
    async with database() as db, db.begin():
        reason = await db.scalar(
            select(AcquisitionReason).where(AcquisitionReason.intent_id == intent_id)
        )
        reason.approval_status, reason.decided_by = "approved", approver_id
        await acquisition_lock(db, works[0])
        await evaluate(db, await db.get(User, owner), await db.get(AcquisitionIntent, intent_id))
        assert (await usage(db, await db.get(User, owner))).windows[0].used_books == 0
    await add(database, owner, works[1])


async def test_existing_library_and_shared_transfer_are_free(database, quota_setup):
    from app.db.models import (
        AcquisitionReservation,
        AssetContains,
        Integration,
        Library,
        LibraryAsset,
        LibraryGrant,
    )

    owner, works = quota_setup
    async with database() as db, db.begin():
        library_connection = Integration(
            kind="audiobookshelf",
            name="Shared",
            base_url="http://fixture.invalid",
            encrypted_secrets="unused",
        )
        db.add(library_connection)
        await db.flush()
        library = Library(
            integration_id=library_connection.id,
            external_id="books",
            name="Books",
            last_complete_sync=datetime.now(UTC),
        )
        db.add(library)
        await db.flush()
        db.add(LibraryGrant(user_id=owner, library_id=library.id))
        asset = LibraryAsset(
            library_id=library.id,
            external_id="existing",
            medium="audio",
            state="present",
            full_content=True,
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(asset_id=asset.id, work_id=works[0], verified=True))
        (await db.get(RequestQuotaPolicy, "installation")).configuration = {
            "windows": [{"books": 0}]
        }
        db.add(
            AcquisitionReservation(
                work_id=works[1],
                scope=f"unconfigured:{owner}",
                state="committed",
                requirements=RequestSpec(mode="audio").rule("audio"),
            )
        )
    await add(database, owner, works[0])
    intent_id, _ = await add(database, owner, works[1])
    async with database() as db, db.begin():
        intent = await db.get(AcquisitionIntent, intent_id)
        await acquisition_lock(db, intent.work_id)
        await evaluate(db, await db.get(User, owner), intent)
        target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == intent_id)
        )
        assert not target.quota_waiting and target.reservation_id
        assert (await db.get(AcquisitionReservation, target.reservation_id)).state == "committed"
        assert (await usage(db, await db.get(User, owner))).windows[0].used_books == 0


async def test_automatic_size_hold_is_durable_and_resumes(database, quota_setup):
    from app.db.models import Operation
    from app.domain.request_quotas import hold_selection

    owner, works = quota_setup
    async with database() as db, db.begin():
        (await db.get(RequestQuotaPolicy, "installation")).configuration = {
            "windows": [{"window": "day", "size_bytes": 100}]
        }
    first, _ = await add(database, owner, works[0], automatic=True)
    second, _ = await add(database, owner, works[1], automatic=True)
    async with database() as db, db.begin():
        user = await db.get(User, owner)
        await acquisition_lock(db, works[0])
        first_target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == first)
        )
        await reserve_size(db, user, first_target, "audio", 80)
        target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == second)
        )
        op = Operation(
            owner_id=owner,
            kind="automatic.selection",
            idempotency_key="quota-size-operation",
            payload={"command": {"intent_id": str(second), "slot": "audio"}},
        )
        db.add(op)
        try:
            await reserve_size(db, user, target, "audio", 40)
        except QuotaExceeded as error:
            await hold_selection(db, op, error)
        await evaluate(db, user, await db.get(AcquisitionIntent, second))
        assert target.quota_waiting
    async with database() as db, db.begin():
        await db.execute(
            update(RequestQuotaCharge).values(
                size_at=RequestQuotaCharge.size_at - timedelta(days=2)
            )
        )
        user = await db.get(User, owner)
        await acquisition_lock(db, works[1])
        await evaluate(db, user, await db.get(AcquisitionIntent, second))
        target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == second)
        )
        assert not target.quota_waiting and target.state == "wanted"


async def test_quota_api_access_and_current_remaining(client, admin, database, quota_setup):
    from app.security import hash_password
    from tests.integration.test_request_approvals import session_for

    owner, works = quota_setup
    async with database() as db, db.begin():
        (await db.get(User, owner)).password_hash = hash_password("reader quota password")
    saved = await client.put(
        f"/api/request-quotas/user:{owner}",
        json={"windows": [{"medium": "audio", "window": "week", "books": 1}]},
    )
    assert saved.status_code == 200
    await add(database, owner, works[0])
    reader = await session_for("quota-reader", "reader quota password")
    try:
        summary = await reader.get("/api/request-quotas/me")
        assert summary.status_code == 200
        assert summary.json()["windows"][0]["remaining_books"] == 0
        assert summary.json()["windows"][0]["capacity_returns_at"]
        refused = await reader.post(
            "/api/requests",
            json={"work_id": str(works[1]), "specification": {"mode": "audio"}},
            headers={"Idempotency-Key": "sixth-api-refusal"},
        )
        assert refused.status_code == 429 and "Retry-After" in refused.headers
        assert "Capacity returns at" in refused.json()["detail"]
        assert (await reader.get("/api/request-quotas/users")).status_code == 403
        assert (await reader.put("/api/request-quotas/installation", json={})).status_code == 403
    finally:
        await reader.aclose()
    summary = await client.get("/api/request-quotas/users")
    assert (
        next(u for u in summary.json() if u["user_id"] == str(owner))["windows"][0]["used_books"]
        == 1
    )
    assert (await client.delete(f"/api/request-quotas/user:{owner}")).status_code == 204
    async with database() as db:
        assert (await usage(db, await db.get(User, owner))).source == "installation"
