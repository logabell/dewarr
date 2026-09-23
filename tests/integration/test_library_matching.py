import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.catalog_types import BookData, SearchPage
from app.db.models import (
    AuditEvent,
    CatalogAccount,
    MetadataSettings,
    Operation,
    Work,
    WorkMetadataSource,
)
from app.domain.library_matching import match_library
from app.security import encrypt_secrets
from tests.contracts.test_audiobookshelf import ABSFixture, connect, sync, titled

pytestmark = pytest.mark.integration

BOOKS = json.loads((Path(__file__).parents[1] / "fixtures" / "hardcover-books.json").read_text())


async def opt_in(database, admin, enabled=True, series=False):
    async with database() as db, db.begin():
        settings = await db.get(MetadataSettings, 1)
        if not settings:
            settings = MetadataSettings(id=1, preferences={})
            db.add(settings)
        settings.preferences = {
            **settings.preferences,
            "automatic_library_matching": enabled,
            "write_library_series": series,
        }
        db.add(
            CatalogAccount(
                user_id=UUID(admin["id"]),
                encrypted_token=encrypt_secrets({"token": "library-match-token"}),
                generation=1,
                enabled=True,
            )
        )


def hardcover(calls):
    books = {key: BookData.model_validate(value) for key, value in BOOKS.items()}
    by_id = {book.external_id: book for book in books.values()}

    async def call(db, user_id, provider, operation, *args):
        calls.append((operation, args))
        await db.rollback()
        if operation == "search":
            items = (
                [books["storm_front"], books["storm_front_adaptation"]]
                if args[0].startswith("storm front")
                else [books["red_rising"]]
            )
            return (
                SearchPage(provider="hardcover", items=items, page=1, has_more=False),
                False,
                None,
            )
        if operation == "title_search":
            return SearchPage(provider="hardcover", items=[], page=1, has_more=False), False, None
        return by_id[args[0]], False, None

    return call


async def match_operations(database):
    async with database() as db:
        return list(
            await db.scalars(
                select(Operation)
                .where(Operation.kind == "library.match")
                .order_by(Operation.created_at)
            )
        )


async def test_sync_matches_library_books_it_can_verify(client, admin, database, monkeypatch):
    await opt_in(database, admin)
    connection = await connect(client)
    fixture = ABSFixture(
        {
            "storm": titled(
                "storm", "Storm Front [Dramatized Adaptation]", ["Full Cast", "Jim Butcher"]
            ),
            "unknown": titled("unknown", "A Book Hardcover Lacks", ["Pierce Brown"]),
        }
    )
    await sync(client, connection, fixture, "match-first-sync")
    (operation,) = await match_operations(database)
    calls = []
    monkeypatch.setattr("app.api.metadata.provider_call", hardcover(calls))
    await match_library(operation.id)
    async with database() as db:
        operation = await db.get(Operation, operation.id)
        assert operation.status == "completed"
        assert operation.payload["matched"] == 1 and operation.payload["checked"] == 2
        storm = await db.scalar(select(Work).where(Work.title == "Storm Front"))
        source = await db.scalar(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id == storm.id)
        )
        assert source.accepted and source.external_id == "1001" and not source.manual_match
        unknown = await db.scalar(select(Work).where(Work.title == "A Book Hardcover Lacks"))
        outcome = unknown.metadata_fields["auto_match"]
        assert outcome["status"] == "unmatched"
        assert [c["title"] for c in outcome["candidates"]] == ["Red Rising"]

    # Nothing changed: the next pass asks Hardcover nothing.
    calls.clear()
    await sync(client, connection, fixture, "match-second-sync")
    operations = await match_operations(database)
    assert len(operations) == 2
    await match_library(operations[-1].id)
    assert calls == []


async def test_matching_is_on_by_default():
    from app.domain.catalog_metadata import MetadataPreferences

    assert MetadataPreferences().automatic_library_matching
    assert not MetadataPreferences().write_library_series


async def test_nothing_is_queued_when_the_admin_turns_matching_off(client, admin, database):
    await opt_in(database, admin, enabled=False)
    connection = await connect(client)
    fixture = ABSFixture({"storm": titled("storm", "Storm Front", ["Jim Butcher"])})
    await sync(client, connection, fixture, "match-opt-out-sync")
    assert await match_operations(database) == []


async def test_a_matched_series_is_added_only_to_items_without_one(
    client, admin, database, monkeypatch
):
    await opt_in(database, admin, series=True)
    connection = await connect(client)
    listed = titled("listed", "Storm Front", ["Jim Butcher"])
    listed["media"]["metadata"]["series"] = [{"id": "s1", "name": "Dresden", "sequence": "1"}]
    fixture = ABSFixture(
        {
            "storm": titled(
                "storm", "Storm Front [Dramatized Adaptation]", ["Full Cast", "Jim Butcher"]
            ),
            "listed": listed,
        }
    )
    await sync(client, connection, fixture, "series-sync")
    (operation,) = await match_operations(database)
    monkeypatch.setattr("app.api.metadata.provider_call", hardcover([]))
    writes = []

    async def update_series(self, item_id, name, sequence):
        writes.append((item_id, name, sequence))

    monkeypatch.setattr(Audiobookshelf, "update_series", update_series)
    await match_library(operation.id)
    assert writes == [("storm", "The Dresden Files", "1")]
    async with database() as db:
        operation = await db.get(Operation, operation.id)
        assert operation.payload["series_written"] == 1
        assert "added the series to 1 Audiobookshelf items" in operation.message
        event = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.series.written")
        )
        assert event.detail == {"item_id": "storm", "series": "The Dresden Files", "sequence": "1"}
