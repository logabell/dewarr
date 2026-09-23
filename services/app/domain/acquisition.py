"""Shared request requirements, inventory checks and pre-dispatch reservations.

This module performs no downloader mutations. Source selection and dispatch consume
these persisted requirements after their integration/import gates are implemented.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)
from sqlalchemy import and_, func, or_, select

from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AssetContains,
    AuditEvent,
    BookList,
    Integration,
    Library,
    LibraryAsset,
    ListEntry,
    Operation,
    ProviderObject,
    Version,
    Work,
    WorkMetadataSource,
)
from app.domain import narrators
from app.domain.availability import owned_coverage
from app.domain.corrections import revision
from app.domain.narrators import NarratorNames
from app.domain.operations import transaction_lock
from app.domain.request_constraints import DownloadConstraints, combine, formats_possible
from app.domain.request_scope import language, same_command, sparse_schema
from app.domain.visibility import visible_library, visible_origin_work, visible_work
from app.domain.work_graph import acquisition_lock, canonical_map, canonical_work, family_ids
from app.jobs.queue import enqueue

Medium = Literal["ebook", "audio"]


def language_accepts(required, observed):
    required, observed = language(required), language(observed)
    return not required or bool(
        observed and (observed == required or observed.startswith(required + "-"))
    )


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["ebook", "audio", "both", "either"]
    preferred_medium: Medium | None = None
    language: str | None = Field(
        default=None, pattern=r"^[a-zA-Z]{2,3}([-_][a-zA-Z0-9]{2,8})*$", max_length=20
    )
    ebook_version_id: UUID | None = None
    audio_version_id: UUID | None = None
    ebook_library_id: UUID | None = None
    audio_library_id: UUID | None = None
    abridged: bool | None = None
    required_narrators: NarratorNames = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    standalone: bool = False
    download_constraints: DownloadConstraints | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("download_constraints")
    @classmethod
    def meaningful_constraints(cls, value):
        return value if value and value.active else None

    @field_validator("language")
    @classmethod
    def canonical_language(cls, value):
        return language(value)

    @model_validator(mode="after")
    def applicable_constraints(self):
        if self.download_constraints:
            media = [self.mode] if self.mode in {"ebook", "audio"} else ["ebook", "audio"]
            if any(
                not formats_possible(self.download_constraints.model_dump(), medium)
                for medium in media
            ):
                raise ValueError("Allow at least one format for each requested medium")
        if self.mode == "either" and not self.preferred_medium:
            raise ValueError("Choose which medium to search first when neither is available")
        if self.mode != "either" and self.preferred_medium:
            raise ValueError("A first-medium preference only applies to Either")
        if self.mode == "ebook" and (
            self.audio_version_id
            or self.audio_library_id
            or self.abridged is not None
            or self.required_narrators
        ):
            raise ValueError("Audiobook constraints do not apply to an ebook-only request")
        if self.mode == "audio" and (self.ebook_version_id or self.ebook_library_id):
            raise ValueError("Ebook constraints do not apply to an audio-only request")
        return self

    def media(self, slot):
        if slot == "either":
            return [self.preferred_medium, "audio" if self.preferred_medium == "ebook" else "ebook"]
        return [slot]

    def slots(self):
        return ["ebook", "audio"] if self.mode == "both" else [self.mode]

    def rule(self, medium):
        return {
            "medium": medium,
            "language": self.language,
            "version_id": str(getattr(self, medium + "_version_id") or "") or None,
            "abridged": self.abridged if medium == "audio" else None,
            "standalone": self.standalone,
            **(
                {"required_narrators": self.required_narrators}
                if medium == "audio" and self.required_narrators
                else {}
            ),
            **(
                {"download_constraints": self.download_constraints.model_dump()}
                if self.download_constraints
                else {}
            ),
        }


class RequestOptions(RequestSpec):
    """Sparse editable choices, validated as a strict RequestSpec after inheritance."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=sparse_schema)
    mode: Literal["ebook", "audio", "both", "either"] | None = None
    required_narrators: NarratorNames = Field(default_factory=list)

    @model_validator(mode="after")
    def applicable_constraints(self):
        return self

    @model_serializer(mode="wrap")
    def sparse(self, handler):
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


class RequestReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    list_id: UUID | None = None


def intersect_rules(left, right):
    """Intersection is safe only before source selection; it never relaxes either request."""
    if left["medium"] != right["medium"]:
        return None
    result = dict(left)
    for field in ("version_id", "abridged"):
        a, b = left[field], right[field]
        if a is not None and b is not None and a != b:
            return None
        result[field] = a if a is not None else b
    a, b = left["language"], right["language"]
    if language_accepts(a, b):
        result["language"] = b
    elif language_accepts(b, a):
        result["language"] = a
    else:
        return None
    result["standalone"] = left["standalone"] or right["standalone"]
    required = narrators.combined(
        left.get("required_narrators", []), right.get("required_narrators", [])
    )
    if required:
        result["required_narrators"] = required
    constraints = combine(left.get("download_constraints"), right.get("download_constraints"))
    if constraints:
        if not formats_possible(constraints, left["medium"]):
            return None
        result["download_constraints"] = constraints
    return result


async def compatible_reservation(db, candidate, rule):
    """Known frozen files can satisfy a stricter limit without rewriting an old decision."""
    compatible = intersect_rules(candidate.requirements, rule)
    if not compatible:
        return None
    if candidate.state == "planned" or compatible == candidate.requirements:
        return compatible
    inspected_fields = {"download_constraints", "required_narrators"}
    if {k: v for k, v in compatible.items() if k not in inspected_fields} != {
        k: v for k, v in candidate.requirements.items() if k not in inspected_fields
    }:
        return None
    selection = await db.scalar(
        select(AcquisitionSelection).where(
            AcquisitionSelection.reservation_id == candidate.id,
            AcquisitionSelection.state.in_(["prepared", "committed"]),
        )
    )
    if not selection:
        return None
    if not narrators.accepts(
        compatible.get("required_narrators", []),
        candidate.requirements.get("required_narrators", []),
    ):
        # A frozen exact recording can establish a stricter reason without changing
        # the selected import contract. A tracker narrator claim alone cannot.
        version = selection.frozen.get("version")
        if not version or not narrators.accepts(
            compatible["required_narrators"], version["narrators"]
        ):
            return None
    from app.adapters.nzb_descriptor import load_descriptor
    from app.adapters.source_releases import release_value
    from app.domain.release_profiles import ProfileSnapshot, ReleasePreferences, enforce_profile
    from app.domain.request_constraints import constrained_preferences

    release = selection.frozen["release"]
    try:
        enforce_profile(
            release_value(release["source"], release),
            load_descriptor(selection.frozen["descriptor"]),
            ProfileSnapshot(preferences=constrained_preferences(ReleasePreferences(), compatible)),
        )
    except HTTPException:
        return None
    return compatible


