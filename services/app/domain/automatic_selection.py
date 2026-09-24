"""Bounded automatic release preparation through the shared acquisition selector."""

import asyncio
import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.nzb_descriptor import load_descriptor
from app.adapters.source_releases import release_value as parse_release
from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionTarget,
    ListCatalogBinding,
    Operation,
    ProviderObject,
    SourceArtifact,
    SourceConnection,
    SourceResult,
    User,
    Version,
    Work,
    WorkMetadataSource,
)
from app.db.session import session_factory
from app.domain import automatic_dispatch, pack_coverage
from app.domain.acquisition import evaluate
from app.domain.acquisition_selection import SelectionInput, prepare
from app.domain.automatic_eligibility import (
    AUDIO,
    EBOOKS,
    collection_candidate,
    eligibility,
    limit_bytes,
)
from app.domain.book_sources import checked
from app.domain.downloaders import client_protocol, connection_or_404
from app.domain.operations import transaction_lock
from app.domain.prowlarr_network import prowlarr_call
from app.domain.release_profiles import (
    ProfileSnapshot,
    assess_release,
    normalized,
    ranking_key,
    refresh_profile,
    same_profile,
    source_popularity,
)
from app.domain.request_constraints import constrained_preferences
from app.domain.request_scope import SCOPE_FIELDS
from app.domain.source_artifacts import persist_artifact
from app.domain.source_network import source_call
from app.domain.visibility import visible_origin_work
from app.domain.work_graph import acquisition_lock, family_ids
from app.importing.versioning import version_revision
from app.jobs.queue import enqueue
from app.jobs.retry import SourceSearchRetry
from app.security import decrypt_secrets

KIND = "acquisition.auto-select"
MAX_INSPECTIONS = 5
TERMINAL = {"completed", "held", "failed", "cancelled"}


class AlreadyAvailable(HTTPException):
    def __init__(self):
        super().__init__(
            409, "Requested media is already available; no release selection is needed"
        )


class AutomaticSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: UUID
    slot: str = Field(pattern="^(ebook|audio|either)$")
    search_id: UUID
    result_id: UUID | None = None
    downloader_id: UUID
    downloader_generation: int = Field(ge=1)
    destination_id: UUID
    destination_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    alternate_downloader_id: UUID | None = None
    alternate_downloader_generation: int | None = Field(default=None, ge=1)
    alternate_destination_id: UUID | None = None
    alternate_destination_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    download_when_ready: bool = False
    use_wedge: bool = False

    @model_validator(mode="after")
    def fallback_route(self):
        parts = (
            self.alternate_downloader_id,
            self.alternate_downloader_generation,
            self.alternate_destination_id,
            self.alternate_destination_revision,
        )
        if any(part is not None for part in parts) and not all(part is not None for part in parts):
            raise ValueError("A fallback downloader needs its revision and import route")
        if self.alternate_downloader_id and self.alternate_downloader_id == self.downloader_id:
            raise ValueError("Choose a different client for the other download protocol")
        return self


def release_value(row):
    return parse_release(row.source_key, row.release_snapshot)


def protocol_rejection(protocol):
    if protocol == "nzb":
        return (
            "No ready Usenet download route. Check SABnzbd or NZBGet, its download folder, "
            "and the import destination in Settings."
        )
    if protocol == "soulseek":
        return "Connect and test Soulseek before downloading this folder"
    return (
        "No ready torrent download route. Check your torrent client, its download folder, "
        "and the import destination in Settings."
    )


async def accept_route(db, operation, result_id, route, protocol):
    if route is None:
        await reject_candidate(db, operation, result_id, [protocol_rejection(protocol)])
        return False
    if route_ready(route):
        return True
    if route["approval_key"] == "dispatch_approval":
        finish(operation, "held", "The download client for this release is not ready")
        return False
    await reject_candidate(
        db, operation, result_id, ["The download client for this release is not ready"]
    )
    return False


def route_ready(route):
    downloader = route["downloader"]
    if downloader is None:
        return False
    return bool(
        downloader.enabled
        and downloader.status == "connected"
        and downloader.credential_generation == route["generation"]
    )


def wanted_protocol(protocol):
    if protocol == "nzb":
        return "nzb"
    if protocol == "soulseek":
        return "soulseek"
    return "torrent"


