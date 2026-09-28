# ruff: noqa: F401, F811
"""A followed-list worker ignores retired quota policies and keeps requests stable."""

import pytest
from sqlalchemy import func, select

from app.db.models import (
    AcquisitionTarget,
    LibraryGrant,
    ListAcquisitionBook,
    RequestQuotaCharge,
    RequestQuotaPolicy,
    User,
    Work,
)
from app.domain.permissions import AUTOMATE, MEMBER
from tests.integration.test_list_policies import (
    activate,
    add,
    authorized,
    catalog,
    policy_fixture,
    preview,
    selection_route,
    source,
    tick,
)

pytestmark = pytest.mark.integration


async def test_list_of_twenty_ignores_saved_quota_and_reuses_existing_requests(
    client, admin, database, catalog, policy_fixture
):
    from app.security import hash_password
    from tests.integration.test_request_approvals import session_for

    async with database() as db, db.begin():
        user = User(
            username="list-quota-reader",
            display_name="List reader",
            role="member",
            permissions=MEMBER | AUTOMATE,
            can_automate=True,
            password_hash=hash_password("list quota password"),
        )
        db.add(user)
        await db.flush()
        db.add(LibraryGrant(user_id=user.id, library_id=catalog["library"]))
        db.add(
            RequestQuotaPolicy(
                scope="installation",
                configuration={"windows": [{"medium": "audio", "books": 5, "window": "week"}]},
            )
        )
    reader = await session_for("list-quota-reader", "list quota password")
    shelf = (await reader.post("/api/lists", json={"name": "Reader shelf"})).json()["id"]
    fixture = {**policy_fixture, "list": shelf}
    policy = await activate(reader, fixture, await preview(reader, fixture))
    async with database() as db, db.begin():
        works = [Work(title=f"Future book {i}", authors=["Writer"]) for i in range(20)]
        db.add_all(works)
        await db.flush()
        ids = [str(work.id) for work in works]
    for work in ids:
        await add(reader, fixture, work)
    await reader.aclose()
    await tick(database, policy, worker=False, force_books=True)
    async with database() as db:
        books = list(await db.scalars(select(ListAcquisitionBook)))
        assert len(books) == 20
        assert all(book.state == "searching" for book in books)
        assert all(book.next_check_at is not None for book in books)
        intents = {book.work_id: book.intent_id for book in books}
        assert all(intents.values())
    await tick(database, policy, worker=False, force_books=True)
    async with database() as db:
        books = list(await db.scalars(select(ListAcquisitionBook)))
        assert {book.work_id: book.intent_id for book in books} == intents
        targets = list(
            await db.scalars(
                select(AcquisitionTarget).where(AcquisitionTarget.intent_id.in_(intents.values()))
            )
        )
        assert len(targets) == 20
        assert not any(target.quota_waiting for target in targets)
        assert await db.scalar(select(func.count()).select_from(RequestQuotaCharge)) == 0