async def validate_request(db, user, work_id, spec, reason=None, *, check_version_constraints=True):
    canonical = await canonical_work(db, work_id)
    work = await db.scalar(
        select(Work).where(
            Work.id == canonical.id,
            Work.redirect_to.is_(None),
            visible_work(user),
        )
    )
    if not work:
        raise HTTPException(404, "Book not found or merged; select its current record")
    for medium in ("ebook", "audio"):
        library_id = getattr(spec, medium + "_library_id")
        if library_id and not await db.scalar(
            select(Library.id)
            .join(Integration)
            .where(
                Library.id == library_id,
                Library.accessible.is_(True),
                Integration.enabled.is_(True),
                visible_library(user),
            )
        ):
            raise HTTPException(404, "Destination library is not accessible")
        version_id = getattr(spec, medium + "_version_id")
        if not version_id:
            continue
        version = await db.get(Version, version_id)
        # An internal UUID is not proof that this account may inspect a private recording.
        catalog = await db.scalar(
            select(ProviderObject.id)
            .join(WorkMetadataSource)
            .join(Work, Work.id == WorkMetadataSource.work_id)
            .where(
                visible_origin_work(user),
                ProviderObject.version_id == version_id,
                WorkMetadataSource.accepted.is_(True),
            )
            .limit(1)
        )
        asset = await db.scalar(
            select(LibraryAsset.id)
            .join(Library)
            .join(Integration)
            .where(
                LibraryAsset.version_id == version_id,
                Library.accessible.is_(True),
                Integration.enabled.is_(True),
                visible_library(user),
            )
            .limit(1)
        )
        if (
            not version
            or (await canonical_work(db, version.work_id)).id != work.id
            or version.medium != medium
            or not (catalog or asset)
        ):
            raise HTTPException(
                404, "Requested edition or recording is not available in this catalog"
            )
        # Reading saved history or observing an existing transfer still requires
        # access to this exact version. A later metadata correction is a content
        # conflict, not a revocation of that access.
        if not check_version_constraints:
            continue
        if (
            spec.language
            and version.language
            and not language_accepts(spec.language, version.language)
        ):
            raise HTTPException(422, "The selected version conflicts with the requested language")
        if (
            medium == "audio"
            and spec.abridged is not None
            and version.abridged is not None
            and version.abridged != spec.abridged
        ):
            raise HTTPException(
                422, "The selected recording conflicts with the abridgment requirement"
            )
        if medium == "audio" and not narrators.accepts(spec.required_narrators, version.narrators):
            raise HTTPException(
                422, "The selected recording does not confirm every required narrator"
            )
    if reason and reason.list_id:
        owned = await db.scalar(
            select(BookList.id)
            .join(ListEntry)
            .where(
                BookList.id == reason.list_id,
                BookList.owner_id == user.id,
                ListEntry.work_id.in_(family_ids(work_id)),
            )
        )
        if not owned:
            raise HTTPException(404, "Select a book from a list you own")
    return work


async def inventory_candidates(db, user, work_id):
    mapping = canonical_map()
    coverage = (
        select(func.count(func.distinct(mapping.c.work_id)))
        .select_from(AssetContains)
        .join(mapping, mapping.c.origin_id == AssetContains.work_id)
        .where(AssetContains.asset_id == LibraryAsset.id)
        .correlate(LibraryAsset)
        .scalar_subquery()
    )
    return (
        await db.execute(
            select(LibraryAsset, Version, coverage)
            .outerjoin(
                Version,
                and_(
                    LibraryAsset.version_id == Version.id, Version.work_id.in_(family_ids(work_id))
                ),
            )
            .join(Library)
            .join(Integration)
            .join(AssetContains)
            .where(
                AssetContains.work_id.in_(family_ids(work_id)),
                or_(
                    owned_coverage(),
                    LibraryAsset.containment["valid"].as_boolean().is_(False),
                ),
                Library.accessible.is_(True),
                Integration.enabled.is_(True),
                visible_library(user),
            )
            .order_by(LibraryAsset.id)
        )
    ).all()


def asset_satisfies(asset, version, count, rule):
    if asset.medium != rule["medium"]:
        return False
    if rule["version_id"] and (not version or str(version.id) != rule["version_id"]):
        return False
    if not language_accepts(rule["language"], version.language if version else None):
        return False
    if rule["abridged"] is not None and (not version or version.abridged != rule["abridged"]):
        return False
    if not narrators.accepts(
        rule.get("required_narrators", []), version.narrators if version else []
    ):
        return False
    return not rule["standalone"] or (count == 1 and not asset.containment)