async def matching_route(db, body, protocol):
    """Return the saved client whose protocol matches this release."""
    if protocol == "soulseek":
        from app.domain.slskd_connection import integration as soulseek_integration

        downloader = await soulseek_integration(db)
        if not downloader:
            return None
        return {
            "downloader": downloader,
            "generation": downloader.credential_generation,
            "destination_id": body.destination_id,
            "destination_revision": body.destination_revision,
            "approval_key": "dispatch_approval",
        }
    wanted = wanted_protocol(protocol)
    try:
        primary = await connection_or_404(db, body.downloader_id)
    except HTTPException:
        primary = None
    if primary and client_protocol(primary.kind) == wanted:
        return {
            "downloader": primary,
            "generation": body.downloader_generation,
            "destination_id": body.destination_id,
            "destination_revision": body.destination_revision,
            "approval_key": "dispatch_approval",
        }
    alternate = None
    if body.alternate_downloader_id:
        try:
            alternate = await connection_or_404(db, body.alternate_downloader_id)
        except HTTPException:
            alternate = None
    if alternate and client_protocol(alternate.kind) == wanted:
        return {
            "downloader": alternate,
            "generation": body.alternate_downloader_generation,
            "destination_id": body.alternate_destination_id,
            "destination_revision": body.alternate_destination_revision,
            "approval_key": "alternate_dispatch_approval",
        }
    if primary is None and alternate is None:
        return {
            "downloader": None,
            "generation": body.downloader_generation,
            "destination_id": body.destination_id,
            "destination_revision": body.destination_revision,
            "approval_key": "dispatch_approval",
        }
    return None


def verify_version_snapshot(payload, version):
    if version and "version_identity_revision" not in payload:
        raise HTTPException(409, "Version identity was not frozen; start a fresh selection")
    current = version_revision(version) if version else None
    if payload.get("version_identity_revision") != current:
        raise HTTPException(409, "Catalog edition or recording changed; start a fresh selection")


async def context(db, user_id, body, *, recovery_selection_id=None):
    user = await db.get(User, user_id, populate_existing=True)
    if not user or not user.active or user.role == "viewer":
        raise HTTPException(403, "Request account access changed")
    intent = await db.get(AcquisitionIntent, body.intent_id, populate_existing=True)
    if not intent or intent.owner_id != user_id:
        raise HTTPException(404, "Request not found")
    work = await acquisition_lock(db, intent.work_id)
    await evaluate(db, user, intent)
    await db.flush()
    target = await db.scalar(
        select(AcquisitionTarget).where(
            AcquisitionTarget.intent_id == intent.id,
            AcquisitionTarget.slot == body.slot,
        )
    )
    if target and target.state == "satisfied":
        raise AlreadyAvailable()
    if not target or target.state != "wanted" or not target.reservation_id:
        raise HTTPException(
            409, "This target is no longer wanted; check current library availability"
        )
    reservation = await db.get(AcquisitionReservation, target.reservation_id)
    if reservation.state != "planned":
        raise HTTPException(409, "A compatible request already has an acquisition in progress")
    search, changed = await checked(db, body.search_id, user_id)
    if changed or UUID(search.payload["work"]["id"]) != work.id:
        raise HTTPException(409, "The source search no longer matches this request")
    if search.status != "completed":
        raise HTTPException(409, "Wait for the source search to finish")
    if datetime.fromisoformat(search.payload["expires_at"]) <= datetime.now(UTC):
        raise HTTPException(409, "Source results expired; refresh the source search")
    # General book searches can serve strict scope targets; candidate/selection
    # checks still enforce their language, recording and library requirements.
    # Custom release ranking/limits require the request-bound search snapshot.
    bound_request = search.payload.get("command", {}).get("request_id")
    if (
        intent.release_policy
        and any(
            set(intent.release_policy.get(key) or {}) - set(SCOPE_FIELDS.values())
            for key in ("list_overrides", "request_overrides")
        )
        and bound_request != str(intent.id)
    ):
        raise HTTPException(409, "Refresh sources for this request’s preferences")
    if bound_request and bound_request != str(intent.id):
        raise HTTPException(409, "Source search belongs to a different request")
    profile = ProfileSnapshot.model_validate(search.payload["profile"])
    if not bound_request and intent.release_policy and intent.release_policy.get("id"):
        requested = ProfileSnapshot.model_validate(intent.release_policy)
        scope_fields = set(SCOPE_FIELDS.values())
        if (requested.id, requested.generation) != (profile.id, profile.generation) or (
            requested.preferences.model_dump(exclude=scope_fields)
            != profile.preferences.model_dump(exclude=scope_fields)
        ):
            raise HTTPException(409, "Refresh sources for this request’s preferences")
    current = await refresh_profile(db, user_id, profile)
    if not same_profile(current, profile):
        raise HTTPException(409, "Download preferences changed; refresh the source search")
    # A matched source/provider identity is required; manually typed titles alone
    # remain usable in the reviewed flow rather than silently acquiring namesakes.
    anchor = await db.scalar(
        select(WorkMetadataSource.id)
        .join(Work)
        .where(
            visible_origin_work(user),
            WorkMetadataSource.work_id.in_(family_ids(work.id)),
            WorkMetadataSource.accepted.is_(True),
        )
        .limit(1)
    )
    if not anchor:
        bindings = await db.scalars(
            select(ListCatalogBinding).where(
                ListCatalogBinding.owner_id == user_id,
                ListCatalogBinding.work_id.in_(family_ids(work.id)),
            )
        )
        anchor = any(
            b.identity_key.startswith(("hardcover:", "goodreads:"))
            and normalized(b.assertion.get("title", "")) == normalized(work.title)
            and {normalized(a) for a in b.assertion.get("authors", [])}.intersection(
                normalized(a) for a in work.authors
            )
            for b in bindings
        )
    if not anchor:
        raise HTTPException(
            409, "Match this title to a catalog provider before automatic selection"
        )
    rule = dict(reservation.requirements)
    version = await db.get(Version, UUID(rule["version_id"])) if rule["version_id"] else None
    if version and await db.scalar(
        select(ProviderObject.id)
        .where(
            ProviderObject.version_id == version.id, ProviderObject.match_status == "needs-review"
        )
        .limit(1)
    ):
        raise HTTPException(
            409, "Resolve this catalog version's metadata conflict before automatic selection"
        )
    if recovery_selection_id:
        from app.domain.download_recovery import frozen_context

        _, rule, _ = await frozen_context(db, recovery_selection_id, rule, profile)
    return user, work, search, profile, rule, version


