# ruff: noqa: F811, F401
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.models import AcquisitionReason, AcquisitionSelection, ImportEntry, Version
from app.domain import download_attempts
from app.importing import execution
from app.jobs.queue import get_queue
from app.notifications.delivery import tick
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_download_attempts import downloader, selected
from tests.integration.test_download_attempts import start as start_download
from tests.integration.test_import_destinations import route as destination_route
from tests.integration.test_import_execution import ready_route
from tests.integration.test_import_execution import start as start_import
from tests.integration.test_notifications import channel, sent

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("already_complete", [False, True])
async def test_pending_approved_and_grabbed_each_deliver_once_after_repeated_jobs(
    client, admin, database, selected, downloader, sent, already_complete
):
    await channel(
        client, ["request.pending", "request.approved", "download.started"], installation=True
    )
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        reason = await db.scalar(
            select(AcquisitionReason).where(AcquisitionReason.intent_id == selection.intent_id)
        )
        reason.approval_status = "pending"
    await tick()
    async with database() as db, db.begin():
        reason = await db.get(AcquisitionReason, reason.id)
        reason.approval_status = "approved"
    await tick()
    downloader.complete = already_complete
    response = await start_download(client, selected)
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    for _ in range(3):
        await download_attempts.run(identifier)
        await tick()
    assert [item["events"][0]["type"] for item in sent] == [
        "request.pending",
        "request.approved",
        "download.started",
    ]


async def test_confirmed_import_once_and_held_import_has_review_reason_and_link(
    client, admin, database, ready_route, sent
):
    await channel(client, ["import.available", "operation.held", "operation.failed"])
    response = await start_import(client, ready_route)
    assert response.status_code == 202, response.text
    entry_id = UUID(response.json()["entries"][0]["id"])
    async with database() as db:
        entry = await db.get(ImportEntry, entry_id)
    await execution.execute(entry.operation_id)
    await tick()
    await execution.execute(entry.operation_id)
    await tick()
    available = [
        event
        for payload in sent
        for event in payload["events"]
        if event["type"] == "import.available"
    ]
    assert len(available) == 1
    before_hold = len(sent)
    # A held ledger transition uses the safe reason and the exact inspection link.
    async with database() as db, db.begin():
        entry = await db.get(ImportEntry, entry_id)
        entry.state = "held"
        entry.message = (
            "Metadata changed; review this import. https://private.example/feed?token=secret"
        )
    await tick()
    assert len(sent) == before_hold + 1
    held = sent[-1]["events"][0]
    assert held["type"] == "operation.held"
    assert "Metadata changed" in held["message"]
    assert "private.example" not in str(held) and "secret" not in str(held)
    assert "/organization/inspections?inspection=" in held["url"]