async def assess(db, user, work_id, spec):
    rows = await inventory_candidates(db, user, work_id)
    outcomes = []
    for slot in spec.slots():
        media = spec.media(slot)
        matched = [
            (asset, version)
            for medium in media
            for asset, version, count in rows
            if asset.full_content and asset_satisfies(asset, version, count, spec.rule(medium))
        ]
        present = next((asset for asset, _ in matched if asset.state == "present"), None)
        if present:
            outcomes.append(
                {
                    "slot": slot,
                    "state": "satisfied",
                    "message": "Already available in your library",
                    "asset_id": present.id,
                    "medium": present.medium,
                }
            )
        elif any(
            asset.containment
            and not asset.full_content
            and asset_satisfies(asset, version, count, spec.rule(medium))
            for medium in media
            for asset, version, count in rows
        ):
            outcomes.append(
                {
                    "slot": slot,
                    "state": "awaiting-inventory",
                    "message": "Review changed collection contents before acquiring another copy",
                    "asset_id": None,
                    "medium": media[0],
                }
            )
        elif any(
            asset.state in {"stale", "missing-suspected", "scope-unavailable"}
            for asset, _ in matched
        ):
            outcomes.append(
                {
                    "slot": slot,
                    "state": "awaiting-inventory",
                    "message": "Refresh library inventory before acquiring another copy",
                    "asset_id": None,
                    "medium": media[0],
                }
            )
        elif any(
            asset.state in {"missing-confirmed", "intentionally-removed"} for asset, _ in matched
        ):
            outcomes.append(
                {
                    "slot": slot,
                    "state": "paused",
                    "message": "A previous copy is missing; decide whether to replace it",
                    "asset_id": None,
                    "medium": media[0],
                }
            )
        else:
            outcomes.append(
                {
                    "slot": slot,
                    "state": "wanted",
                    "message": "Requested media is missing",
                    "asset_id": None,
                    "medium": media[0],
                }
            )
    return outcomes


async def release_unused(db, work_id):
    await db.flush()
    reservations = (
        await db.scalars(
            select(AcquisitionReservation).where(
                AcquisitionReservation.work_id.in_(family_ids(work_id)),
                AcquisitionReservation.state.in_(["planned", "selected"]),
            )
        )
    ).all()
    for reservation in reservations:
        if reservation.state == "selected":
            selection = await db.scalar(
                select(AcquisitionSelection).where(
                    AcquisitionSelection.reservation_id == reservation.id,
                    AcquisitionSelection.state == "prepared",
                )
            )
            target = await db.get(AcquisitionTarget, selection.target_id) if selection else None
            if not target or target.state != "wanted" or target.reservation_id != reservation.id:
                if selection:
                    selection.state = "cancelled"
                    selection.message = (
                        "Request changed; release selection cancelled before downloading"
                    )
                reservation.state = "planned"
        specifications = (
            await db.scalars(
                select(AcquisitionIntent.specification)
                .join(AcquisitionTarget)
                .where(
                    AcquisitionTarget.reservation_id == reservation.id,
                    AcquisitionTarget.state == "wanted",
                )
            )
        ).all()
        if not specifications:
            reservation.state = "released"
        elif reservation.state == "planned":
            rules = [
                RequestSpec.model_validate(spec).rule(reservation.requirements["medium"])
                for spec in specifications
            ]
            merged = rules[0]
            for rule in rules[1:]:
                merged = intersect_rules(merged, rule)
                if merged is None:
                    raise HTTPException(409, "Reservation requirements need reconciliation")
            reservation.requirements = merged


