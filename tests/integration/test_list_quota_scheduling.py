# ruff: noqa: F401, F811
"""A real followed-list worker persists and revisits quota holds across windows."""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

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


async def test_list_of_twenty_admits_five_then_resumes_remaining_fifteen(
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
        assert sum(book.state == "searching" for book in books) == 5
        assert sum(book.message.startswith("Waiting for quota") for book in books) == 15
        assert all(book.next_check_at is not None for book in books)
    for waiting in (10, 5, 0):
        async with database() as db, db.begin():
            await db.execute(
                update(RequestQuotaCharge).values(
                    admitted_at=RequestQuotaCharge.admitted_at - timedelta(days=8)
                )
            )
        await tick(database, policy, worker=False, force_books=True)
        async with database() as db:
            targets = list(
                await db.scalars(
                    select(AcquisitionTarget).where(AcquisitionTarget.quota_waiting.is_(True))
                )
            )
            assert len(targets) == waiting
