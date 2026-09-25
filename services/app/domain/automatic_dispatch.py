"""Explicit automatic-download consent and current standing import-route authority."""

from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    AutomaticImportPolicy,
    ImportDestination,
    Integration,
    Library,
    Operation,
    User,
)
from app.domain.book_sources import identity
from app.domain.release_profiles import ProfileSnapshot, refresh_profile, same_profile
from app.domain.visibility import visible_library
from app.domain.work_graph import canonical_work
from app.importing.settings import current_profile


def consent(selection):
    return (selection.frozen.get("automatic_selection") or {}).get("dispatch_approval")


async def lock_principals(db, owner_id, approval, list_authority=None, series_authority=None):
    from app.domain.list_policies import lock_authority

    origins = [list_authority, (series_authority or {}).get("list_origin", {}).get("authority")]
    lists = {value["list_id"]: value for value in origins if value}
    for key in sorted(lists):
        await lock_authority(db, lists[key])
    from app.domain.series_acquisition import lock_authority as lock_series

    await lock_series(db, series_authority)
    if approval:
        await db.scalars(
            select(User)
            .where(User.id.in_([owner_id, UUID(approval["approved_by"])]))
            .order_by(User.id)
            .with_for_update(read=True)
        )


async def lock_group_principals(db, selections, *, additional_user_ids=()):
    from app.domain.list_policies import lock_authority

    proofs = [
        (
            item.owner_id,
            consent(item),
            (item.frozen.get("automatic_selection") or {}).get("list_authority"),
        )
        for item in selections
    ]
    from app.domain.series_acquisition import lock_authority as lock_series

    series = {
        proof["operation_id"]: proof
        for item in selections
        if (proof := (item.frozen.get("automatic_selection") or {}).get("series_authority"))
    }
    lists = {proof["list_id"]: proof for _, _, proof in proofs if proof}
    for proof in series.values():
        if saved := proof.get("list_origin"):
            lists[saved["authority"]["list_id"]] = saved["authority"]
    for key in sorted(lists):
        await lock_authority(db, lists[key])
    for key in sorted(series):
        await lock_series(db, series[key])
    principals = {owner for owner, _, _ in proofs} | set(additional_user_ids)
    principals.update(UUID(approval["approved_by"]) for _, approval, _ in proofs if approval)
    if principals:
        await db.scalars(
            select(User).where(User.id.in_(principals)).order_by(User.id).with_for_update(read=True)
        )


async def approve_route(
    db, owner_id, destination_id, revision, *, expected=None, lock=False, mapping=None
):
    from app.importing.automatic import check_policy

    if get_settings().recovery_mode:
        raise HTTPException(409, "Downloads are paused while this installation is in recovery mode")
    if not get_settings().download_dispatch_enabled:
        raise HTTPException(
            409,
            "Downloads are disabled on this server. Ask an administrator to set "
            "BOOK_DOWNLOAD_DISPATCH_ENABLED=true and restart Dewarr.",
        )
    user = await db.get(User, owner_id, populate_existing=True)
    if not user or not user.active or user.role == "viewer":
        raise HTTPException(403, "The requesting account can no longer acquire books")
    destination = await db.get(ImportDestination, destination_id, populate_existing=True)
    if not destination or not await db.scalar(
        select(Library.id)
        .join(Integration)
        .where(
            Library.id == destination.library_id,
            Library.accessible.is_(True),
            Integration.enabled.is_(True),
            visible_library(user),
        )
    ):
        raise HTTPException(404, "Automatic import destination is not accessible")
    query = select(AutomaticImportPolicy).where(
        AutomaticImportPolicy.destination_id == destination_id
    )
    if lock:
        query = query.with_for_update(read=True)
    policy = await db.scalar(query.execution_options(populate_existing=True))
    if not policy:
        raise HTTPException(409, "An administrator must approve automatic imports for this route")
    snapshot = {
        "policy_id": str(policy.id),
        "policy_generation": policy.generation,
        "approved_by": str(policy.approved_by),
        "destination_id": str(destination_id),
        "destination_revision": revision,
    }
    if expected is not None and snapshot != expected:
        raise HTTPException(409, "Automatic import approval changed; review acquisition again")
    _, _, _, current = await check_policy(
        db, SimpleNamespace(policy_id=policy.id, policy_generation=policy.generation), lock=lock
    )
    if current.revision != revision or (await current_profile(db)).layout != "conventional":
        raise HTTPException(409, "Verify a supported automatic import route before downloading")
    if mapping is not None:
        from app.importing.route_evidence import approved

        if not approved(policy.configuration, current.probe, mapping):
            raise HTTPException(
                409,
                "This download client's folder is not verified for the library. "
                "Check its folder mapping in Settings → Download clients.",
            )
    return snapshot


async def require_selection(db, selection):
    approval = consent(selection)
    if not approval:
        return
    proof = selection.frozen["automatic_selection"]
    from app.domain.list_policies import require_authority

    await require_authority(
        db, selection.owner_id, proof.get("list_authority"), intent_id=selection.intent_id
    )
    from app.domain.series_acquisition import require_authority as require_series

    await require_series(
        db, selection.owner_id, proof.get("series_authority"), intent_id=selection.intent_id
    )
    pinned = (proof.get("series_authority") or {}).get("pack_origin")
    if (
        pinned
        and not selection.frozen.get("download_recovery")
        and str(selection.artifact_id) != pinned["artifact_id"]
    ):
        raise HTTPException(409, "Additional pack books must use the originally selected torrent")
    operation = await db.get(Operation, UUID(proof["operation_id"]), populate_existing=True)
    if (
        not operation
        or operation.kind != "acquisition.auto-select"
        or operation.owner_id != selection.owner_id
        or operation.status not in {"queued", "running", "completed"}
        or not operation.payload["command"].get("download_when_ready")
        or operation.payload.get("dispatch_approval") != approval
        or operation.payload.get("selection_id") != str(selection.id)
        or operation.payload.get("list_authority") != proof.get("list_authority")
        or operation.payload.get("series_authority") != proof.get("series_authority")
    ):
        raise HTTPException(409, "Automatic acquisition authorization is no longer current")
    await approve_route(
        db,
        selection.owner_id,
        selection.destination_id,
        approval["destination_revision"],
        expected=approval,
        lock=True,
        mapping=selection.frozen["mapping"],
    )
    profile = ProfileSnapshot.model_validate(operation.payload["profile"])
    current = await refresh_profile(db, selection.owner_id, profile)
    if not same_profile(current, profile):
        raise HTTPException(409, "Automatic acquisition preferences changed; review this request")
    work = await canonical_work(db, UUID(selection.frozen["origin_work_id"]))
    if identity(work) != operation.payload["work"]:
        raise HTTPException(
            409, "Catalog identity changed after automatic acquisition was authorized"
        )
    if proof.get("coverage"):
        from app.domain import pack_coverage

        user = await db.get(User, selection.owner_id, populate_existing=True)
        if proof.get("pack_catalog") != await pack_coverage.catalog(db, user, work):
            raise HTTPException(409, "Series coverage changed before download; review this request")
    if selection.frozen["descriptor"]["torrent_bytes"] > proof["maximum_bytes"]:
        raise HTTPException(409, "The whole torrent exceeds this automatic acquisition limit")