async def reserve(db, user, intent, spec, slot, *, only_medium=None):
    media = [only_medium] if only_medium else spec.media(slot)
    for state in ("committed", "selected", "planned"):
        for medium in media:
            destination = getattr(spec, medium + "_library_id")
            scope = str(destination) if destination else "unconfigured:" + str(user.id)
            rule = spec.rule(medium)
            candidates = (
                await db.scalars(
                    select(AcquisitionReservation)
                    .where(
                        AcquisitionReservation.work_id.in_(family_ids(intent.work_id)),
                        AcquisitionReservation.scope == scope,
                        AcquisitionReservation.state == state,
                    )
                    .order_by(
                        (AcquisitionReservation.state == "selected").desc(),
                        AcquisitionReservation.created_at,
                        AcquisitionReservation.id,
                    )
                )
            ).all()
            for candidate in candidates:
                compatible = await compatible_reservation(db, candidate, rule)
                if not compatible:
                    continue
                if candidate.state in {"selected", "committed"}:
                    return candidate
                if compatible["version_id"]:
                    version = await db.get(Version, UUID(compatible["version_id"]))
                    if (
                        not version
                        or not narrators.accepts(
                            compatible.get("required_narrators", []), version.narrators
                        )
                        or (
                            version.language
                            and not language_accepts(compatible["language"], version.language)
                        )
                        or (
                            compatible["abridged"] is not None
                            and version.abridged is not None
                            and version.abridged != compatible["abridged"]
                        )
                    ):
                        continue
                candidate.requirements = compatible
                return candidate
    medium = media[0]
    destination = getattr(spec, medium + "_library_id")
    reservation = AcquisitionReservation(
        work_id=(await canonical_work(db, intent.work_id)).id,
        destination_id=destination,
        scope=str(destination) if destination else "unconfigured:" + str(user.id),
        requirements=spec.rule(medium),
    )
    db.add(reservation)
    await db.flush()
    return reservation