async def begin(
    db, user, body, key, *, list_authority=None, series_authority=None, recovery_selection_id=None
):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Automatic selection is paused for recovery")
    from app.domain.list_policies import require_authority

    await require_authority(db, user.id, list_authority, intent_id=body.intent_id)
    from app.domain.series_acquisition import require_authority as require_series

    await require_series(db, user.id, series_authority, intent_id=body.intent_id)
    await transaction_lock(db, f"operation:{user.id}:{key}")
    command = body.model_dump(mode="json", exclude_none=True)
    if body.result_id is None:
        command.pop("result_id", None)
    if not body.download_when_ready:
        command.pop("download_when_ready")  # Preserve earlier preparation-only command receipts.
    if not body.use_wedge:
        command.pop("use_wedge")
    previous = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if previous:
        if (
            previous.kind != KIND
            or previous.payload["command"] != command
            or (previous.payload.get("list_authority") != list_authority)
            or (previous.payload.get("series_authority") != series_authority)
        ):
            raise HTTPException(409, "This command key was already used for another selection")
        return previous
    _, work, search, profile, rule, version = await context(
        db, user.id, body, recovery_selection_id=recovery_selection_id
    )
    approval = (
        await automatic_dispatch.approve_route(
            db, user.id, body.destination_id, body.destination_revision
        )
        if body.download_when_ready
        else None
    )
    alternate_approval = (
        await automatic_dispatch.approve_route(
            db, user.id, body.alternate_destination_id, body.alternate_destination_revision
        )
        if body.download_when_ready and body.alternate_destination_id
        else None
    )
    active = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == KIND,
            Operation.payload["command"]["intent_id"].astext == str(body.intent_id),
            Operation.payload["command"]["slot"].astext == body.slot,
            Operation.status.in_(["queued", "running"]),
        )
        .order_by(Operation.created_at.desc())
        .limit(1)
    )
    if active:
        await repair(db, active)
        if active.status in {"queued", "running"}:
            if active.payload["command"] != command:
                raise HTTPException(409, "Cancel the active selection before changing its options")
            raise HTTPException(
                409, "A selection is already running for this target; open its current status"
            )
    pack_scope = None
    if (
        body.download_when_ready
        and not series_authority
        and profile.preferences.effective_series_scope == "prefer_packs"
    ):
        from app.domain.list_series import plan

        pack_scope = await plan(db, user, work.id)
    operation = Operation(
        owner_id=user.id,
        kind=KIND,
        idempotency_key=key,
        message="Waiting to assess source candidates",
        payload={
            "command": command,
            **(
                {"recovery_selection_id": str(recovery_selection_id)}
                if recovery_selection_id
                else {}
            ),
            "work": deepcopy(search.payload["work"]),
            "profile": profile.model_dump(mode="json"),
            "requirements": rule,
            "version_identity_revision": version_revision(version) if version else None,
            "pack_catalog": await pack_coverage.catalog(db, user, work)
            if profile.preferences.allows_series_packs
            else None,
            "maximum_bytes": limit_bytes(
                constrained_preferences(profile.preferences, rule), rule["medium"]
            ),
            "maximum_pack_bytes": limit_bytes(
                constrained_preferences(profile.preferences, rule), rule["medium"], pack=True
            )
            if profile.preferences.allows_series_packs
            else None,
            "inspected": [],
            "verified": {},
            "decisions": [],
            "selection_id": None,
            "token": None,
            "dispatch_approval": approval,
            **(
                {"alternate_dispatch_approval": alternate_approval}
                if alternate_approval is not None
                else {}
            ),
            "download_id": None,
            "list_authority": list_authority,
            "series_authority": series_authority,
            **({"pack_scope": pack_scope} if pack_scope else {}),
        },
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))
    return operation


async def owned(db, user, identifier):
    await transaction_lock(db, f"auto-select:{identifier}")
    operation = await db.get(Operation, identifier, populate_existing=True)
    if not operation or operation.owner_id != user.id or operation.kind != KIND:
        raise HTTPException(404, "Automatic selection not found")
    await repair(db, operation)
    return operation


