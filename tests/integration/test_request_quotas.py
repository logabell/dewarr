from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

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
from app.domain.permissions import MEMBER, REQUESTER
from app.domain.request_quotas import reserve_size, usage
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


@pytest.mark.parametrize(
    "automatic,permissions", [(False, MEMBER), (True, MEMBER), (False, REQUESTER)]
)
async def test_saved_request_caps_do_not_limit_new_requests(
    database, quota_setup, automatic, permissions
):
    owner, works = quota_setup
    async with database() as db, db.begin():
        (await db.get(User, owner)).permissions = permissions
        for scope in ("installation", "role:member", f"user:{owner}"):
            policy = await db.get(RequestQuotaPolicy, scope)
            if policy is None:
                policy = RequestQuotaPolicy(scope=scope)
                db.add(policy)
            policy.configuration = {
                "pending_cap": 0,
                "windows": [
                    {"medium": "combined", "window": window, "books": 0, "size_bytes": 0}
                    for window in ("day", "week", "month")
                ],
            }
    results = [await add(database, owner, work, automatic=automatic) for work in works]
    assert not any(waiting for _, waiting in results)
    async with database() as db, db.begin():
        user = await db.get(User, owner)
        targets = list(await db.scalars(select(AcquisitionTarget)))
        assert len(targets) == len(works)
        for target in targets:
            await reserve_size(db, user, target, "audio", 100 * 1024**3)
        summary = await usage(db, user)
        assert summary.bypass and not summary.windows and summary.pending_remaining is None
        assert await db.scalar(select(func.count()).select_from(RequestQuotaCharge)) == 0


async def test_previously_quota_paused_request_can_resume(database, quota_setup):
    owner, works = quota_setup
    intent_id, _ = await add(database, owner, works[0], automatic=True)
    async with database() as db, db.begin():
        intent = await db.get(AcquisitionIntent, intent_id)
        target = await db.scalar(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == intent.id)
        )
        target.state, target.message = "paused", "Waiting for quota"
        target.quota_waiting = True
        target.quota_requirement = {"medium": "audio", "size_bytes": 100 * 1024**3}
        await db.flush()
        await acquisition_lock(db, intent.work_id)
        await evaluate(db, await db.get(User, owner), intent)
        assert target.state == "wanted" and not target.quota_waiting
        assert target.quota_requirement is None


async def test_retired_quota_api_cannot_reenable_caps(client, admin, database, quota_setup):
    response = await client.put("/api/request-quotas/installation", json={"pending_cap": 0})
    assert response.status_code == 410
    assert (await client.get("/api/request-quotas")).json() == []
    summary = (await client.get("/api/request-quotas/me")).json()
    assert summary["bypass"] and summary["windows"] == []