async def evaluate(db, user, intent):
    from app.domain.download_fulfillment import record_satisfaction, retire_satisfied

    spec = RequestSpec.model_validate(intent.specification)
    reasons = (
        await db.scalars(
            select(AcquisitionReason).where(
                AcquisitionReason.intent_id == intent.id,
                AcquisitionReason.active.is_(True),
            )
        )
    ).all()
    # Deleting a list or removing its member must not leave a durable acquisition reason alive.
    for reason in reasons:
        if reason.kind == "series":
            from app.domain.list_series import origin, removed

            parent = await db.get(Operation, UUID(reason.reference))
            from app.domain.pack_expansion import removed as pack_removed

            if await removed(db, origin(parent)) or await pack_removed(
                db, parent.payload.get("pack_origin") if parent else None
            ):
                reason.active = False
        if reason.kind == "list" and not await db.scalar(
            select(BookList.id)
            .join(ListEntry)
            .where(
                BookList.id == reason.list_id,
                BookList.owner_id == intent.owner_id,
                ListEntry.work_id.in_(family_ids(intent.work_id)),
            )
        ):
            reason.active = False

    def open_reason(reason, status):
        return reason.active and reason.approval_status == status

    approved = [reason for reason in reasons if open_reason(reason, "approved")]
    pending = [reason for reason in reasons if open_reason(reason, "pending")]
    declined = [reason for reason in reasons if open_reason(reason, "declined")]
    active = bool(approved)
    allowed = bool(user and user.active and user.role != "viewer")
    if allowed:
        try:
            await validate_request(db, user, intent.work_id, spec)
        except HTTPException:
            allowed = False
    outcomes = (
        await assess(db, user, intent.work_id, spec) if allowed and (active or pending) else []
    )
    targets = {
        target.slot: target
        for target in (
            await db.scalars(
                select(AcquisitionTarget).where(
                    AcquisitionTarget.intent_id == intent.id,
                )
            )
        ).all()
    }
    from app.db.models import RequestQuotaCharge
    from app.domain.request_quotas import admin_exempt, effective

    quota_rules, _ = await effective(db, user) if user else (None, None)
    if quota_rules and await admin_exempt(db, intent, quota_rules):
        for charge in await db.scalars(
            select(RequestQuotaCharge).where(
                RequestQuotaCharge.target_id.in_([target.id for target in targets.values()])
            )
        ):
            charge.exempt = True
    for slot in spec.slots():
        target = targets.get(slot)
        if not target:
            target = AcquisitionTarget(intent_id=intent.id, slot=slot, message="Evaluating request")
            db.add(target)
            await db.flush()
        target.quota_waiting, target.quota_retry_at = False, None
        previous_reservation_id = target.reservation_id
        target.reservation_id, target.satisfied_asset_id = None, None
        if not active or not allowed:
            if not allowed:
                target.state, target.message = "paused", "Request access needs attention"
            elif pending:
                target.state, target.message = "paused", "Waiting for approval"
                outcome = next(item for item in outcomes if item["slot"] == slot)
                if outcome["state"] == "wanted":
                    from app.domain.request_quotas import QuotaExceeded, admit

                    try:
                        await admit(db, user, intent, target, outcome["medium"], pending=True)
                    except QuotaExceeded as exc:
                        target.quota_waiting, target.quota_retry_at = True, exc.retry_at
                        target.message = "Waiting for quota. " + exc.detail
            elif declined:
                target.state, target.message = "cancelled", "Request declined"
            else:
                target.state, target.message = "cancelled", "No active request reasons"
            continue
        outcome = next(item for item in outcomes if item["slot"] == slot)
        target.state, target.message = outcome["state"], outcome["message"]
        target.satisfied_asset_id = outcome["asset_id"]
        if target.state != "wanted":
            await record_satisfaction(db, intent, target, previous_reservation_id)
            continue
        reservation = await reserve(db, user, intent, spec, slot)
        target.reservation_id = reservation.id
        from app.domain.request_quotas import QuotaExceeded, admit, reserve_size

        # Committed transfers are observation-only: a later quota change never cancels them.
        if reservation.state != "committed":
            try:
                await admit(db, user, intent, target, reservation.requirements["medium"])
                if target.quota_requirement:
                    await reserve_size(db, user, target, **target.quota_requirement)
                    target.quota_requirement = None
            except QuotaExceeded as exc:
                target.state, target.reservation_id = "paused", None
                target.quota_waiting, target.quota_retry_at = True, exc.retry_at
                target.message = "Waiting for quota. " + exc.detail
                continue
        else:
            selected_owner_target = await db.scalar(
                select(AcquisitionSelection.target_id)
                .where(
                    AcquisitionSelection.reservation_id == reservation.id,
                    AcquisitionSelection.state == "committed",
                )
                .limit(1)
            )
            if selected_owner_target and selected_owner_target != target.id:
                await admit(
                    db, user, intent, target, reservation.requirements["medium"], shared=True
                )
        target.message = (
            "Acquisition pending; check download activity"
            if reservation.state == "committed"
            else "Release selected; download has not started"
            if reservation.state == "selected"
            else "Saved to wanted; choose a source release to continue"
        )
    await release_unused(db, intent.work_id)
    await retire_satisfied(db, intent.work_id)


