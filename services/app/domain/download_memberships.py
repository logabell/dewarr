"""Transfer membership is explicit; matching hashes never imply authorization."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import (
    AcquisitionIntent,
    AcquisitionSelection,
    DownloadAttempt,
    DownloadMembership,
)
from app.domain import automatic_dispatch
from app.domain.operations import transaction_lock
from app.domain.work_graph import canonical_work, graph_lock


async def for_attempt(db, attempt_id):
    return list(
        await db.scalars(
            select(AcquisitionSelection)
            .join(DownloadMembership, DownloadMembership.selection_id == AcquisitionSelection.id)
            .where(DownloadMembership.attempt_id == attempt_id)
            .order_by(AcquisitionSelection.id)
            .execution_options(populate_existing=True)
        )
    )


async def attempt_for(db, selection_id):
    return await db.scalar(
        select(DownloadAttempt)
        .join(DownloadMembership, DownloadMembership.attempt_id == DownloadAttempt.id)
        .where(DownloadMembership.selection_id == selection_id)
    )


async def lock(db, selections):
    # Freeze all list authority before principal and canonical-work locks.
    await automatic_dispatch.lock_group_principals(db, selections)
    await graph_lock(db)
    await transaction_lock(db, "request-quotas:admission")
    roots = {
        (await canonical_work(db, UUID(item.frozen["origin_work_id"]))).id for item in selections
    }
    # Incidental children retain their root's authority even when joining after
    # its import. Fence cancellation under the same sorted work locks; locking
    # the root selection before these locks would invert fulfillment's order.
    for item in selections:
        authority = (item.frozen.get("automatic_selection") or {}).get("series_authority") or {}
        if origin := authority.get("pack_origin"):
            intent = await db.get(AcquisitionIntent, UUID(origin["root_intent_id"]))
            if intent:
                roots.add((await canonical_work(db, intent.work_id)).id)
    for work_id in sorted(roots):
        await transaction_lock(db, "acquisition:" + str(work_id))
    for selection in selections:
        await db.refresh(selection)


def require_same_transfer(selections):
    """Reviewed groups freeze one physical route, while retaining every child rule."""
    first = selections[0]
    keys = (
        "source_generation",
        "artifact_sha256",
        "descriptor",
        "downloader",
        "mapping",
        "destination",
    )
    for item in selections:
        if item.owner_id != first.owner_id or item.artifact_id != first.artifact_id:
            raise HTTPException(422, "Choose your saved selections for the same source artifact")
        if (
            item.downloader_id != first.downloader_id
            or item.destination_id != first.destination_id
            or item.frozen["requirements"]["medium"] != first.frozen["requirements"]["medium"]
            or any(item.frozen.get(key) != first.frozen.get(key) for key in keys)
        ):
            raise HTTPException(
                422, "Grouped selections need the same downloader and verified import route"
            )


def require_compatible(selections, *, automatic=False):
    require_same_transfer(selections)
    for item in selections:
        proof = item.frozen.get("automatic_selection")
        if automatic and (
            not proof or not proof.get("dispatch_approval") or not proof.get("coverage")
        ):
            raise HTTPException(
                422, "Every automatic pack member needs independent coverage and dispatch consent"
            )
        if not automatic and proof:
            raise HTTPException(
                422, "Automatic selections retain their own acquisition authorization"
            )