async def repair(db, operation):
    if operation.status in {"queued", "running"}:
        status = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        if status not in {"todo", "doing"}:
            operation.status, operation.message = (
                "failed",
                "Pack coordination stopped; cancel this preparation before retrying"
                if operation.payload.get("pack_dispatch", {}).get("state") == "waiting"
                else "Selection worker stopped; start a new selection from fresh results",
            )


async def candidates(db, operation, work, profile, rule, version):
    search_id = UUID(operation.payload["command"]["search_id"])
    rows = list(
        await db.scalars(
            select(SourceResult).where(
                SourceResult.operation_id == search_id,
                SourceResult.owner_id == operation.owner_id,
            )
        )
    )
    sources = {s.key: s for s in await db.scalars(select(SourceConnection))}
    ranked = []
    for row in rows:
        pinned = operation.payload["command"].get("result_id")
        if pinned and str(row.id) != pinned:
            continue
        release = release_value(row)
        verified = operation.payload.get("verified", {}).get(str(row.id))
        if verified:
            release = type(release).model_validate(verified["release"])
        from app.domain.pack_expansion import matches_source, pinned_source

        problems = eligibility(
            release,
            operation.payload["work"],
            rule,
            profile.preferences,
            version=version,
            unattended=operation.payload["command"].get("download_when_ready", False),
            catalog=operation.payload.get("pack_catalog"),
        )
        if not operation.payload.get("recovery_selection_id") and not matches_source(
            pinned_source(operation.payload.get("series_authority")), row, release
        ):
            problems.append("Additional pack books must use their originally selected torrent")
        from app.domain import release_blocklist

        if await release_blocklist.blocked(db, work.id, rule["medium"], release):
            problems.append("This release is blocklisted for this book and medium")
        root_id = operation.payload.get("recovery_selection_id")
        if root_id:
            from app.domain.download_recovery import frozen_context

            _, original_rule, original_profile = await frozen_context(db, root_id, rule, profile)
            problems.extend(
                eligibility(
                    release,
                    operation.payload["work"],
                    original_rule,
                    original_profile.preferences,
                    version=version,
                    unattended=True,
                    catalog=operation.payload.get("pack_catalog"),
                )
            )
        source = sources.get(row.source_key)
        if (
            not source
            or not source.enabled
            or source.generation != row.source_generation
            or row.expires_at <= datetime.now(UTC)
        ):
            problems.append("Source connection changed or this observation expired")
        ranked_release = (
            release.model_copy(
                update={
                    "formats": verified.get("target_formats", verified["formats"]),
                    **({"narrators": []} if verified.get("coverage") else {}),
                }
            )
            if verified
            else release
        )
        assessment = assess_release(
            ranked_release, operation.payload["work"], profile.preferences, rule["medium"]
        )
        is_pack = collection_candidate(
            release, operation.payload["work"], operation.payload.get("pack_catalog")
        )
        if is_pack and not problems:
            assessment = assessment.model_copy(update={"identity": "corroborated"})
        rank = ranking_key(ranked_release, assessment, profile.preferences)
        ranked.append(
            (
                (0 if is_pack and profile.preferences.allows_series_packs else 1, *rank),
                row,
                release,
                problems,
            )
        )
    ranked.sort(key=lambda value: value[0])
    return ranked


async def resolve_candidate(
    owner_id, row, *, downloader_id=None, downloader_generation=None, use_wedge=False
):
    if row.source_key == "audiobookbay":
        from app.domain.audiobookbay_network import resolve_abb

        return await resolve_abb(
            owner_id,
            release_value(row),
            expected_generation=row.source_generation,
            downloader_id=downloader_id,
            downloader_generation=downloader_generation,
        )
    if row.source_key == "mam":
        source_id = row.release_snapshot["source_id"]
        artifact, generation = await source_call(
            owner_id,
            "resolve",
            {"source_id": source_id, "use_wedge": True} if use_wedge else source_id,
            with_generation=True,
            expected_generation=row.source_generation,
        )
    else:
        release = release_value(row)
        reference = decrypt_secrets(row.encrypted_reference).get("link")
        if not reference:
            label = "NZB" if getattr(release, "protocol", None) == "nzb" else "torrent"
            raise HTTPException(422, f"This result has no supported {label} file")
        artifact, generation = await prowlarr_call(
            owner_id, "resolve", (release, reference), expected_generation=row.source_generation
        )
    identifier = await persist_artifact(
        owner_id, artifact.release.source_id, artifact, generation, row.source_key
    )
    return identifier, artifact.release


def finish(operation, state, message):
    operation.status, operation.message = state, message
    operation.payload = {**operation.payload, "token": None}