async def submit(
    db,
    user,
    work_id,
    spec,
    reason,
    key,
    *,
    policy_reference=None,
    preference_choice=None,
    frozen_preferences=None,
    expected_preference_revision=None,
    series_reference=None,
    hold_for_approval=True,
    automatic=False,
):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Request evaluation is paused for recovery")
    explicit_fields = set(spec.model_fields_set)
    original = spec
    payload = {
        "work_id": str(work_id),
        "specification": spec.model_dump(mode="json"),
        "reason": reason.model_dump(mode="json"),
        **({"scope_inheritance": 1} if isinstance(spec, RequestOptions) else {}),
    }
    if preference_choice is not None:
        payload["release_preferences"] = preference_choice.model_dump(
            mode="json", exclude_unset=True
        )
    if expected_preference_revision is not None:
        payload["expected_preference_revision"] = expected_preference_revision
    if policy_reference is not None:
        if not reason.list_id:
            raise ValueError("Policy requests require a list")
        payload["policy_reference"] = policy_reference
    if series_reference is not None:
        if reason.list_id or policy_reference:
            raise ValueError("Series reasons are independent of list reasons")
        payload["series_reference"] = str(series_reference)
    await transaction_lock(db, f"operation:{user.id}:{key}")
    await db.refresh(user)
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "Your account no longer has permission to create requests")
    existing = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if existing:
        if existing.kind != "acquisition.evaluate" or not same_command(
            existing.payload.get("command"), payload
        ):
            raise HTTPException(409, "This operation key was already used for another command")
        return await db.get(AcquisitionIntent, UUID(existing.payload["intent_id"])), existing
    if reason.list_id:
        # List edits acquire list → work locks in the same order. This also prevents
        # deleting a list while its new reason is acquiring the FK's key-share lock.
        if not await db.scalar(
            select(BookList.id)
            .where(
                BookList.id == reason.list_id,
                BookList.owner_id == user.id,
            )
            .with_for_update()
        ):
            raise HTTPException(404, "List not found")
    canonical = await acquisition_lock(db, work_id)
    work_id = canonical.id
    await db.refresh(user)
    if not user.active or user.role == "viewer":
        raise HTTPException(403, "Your account no longer has permission to create requests")
    from app.domain.request_preferences import policy_identity, resolve

    spec, profile = await resolve(
        db,
        user,
        spec,
        reason,
        preference_choice,
        frozen=frozen_preferences,
        expected=expected_preference_revision,
    )
    await validate_request(db, user, work_id, spec, reason)
    if (
        not series_reference
        and not policy_reference
        and profile.preferences.effective_series_scope == "complete_series"
    ):
        from app.domain.list_series import plan

        expansion = await plan(db, user, work_id)
        if expansion["state"] != "single":
            raise HTTPException(
                409,
                "Complete series requires the reviewed series request page; "
                "open this book's series or choose Just this book",
            )
    if series_reference is not None:
        parent = await db.get(Operation, series_reference)
        if (
            not parent
            or parent.owner_id != user.id
            or parent.kind != "series.requests"
            or parent.status not in {"queued", "running"}
            or not parent.payload.get("accepted_at")
            or str(work_id) not in parent.payload["command"]["work_ids"]
            or spec.model_dump(mode="json") != parent.payload["effective_specification"]
            or profile.model_dump(mode="json") != parent.payload["release_policy"]
        ):
            raise HTTPException(409, "Series request scope no longer authorizes this book")
    policy_key = policy_identity(profile)
    fingerprint = revision(
        {**spec.model_dump(mode="json"), **({"release_policy": policy_key} if policy_key else {})}
    )
    intent = await db.scalar(
        select(AcquisitionIntent)
        .where(
            AcquisitionIntent.owner_id == user.id,
            AcquisitionIntent.work_id == work_id,
            AcquisitionIntent.fingerprint == fingerprint,
        )
        .order_by(AcquisitionIntent.created_at, AcquisitionIntent.id)
        .limit(1)
    )
    if not intent:
        intent = AcquisitionIntent(
            owner_id=user.id,
            work_id=work_id,
            fingerprint=fingerprint,
            specification=spec.model_dump(mode="json"),
            release_policy=profile.model_dump(mode="json"),
        )
        db.add(intent)
        await db.flush()
    kind, reference = ("list", str(reason.list_id)) if reason.list_id else ("manual", "manual")
    reference = policy_reference or reference
    if series_reference is not None:
        kind, reference = "series", str(series_reference)
    record = await db.scalar(
        select(AcquisitionReason).where(
            AcquisitionReason.intent_id == intent.id,
            AcquisitionReason.kind == kind,
            AcquisitionReason.reference == reference,
        )
    )
    if not record:
        record = AcquisitionReason(
            intent_id=intent.id, kind=kind, reference=reference, list_id=reason.list_id
        )
        db.add(record)
    record.active = True
    record.release_policy = profile.model_dump(mode="json")
    if hold_for_approval:
        from app.domain.permissions import prepare_manual_approval

        prepare_manual_approval(user, spec, record, explicit_fields, original, preference_choice)
    elif record.approval_status != "approved":
        record.approval_status = "approved"
        record.decided_by = user.id
        record.decided_at = datetime.now(UTC)
        record.decision_note = None
    await db.flush()
    await evaluate(db, user, intent)
    if not (automatic or policy_reference or series_reference):
        from app.domain.request_quotas import QuotaExceeded

        held = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == intent.id, AcquisitionTarget.quota_waiting.is_(True)
            )
        )
        if held:
            raise QuotaExceeded(
                held.message.removeprefix("Waiting for quota. "), held.quota_retry_at
            )
    operation = Operation(
        owner_id=user.id,
        kind="acquisition.evaluate",
        idempotency_key=key,
        payload={"intent_id": str(intent.id), "command": payload},
        message="Waiting to recheck requested media against library inventory",
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, "acquisition.evaluate", operation_id=str(operation.id))
    db.add(AuditEvent(actor_id=user.id, action="acquisition.requested", entity_id=intent.id))
    await db.flush()
    await db.refresh(operation)
    return intent, operation


