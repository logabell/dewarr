"""Finite extra-book requests derived from one authorized selected torrent."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionSelection,
    AcquisitionTarget,
    ListAcquisitionBook,
    ListAcquisitionPolicy,
    Operation,
    SourceArtifact,
)
from app.domain import download_memberships, series_acquisition
from app.domain.acquisition import RequestSpec
from app.domain.automatic_routes import AutomaticRoutes
from app.domain.release_profiles import ProfileSnapshot
from app.importing.naming import fingerprint
from app.jobs.queue import enqueue


async def removed(db, saved):
    if not saved:
        return False
    operation = await db.get(Operation, UUID(saved["operation_id"]), populate_existing=True)
    selection = await db.get(
        AcquisitionSelection, UUID(saved["selection_id"]), populate_existing=True
    )
    return bool(
        not operation
        or operation.kind != "acquisition.auto-select"
        or operation.status in {"held", "failed", "cancelled"}
        or operation.payload.get("selection_id") != saved["selection_id"]
        or not selection
        # Finishing the root does not withdraw consent for its covered siblings.
        or (
            selection.state not in {"prepared", "committed", "fulfilled"}
            and not await failed_pack_member(db, selection)
        )
        or str(selection.intent_id) != saved["root_intent_id"]
        or str(selection.artifact_id) != saved["artifact_id"]
        or selection.frozen["artifact_sha256"] != saved["artifact_sha256"]
        or not await db.scalar(
            select(AcquisitionReason.id)
            .where(
                AcquisitionReason.intent_id == selection.intent_id,
                AcquisitionReason.active.is_(True),
            )
            .limit(1)
        )
    )


async def require_origin(db, owner_id, saved, *, publication=False):
    if not saved:
        return
    if await removed(db, saved):
        raise HTTPException(409, "The selected pack's originating request was withdrawn")
    selection = await db.get(AcquisitionSelection, UUID(saved["selection_id"]))
    if selection.owner_id != owner_id:
        raise HTTPException(409, "The selected pack belongs to another account")
    attempt = await download_memberships.attempt_for(db, selection.id)
    submitted = bool(attempt and attempt.external_may_exist)
    if not submitted:
        target = await db.get(AcquisitionTarget, selection.target_id, populate_existing=True)
        if target.state != "wanted":
            raise HTTPException(409, "The original book no longer needs this pack")
    if not publication:
        from app.domain.automatic_dispatch import require_selection

        await require_selection(db, selection)


def pinned_source(authority):
    return (authority or {}).get("pack_origin")


def matches_source(saved, row, release):
    return not saved or (
        row.source_key == saved["source_key"]
        and row.source_generation == saved["source_generation"]
        and release.source_id == saved["source_id"]
    )


async def create(db, user, operation, selection, coverage):
    """Create only covered reviewed children; root action supplies acquisition consent."""
    if operation.payload.get("pack_expansion"):
        return
    profile = ProfileSnapshot.model_validate(operation.payload["profile"])
    if (
        not operation.payload["command"].get("download_when_ready")
        or profile.preferences.effective_series_scope != "prefer_packs"
        or operation.payload.get("series_authority")
        or operation.payload.get("recovery_selection_id")
    ):
        return
    planned = operation.payload.get("pack_scope") or {
        "state": "review",
        "message": "Review the main-series books before expanding this pack",
    }
    outcome = {
        "state": "review",
        "message": planned["message"],
        "external_id": planned.get("external_id"),
    }
    if planned["state"] != "ready" or planned["series_id"] != coverage["series_id"]:
        operation.payload = {**operation.payload, "pack_expansion": outcome}
        return
    root_id = str(selection.frozen["work_id"])
    covered = {r["work"]["id"] for r in coverage["members"]} - {root_id}
    records = [r for r in planned["records"] if r["work_id"] in covered]
    if not records:
        operation.payload = {
            **operation.payload,
            "pack_expansion": {
                **outcome,
                "state": "empty",
                "message": "No additional reviewed main books in this pack",
            },
        }
        return
    from app.domain.permissions import automation_allowed

    if not automation_allowed(user):
        operation.payload = {
            **operation.payload,
            "pack_expansion": {
                **outcome,
                "message": "Series expansion needs this account's automation permission",
            },
        }
        return
    intent = await db.get(AcquisitionIntent, selection.intent_id)
    spec = RequestSpec.model_validate(intent.specification)
    medium = selection.frozen["requirements"]["medium"]
    values = spec.model_dump(mode="json")
    values.update(mode=medium, preferred_medium=None, ebook_version_id=None, audio_version_id=None)
    values[("audio" if medium == "ebook" else "ebook") + "_library_id"] = None
    if medium == "ebook":
        values.update(abridged=None, required_narrators=[])
    command = operation.payload["command"]
    config = await series_acquisition.configuration(
        db,
        user,
        RequestSpec.model_validate(values),
        profile,
        AutomaticRoutes.model_validate(
            {
                "downloader_id": command["downloader_id"],
                "downloader_generation": command["downloader_generation"],
                "routes": {
                    medium: {
                        "destination_id": command["destination_id"],
                        "destination_revision": command["destination_revision"],
                    }
                },
            }
        ),
    )
    list_origin = None
    if authority := operation.payload.get("list_authority"):
        policy = await db.get(ListAcquisitionPolicy, UUID(authority["policy_id"]))
        book = await db.get(ListAcquisitionBook, UUID(authority["book_id"]))
        list_origin = {
            "authority": authority,
            "activation": book.progress["activation"],
            "root_intent_id": str(selection.intent_id),
            "configuration_revision": fingerprint(policy.configuration),
        }
    artifact = await db.get(SourceArtifact, selection.artifact_id)
    now = datetime.now(UTC)
    origin = {
        "operation_id": str(operation.id),
        "selection_id": str(selection.id),
        "root_intent_id": str(selection.intent_id),
        "artifact_id": str(selection.artifact_id),
        "artifact_sha256": selection.frozen["artifact_sha256"],
        "source_key": artifact.source_key,
        "source_generation": selection.frozen["source_generation"],
        "source_id": selection.frozen["release"]["source_id"],
    }
    parent = Operation(
        owner_id=user.id,
        kind="series.requests",
        status="queued",
        idempotency_key=f"pack-extra-books:{operation.id}",
        message="Saving additional reviewed books covered by the selected pack",
        payload={
            "command": {
                "external_id": planned["external_id"],
                "work_ids": sorted(r["work_id"] for r in records),
                "scope": "selected",
                "confirm_main_membership": False,
                "expected_generation": planned["catalog_generation"],
                "specification": config["specification"],
            },
            "series": {
                "id": planned["series_id"],
                "name": planned["series_name"],
                "external_id": planned["external_id"],
                "generation": planned["catalog_generation"],
                "fetched_at": planned["fetched_at"],
            },
            "records": deepcopy(records),
            "omitted": deepcopy(planned["omitted"]),
            "scope_review": planned["scope_review"],
            "main_membership": "user-confirmed",
            "effective_specification": config["specification"],
            "release_policy": config["profile"],
            "automatic_configuration": config,
            "pack_origin": origin,
            **({"list_origin": list_origin} if list_origin else {}),
            "accepted_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        },
    )
    db.add(parent)
    await db.flush()
    parent.job_id = await enqueue(db, "series.requests", operation_id=str(parent.id))
    operation.payload = {
        **operation.payload,
        "pack_expansion": {
            "state": "accepted",
            "message": f"Acquiring {len(records)} additional reviewed books from this pack",
            "external_id": planned["external_id"],
            "request_id": str(parent.id),
            "work_ids": [r["work_id"] for r in records],
        },
    }


async def failed_pack_member(db, selection):
    from app.db.models import DownloadRecovery

    return bool(
        await db.scalar(
            select(DownloadRecovery.id).where(DownloadRecovery.selection_id == selection.id)
        )
    )