async def reject_candidate(db, operation, result_id, reasons):
    payload = deepcopy(operation.payload)
    if str(result_id) not in payload["inspected"]:
        payload["inspected"].append(str(result_id))
    payload.get("verified", {}).pop(str(result_id), None)
    payload["token"] = None
    for decision in payload["decisions"]:
        if decision["result_id"] == str(result_id):
            decision["reasons"] = reasons
            decision["inspected"] = True
    operation.payload = payload
    operation.status, operation.message = (
        "queued",
        "This torrent needs review; checking the next candidate",
    )
    operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))


def eligible_candidates(ranked, payload):
    inspected = set(payload["inspected"])
    verified = payload.get("verified", {})
    return [
        item
        for item in ranked
        if not item[3]
        and (
            str(item[1].id) in verified
            or (str(item[1].id) not in inspected and len(inspected) < MAX_INSPECTIONS)
        )
    ]


async def run(identifier):
    if get_settings().recovery_mode:
        raise SourceSearchRetry(60)
    token = str(uuid4())
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"auto-select:{identifier}")
        operation = await db.get(Operation, identifier, populate_existing=True)
        if not operation or operation.kind != KIND or operation.status in TERMINAL:
            return
        if operation.payload.get("pack_dispatch", {}).get("state") == "waiting":
            return
        if operation.payload.get("token") and datetime.fromisoformat(
            operation.payload["lease_until"]
        ) > datetime.now(UTC):
            raise SourceSearchRetry(
                max(
                    1,
                    math.ceil(
                        (
                            datetime.fromisoformat(operation.payload["lease_until"])
                            - datetime.now(UTC)
                        ).total_seconds()
                    ),
                )
            )
        body = AutomaticSelectionInput.model_validate(operation.payload["command"])
        try:
            from app.domain.list_policies import require_authority

            await require_authority(
                db,
                operation.owner_id,
                operation.payload.get("list_authority"),
                intent_id=body.intent_id,
            )
            from app.domain.series_acquisition import require_authority as require_series

            await require_series(
                db,
                operation.owner_id,
                operation.payload.get("series_authority"),
                intent_id=body.intent_id,
            )
            user, work, search, profile, rule, version = await context(
                db,
                operation.owner_id,
                body,
                recovery_selection_id=operation.payload.get("recovery_selection_id"),
            )
            verify_version_snapshot(operation.payload, version)
            if body.download_when_ready:
                await automatic_dispatch.approve_route(
                    db,
                    user.id,
                    body.destination_id,
                    body.destination_revision,
                    expected=operation.payload["dispatch_approval"],
                )
                if body.alternate_destination_id:
                    await automatic_dispatch.approve_route(
                        db,
                        user.id,
                        body.alternate_destination_id,
                        body.alternate_destination_revision,
                        expected=operation.payload.get("alternate_dispatch_approval"),
                    )
            frozen_catalog = operation.payload.get("pack_catalog")
            if frozen_catalog is not None and frozen_catalog != await pack_coverage.catalog(
                db, user, work
            ):
                raise HTTPException(409, "Series coverage changed; refresh the automatic selection")
            if (
                rule != operation.payload["requirements"]
                or search.payload["work"] != operation.payload["work"]
            ):
                raise HTTPException(
                    409, "Request or catalog evidence changed; start a fresh selection"
                )
        except (HTTPException, AdapterError) as error:
            finish(
                operation,
                "completed" if isinstance(error, AlreadyAvailable) else "held",
                str(error.detail) if isinstance(error, HTTPException) else str(error),
            )
            return
        ranked = await candidates(db, operation, work, profile, rule, version)
        inspected = set(operation.payload["inspected"])
        possible = eligible_candidates(ranked, operation.payload)
        payload = deepcopy(operation.payload)
        previous_decisions = {v["result_id"]: v for v in payload["decisions"] if v.get("inspected")}
        payload["decisions"] = [
            {
                "result_id": str(row.id),
                "source": release.source,
                "title": release.raw_title,
                "reasons": list(
                    dict.fromkeys(
                        [*issues, *previous_decisions.get(str(row.id), {}).get("reasons", [])]
                    )
                ),
                "inspected": str(row.id) in inspected,
                "coverage": payload.get("verified", {}).get(str(row.id), {}).get("coverage"),
            }
            for _, row, release, issues in ranked
        ]
        operation.payload = payload
        if not possible:
            from app.domain.release_download_status import selection_feedback

            message = (
                "This release could not be downloaded. Inspect its details or refresh sources."
                if body.result_id
                else "No eligible release found within this page and inspection budget; "
                "review candidate reasons or refresh results"
            )
            finish(operation, "held", message)
            operation.message = selection_feedback(operation)[0]
            return
        row, preview = possible[0][1], possible[0][2]
        route = await matching_route(db, body, preview.protocol)
        if not await accept_route(db, operation, row.id, route, preview.protocol):
            return
        cached = payload.get("verified", {}).get(str(row.id))
        payload = {
            **payload,
            "token": token,
            "lease_until": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
        }
        operation.payload = payload
        operation.status, operation.message = (
            "running",
            "Inspecting the highest ranked eligible Soulseek folder"
            if getattr(preview, "source", None) == "slskd"
            else "Inspecting the highest ranked eligible torrent",
        )
        owner_id = operation.owner_id
        from app.domain.pack_expansion import pinned_source

        pinned = (
            None
            if operation.payload.get("recovery_selection_id")
            else pinned_source(operation.payload.get("series_authority"))
        )
    try:
        if pinned:
            artifact_id = UUID(pinned["artifact_id"])
            fresh = release_value(row)
        elif cached:
            artifact_id = UUID(cached["artifact_id"])
            fresh = type(release_value(row)).model_validate(cached["release"])
        else:
            async with asyncio.timeout(180):
                if row.source_key == "audiobookbay":
                    artifact_id, fresh = await resolve_candidate(
                        owner_id,
                        row,
                        downloader_id=route["downloader"].id,
                        downloader_generation=route["generation"],
                    )
                else:
                    artifact_id, fresh = await resolve_candidate(
                        owner_id,
                        row,
                        use_wedge=bool(
                            body.use_wedge
                            and body.result_id is not None
                            and row.id == body.result_id
                            and row.source_key == "mam"
                        ),
                    )
    except (AdapterError, HTTPException, TimeoutError) as error:
        async with session_factory()() as db, db.begin():
            await transaction_lock(db, f"auto-select:{identifier}")
            operation = await db.get(Operation, identifier, populate_existing=True)
            if operation.status in TERMINAL or operation.payload.get("token") != token:
                return
            retry = (
                isinstance(error, TimeoutError)
                or isinstance(error, AdapterError)
                and error.kind
                in {
                    FailureKind.RATE_LIMIT,
                    FailureKind.UNAVAILABLE,
                    FailureKind.TIMEOUT,
                }
            )
            rejected = (
                isinstance(error, AdapterError)
                and error.kind
                in {FailureKind.PARSER, FailureKind.UNSUPPORTED, FailureKind.NOT_FOUND}
                or isinstance(error, HTTPException)
                and error.status_code in {404, 422}
            )
            if rejected:
                await reject_candidate(
                    db,
                    operation,
                    row.id,
                    ["Release could not be inspected or is unsupported; review this source result"],
                )
                return
            finish(
                operation,
                "queued" if retry else "held",
                "Source access is temporarily unavailable"
                if retry
                else "Release inspection needs review; inspect the source result manually",
            )
        if retry:
            raise SourceSearchRetry(getattr(error, "retry_after", None) or 60) from None
        return
    soulseek_batch = None
    accepted_batch = False
    try:
        async with session_factory()() as db, db.begin():
            # Selection commands take their key before the acquisition lock. Match
            # that order even when a user races the internally generated command.
            child_key = f"auto-selected:{identifier}"
            dispatch_key = f"auto-download:{identifier}"
            if body.download_when_ready:
                await transaction_lock(db, f"operation:{owner_id}:{dispatch_key}")
            await transaction_lock(db, f"operation:{owner_id}:{child_key}")
            await transaction_lock(db, f"auto-select:{identifier}")
            operation = await db.get(Operation, identifier, populate_existing=True)
            if operation.status in TERMINAL or operation.payload.get("token") != token:
                return
            await automatic_dispatch.lock_principals(
                db,
                owner_id,
                operation.payload.get("dispatch_approval"),
                operation.payload.get("list_authority"),
                operation.payload.get("series_authority"),
            )
            try:
                await require_authority(
                    db, owner_id, operation.payload.get("list_authority"), intent_id=body.intent_id
                )
                await require_series(
                    db,
                    owner_id,
                    operation.payload.get("series_authority"),
                    intent_id=body.intent_id,
                )
                user, work, search, profile, rule, version = await context(
                    db,
                    owner_id,
                    body,
                    recovery_selection_id=operation.payload.get("recovery_selection_id"),
                )
                verify_version_snapshot(operation.payload, version)
                frozen_catalog = operation.payload.get("pack_catalog")
                if frozen_catalog is not None and frozen_catalog != await pack_coverage.catalog(
                    db, user, work
                ):
                    raise HTTPException(
                        409, "Series coverage changed; refresh the automatic selection"
                    )
                if (
                    rule != operation.payload["requirements"]
                    or search.payload["work"] != operation.payload["work"]
                ):
                    raise HTTPException(
                        409, "Request or catalog evidence changed; start a fresh selection"
                    )
                result = await db.get(SourceResult, row.id, populate_existing=True)
                source = await db.get(SourceConnection, row.source_key, populate_existing=True)
                if (
                    not result
                    or result.expires_at <= datetime.now(UTC)
                    or not source
                    or not source.enabled
                    or source.generation != row.source_generation
                ):
                    raise HTTPException(
                        409, "Source observation changed during inspection; refresh results"
                    )
                artifact = await db.get(SourceArtifact, artifact_id)
                if (
                    not artifact
                    or artifact.owner_id != owner_id
                    or artifact.source_key != row.source_key
                    or artifact.source_generation != row.source_generation
                    or artifact.source_id != fresh.source_id
                    or fresh.source_id != release_value(row).source_id
                ):
                    raise HTTPException(
                        409, "Resolved torrent does not match the selected source result"
                    )
                from app.domain.pack_expansion import pinned_source

                pinned = (
                    None
                    if operation.payload.get("recovery_selection_id")
                    else pinned_source(operation.payload.get("series_authority"))
                )
                if pinned and (
                    str(artifact.id) != pinned["artifact_id"]
                    or artifact.sha256 != pinned["artifact_sha256"]
                ):
                    await reject_candidate(
                        db,
                        operation,
                        row.id,
                        [
                            "The resolved torrent differs from the pack authorized "
                            "for these additional books"
                        ],
                    )
                    return
                descriptor = load_descriptor(artifact.descriptor)
                route = await matching_route(db, body, fresh.protocol)
                if not await accept_route(db, operation, row.id, route, fresh.protocol):
                    return
                reasons = eligibility(
                    fresh,
                    operation.payload["work"],
                    rule,
                    profile.preferences,
                    version=version,
                    descriptor=descriptor,
                    unattended=body.download_when_ready,
                    catalog=operation.payload.get("pack_catalog"),
                )
                from app.domain import release_blocklist

                if await release_blocklist.blocked(
                    db, work.id, rule["medium"], fresh, artifact.descriptor
                ):
                    reasons.append("This release or its content hash is blocklisted")
                root_id = operation.payload.get("recovery_selection_id")
                if root_id:
                    from app.domain.download_recovery import frozen_context

                    _, original_rule, original_profile = await frozen_context(
                        db, root_id, rule, profile
                    )
                    reasons.extend(
                        eligibility(
                            fresh,
                            operation.payload["work"],
                            original_rule,
                            original_profile.preferences,
                            version=version,
                            descriptor=descriptor,
                            unattended=True,
                            catalog=operation.payload.get("pack_catalog"),
                        )
                    )
                # Existing artifact snapshots are immutable. Metadata changes require
                # review; fluctuating counts/timestamps cannot change book identity.
                excluded = {
                    "observed_at",
                    "seeders",
                    "leechers",
                    "snatches",
                    "uploaded_at",
                    "description",
                    "media_info",
                }
                if release_value(artifact).model_dump(exclude=excluded) != fresh.model_dump(
                    exclude=excluded
                ):
                    reasons.append(
                        "Source metadata differs from the saved artifact; review this release"
                    )
                if reasons:
                    await reject_candidate(db, operation, row.id, reasons)
                    return
                coverage = (
                    pack_coverage.manifest(
                        fresh,
                        operation.payload["work"],
                        operation.payload.get("pack_catalog"),
                        descriptor,
                        rule["medium"],
                    )
                    if collection_candidate(
                        fresh, operation.payload["work"], operation.payload.get("pack_catalog")
                    )
                    else None
                )
                payload = deepcopy(operation.payload)
                if str(row.id) not in payload["inspected"]:
                    payload["inspected"].append(str(row.id))
                payload["token"] = None
                primary_formats = EBOOKS if rule["medium"] == "ebook" else AUDIO
                payload.setdefault("verified", {})[str(row.id)] = {
                    "artifact_id": str(artifact.id),
                    "coverage": coverage,
                    **(
                        {"target_formats": pack_coverage.target_formats(coverage)}
                        if coverage
                        else {}
                    ),
                    "release": fresh.model_dump(mode="json"),
                    "formats": sorted(
                        {PurePosixPath(f.path).suffix.lower().lstrip(".") for f in descriptor.files}
                        & primary_formats
                    ),
                }
                for decision in payload["decisions"]:
                    if decision["result_id"] == str(row.id):
                        decision["reasons"] = []
                        decision["inspected"] = True
                        decision["coverage"] = coverage
                operation.payload = payload
                remaining = eligible_candidates(
                    await candidates(db, operation, work, profile, rule, version), payload
                )
                if remaining and remaining[0][1].id != row.id:
                    operation.status, operation.message = (
                        "queued",
                        "Inspected release evidence changed the ranking; "
                        "checking the next candidate",
                    )
                    operation.job_id = await enqueue(db, KIND, operation_id=str(identifier))
                    return
                maximum = limit_bytes(
                    constrained_preferences(profile.preferences, rule),
                    rule["medium"],
                    pack=bool(coverage),
                )
                operation.payload = {**operation.payload, "maximum_bytes": maximum}
                if (
                    body.download_when_ready
                    and not coverage
                    and not operation.payload.get("recovery_selection_id")
                    and getattr(fresh, "source", None) == "slskd"
                ):
                    from app.domain.request_quotas import reserve_size
                    from app.domain.slskd_transfers import queue_folder

                    quota_target = await db.scalar(
                        select(AcquisitionTarget).where(
                            AcquisitionTarget.intent_id == body.intent_id,
                            AcquisitionTarget.slot == body.slot,
                        )
                    )
                    await reserve_size(
                        db, user, quota_target, rule["medium"], descriptor.content_bytes
                    )
                    batch = str(uuid4())
                    try:
                        await queue_folder(fresh, batch)
                    except AdapterError as error:
                        retry = error.kind in {
                            FailureKind.RATE_LIMIT,
                            FailureKind.UNAVAILABLE,
                            FailureKind.TIMEOUT,
                            FailureKind.UNCERTAIN,
                        }
                        next_folder = error.kind == FailureKind.NOT_FOUND or (
                            error.kind == FailureKind.UNSUPPORTED
                            and not str(error).startswith("Connect and test")
                        )
                        if next_folder:
                            await reject_candidate(db, operation, row.id, [str(error)])
                            return
                        finish(operation, "queued" if retry else "held", str(error))
                        if retry:
                            raise SourceSearchRetry(
                                getattr(error, "retry_after", None) or 60
                            ) from error
                        return
                    soulseek_batch = batch
                async with db.begin_nested():
                    selected = await prepare(
                        db,
                        user,
                        SelectionInput(
                            intent_id=body.intent_id,
                            search_id=body.search_id,
                            slot=body.slot,
                            artifact_id=artifact_id,
                            downloader_id=route["downloader"].id,
                            downloader_generation=route["generation"],
                            destination_id=route["destination_id"],
                            destination_revision=route["destination_revision"],
                            confirmed_work_id=work.id,
                            profile_id=profile.id,
                            profile_generation=profile.generation,
                            profile_effective_revision=profile.base_effective_revision
                            or profile.effective_revision,
                        ),
                        child_key,
                        recovery_selection_id=operation.payload.get("recovery_selection_id"),
                        automatic_evidence={
                            "operation_id": str(identifier),
                            "search_id": str(body.search_id),
                            "result_id": str(row.id),
                            "maximum_bytes": operation.payload["maximum_bytes"],
                            "inspections": len(payload["inspected"]),
                            "reported_seeders": fresh.seeders,
                            **(
                                {
                                    "source_popularity": {
                                        "origin": fresh.source
                                        + (":" + fresh.indexer_id if fresh.indexer_id else ""),
                                        "metric": "completed_downloads"
                                        if fresh.source == "mam"
                                        else None,
                                        "value": source_popularity(fresh),
                                    }
                                }
                                if "popularity" in profile.preferences.criteria
                                else {}
                            ),
                            "source_observed_at": fresh.observed_at.isoformat(),
                            "inspected_formats": payload["verified"][str(row.id)]["formats"],
                            "coverage": coverage,
                            "pack_catalog": operation.payload.get("pack_catalog")
                            if coverage
                            else None,
                            "scope": (
                                "Catalog and manifest corroborate a bounded series pack; "
                                "only requested books are authorized for import"
                            )
                            if coverage
                            else (
                                "Fetched source page; single-book manifest; "
                                "actual file identity checked after downloading"
                            ),
                            "dispatch_approval": operation.payload.get(route["approval_key"]),
                            "list_authority": operation.payload.get("list_authority"),
                            "series_authority": operation.payload.get("series_authority"),
                        },
                    )
                    operation.payload = {**operation.payload, "selection_id": str(selected.id)}
                    if body.download_when_ready and coverage:
                        from app.domain.automatic_packs import defer
                        from app.domain.pack_expansion import create

                        await create(db, user, operation, selected, coverage)
                        await defer(db, operation, selected)
                    elif body.download_when_ready:
                        from app.domain.download_attempts import start as start_download

                        attempt = await start_download(
                            db,
                            user,
                            selected.id,
                            dispatch_key,
                            automatic=True,
                            attempt_id=UUID(soulseek_batch) if soulseek_batch else None,
                            already_queued=bool(soulseek_batch),
                        )
                        if soulseek_batch:
                            accepted_batch = True
                        operation.payload = {**operation.payload, "download_id": str(attempt.id)}
                if body.download_when_ready and coverage:
                    return
                finish(
                    operation,
                    "completed",
                    "Eligible release selected; automatic download queued"
                    if body.download_when_ready
                    else "Best eligible release prepared; download has not started",
                )
            except (HTTPException, AdapterError) as error:
                # A failed dispatch rolls the nested selection/attempt transaction
                # back together, including any in-memory operation payload changes.
                await db.refresh(operation)
                finish(
                    operation,
                    "completed" if isinstance(error, AlreadyAvailable) else "held",
                    str(error.detail) if isinstance(error, HTTPException) else str(error),
                )
                from app.domain.request_quotas import hold_selection

                await hold_selection(db, operation, error)
    finally:
        if soulseek_batch and not accepted_batch:
            from app.domain.slskd_transfers import cancel_folder

            await cancel_folder(fresh.username, soulseek_batch)