async def deactivate_list_reasons(db, user, list_id, work_id=None):
    from app.domain.list_monitoring import withdraw_membership

    await withdraw_membership(db, list_id, work_id)
    conditions = [
        AcquisitionReason.kind == "list",
        AcquisitionReason.reference.in_([str(list_id)])
        | AcquisitionReason.reference.startswith(f"policy:{list_id}:"),
        AcquisitionIntent.owner_id == user.id,
    ]
    if work_id:
        conditions.append(AcquisitionIntent.work_id.in_(family_ids(work_id)))
    intents = (
        (
            await db.scalars(
                select(AcquisitionIntent)
                .join(AcquisitionReason)
                .where(*conditions)
                .order_by(AcquisitionIntent.work_id, AcquisitionIntent.id)
            )
        )
        .unique()
        .all()
    )
    for intent in intents:
        await acquisition_lock(db, intent.work_id)
        reasons = (
            await db.scalars(
                select(AcquisitionReason).where(
                    AcquisitionReason.intent_id == intent.id,
                    AcquisitionReason.kind == "list",
                    (AcquisitionReason.reference == str(list_id))
                    | AcquisitionReason.reference.startswith(f"policy:{list_id}:"),
                )
            )
        ).all()
        for reason in reasons:
            reason.active = False
        await db.flush()
    return intents


async def withdraw_list_reasons(db, user, list_id, work_id=None):
    for intent in await deactivate_list_reasons(db, user, list_id, work_id):
        await evaluate(db, user, intent)


async def reconcile_requests():
    """Repair persisted targets after inventory, permissions or catalog evidence change.

    Use bounded keyset pages and one short transaction per intent. A retry may
    revisit records safely; no external side effect is performed by this sweep.
    New requests evaluate on submission, including those inserted behind our cursor.
    """
    from app.db.models import User
    from app.db.session import session_factory

    if get_settings().recovery_mode:
        return
    factory = session_factory()
    async with factory() as db:
        ceiling = await db.scalar(
            select(AcquisitionIntent.id).order_by(AcquisitionIntent.id.desc()).limit(1)
        )
    if ceiling is None:
        return
    cursor = None
    while True:
        async with factory() as db:
            statement = (
                select(AcquisitionIntent.id)
                .where(AcquisitionIntent.id <= ceiling)
                .order_by(AcquisitionIntent.id)
                .limit(100)
            )
            if cursor is not None:
                statement = statement.where(AcquisitionIntent.id > cursor)
            batch = (await db.scalars(statement)).all()
        if not batch:
            return
        for intent_id in batch:
            async with factory() as db, db.begin():
                intent = await db.get(AcquisitionIntent, intent_id)
                if intent is None:
                    continue
                await acquisition_lock(db, intent.work_id)
                await db.refresh(intent)
                user = await db.get(User, intent.owner_id)
                await evaluate(db, user, intent)
        cursor = batch[-1]
