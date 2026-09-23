"""Reversible identity decisions. Journal writes share the correction transaction."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.adapters.audiobookshelf import ABSItem
from app.adapters.catalog_types import EditionData
from app.db.models import (
    AssetContains,
    AuditEvent,
    IdentityChange,
    Integration,
    Library,
    LibraryAsset,
    ProviderObject,
    Version,
    Work,
    WorkMetadataSource,
)
from app.domain.catalog_metadata import FIELDS, preferences, resolve_fields
from app.domain.identity import item_part, resolve_abs_version, version_changed
from app.domain.work_graph import canonical_work, family_ids


def revision(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def uuid_or_none(value):
    return UUID(value) if value else None


def check_revision(current, expected):
    if expected and revision(current) != expected:
        raise HTTPException(
            409, "This match changed since you opened it. Refresh and review the current details."
        )


async def journal(db, actor_id, kind, entity_id, work_id, before, after, summary):
    if before == after:
        return None
    change = IdentityChange(
        actor_id=actor_id,
        kind=kind,
        entity_id=entity_id,
        work_id=work_id,
        before=before,
        after=after,
        summary=summary[:500],
    )
    db.add(change)
    await db.flush()
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action=f"identity.{kind}",
            entity_id=entity_id,
            detail={"change_id": str(change.id)},
        )
    )
    return change


async def asset_target(db, asset_id, *, lock=False):
    integration_id = await db.scalar(
        select(Library.integration_id).join(LibraryAsset).where(LibraryAsset.id == asset_id)
    )
    if integration_id and lock:
        # The inventory publisher uses this same integration → asset order.
        await db.get(Integration, integration_id, with_for_update=True)
    asset = await db.get(LibraryAsset, asset_id, with_for_update=lock, populate_existing=True)
    if not asset:
        raise HTTPException(404, "Library item not found")
    kind = (
        await db.scalar(select(Integration.kind).where(Integration.id == integration_id))
        if integration_id
        else None
    )
    prefix = "grimmory" if kind == "grimmory" else "abs"
    link_query = select(ProviderObject).where(
        ProviderObject.provider == f"{prefix}:{integration_id}",
        ProviderObject.kind == f"item:{asset.medium}",
        ProviderObject.external_id == asset.external_id,
    )
    if lock:
        link_query = link_query.with_for_update()
    link = await db.scalar(link_query.execution_options(populate_existing=True))
    if not link:
        raise HTTPException(409, "Sync this library before correcting its match")
    return asset, link


def evidence(snapshot):
    # Sync timestamps, paths and file moves do not change a bibliographic decision.
    fields = (
        "title",
        "authors",
        "narrators",
        "language",
        "year",
        "abridged",
        "identifiers",
        "full_audio",
        "full_ebook",
        "missing",
        "invalid",
    )
    return {field: snapshot.get(field) for field in fields}


async def asset_state(db, asset, link, *, coverage=None):
    if coverage is None:
        coverage = (
            await db.scalars(
                select(AssetContains)
                .where(AssetContains.asset_id == asset.id)
                .order_by(AssetContains.work_id)
            )
        ).all()
    coverage = sorted(coverage, key=lambda row: row.work_id)
    return {
        "link_id": str(link.id),
        "work_id": str(link.work_id) if link.work_id else None,
        "link_version_id": str(link.version_id) if link.version_id else None,
        "version_id": str(asset.version_id) if asset.version_id else None,
        "manual_lock": link.manual_lock,
        "link_status": link.match_status,
        "match_status": asset.match_status,
        "full_content": asset.full_content,
        "accepted_evidence": evidence(link.snapshot),
        "observed_evidence": evidence(asset.metadata_snapshot),
        "observed_files": sorted(asset.files, key=lambda value: value["path"]),
        "containment": asset.containment,
        "coverage": [
            {"work_id": str(row.work_id), "verified": row.verified}
            | (
                {"part_index": row.part_index, "part_total": row.part_total}
                if row.part_total
                else {}
            )
            for row in coverage
        ],
    }


async def correct_asset(db, actor_id, asset_id, work_id, expected_revision=None, *, part=None):
    """``part`` is (N, M), False for the whole book, or None to read it from the title."""
    asset, link = await asset_target(db, asset_id, lock=True)
    before = await asset_state(db, asset, link)
    check_revision(before, expected_revision)
    work = await db.get(Work, work_id) if work_id else None
    if work_id and (not work or work.redirect_to):
        raise HTTPException(404, "Book not found or merged; select its current record")
    item = ABSItem.model_validate(asset.metadata_snapshot)
    if link.work_id != work_id or version_changed(item, link, asset.medium):
        link.version_id = None
    if work and not link.version_id:
        # A prior decision about this exact asset is stronger than narrator/title similarity.
        previous = (
            await db.scalars(
                select(IdentityChange)
                .where(
                    IdentityChange.kind == "asset_match",
                    IdentityChange.entity_id == asset.id,
                    IdentityChange.after["work_id"].astext == str(work.id),
                )
                .order_by(IdentityChange.sequence.desc())
                .limit(50)
            )
        ).all()
        for decision in previous:
            if decision.after["accepted_evidence"] != evidence(item.model_dump(mode="json")):
                continue
            prior = await db.get(Version, uuid_or_none(decision.after["version_id"]))
            if (
                prior
                and prior.work_id == work.id
                and prior.medium == asset.medium
                and prior.language == item.language
                and prior.publication_year == item.year
                and prior.abridged == item.abridged
                and prior.identifiers == item.identifiers
                and prior.narrators == (item.narrators if asset.medium == "audio" else [])
            ):
                link.version_id = prior.id
                break
    asset.containment = None
    link.work_id, link.manual_lock = work_id, True
    await db.execute(delete(AssetContains).where(AssetContains.asset_id == asset.id))
    if work:
        chosen = item_part(item) if part is None else part or None
        version = await resolve_abs_version(db, work, item, asset.medium, link, part=chosen)
        asset.version_id, asset.match_status = version.id, "manual"
        asset.full_content = getattr(item, f"full_{asset.medium}")
        link.match_status = "manual"
        index, total = chosen or (None, None)
        db.add(
            AssetContains(
                asset_id=asset.id,
                work_id=work.id,
                verified=True,
                part_index=index,
                part_total=total,
            )
        )
    else:
        asset.version_id, asset.full_content, asset.match_status = None, False, "needs-review"
        link.version_id, link.match_status = None, "unmatched"
    link.snapshot = item.model_dump(mode="json")
    await db.flush()
    return await journal(
        db,
        actor_id,
        "asset_match",
        asset.id,
        work_id or uuid_or_none(before["work_id"]),
        before,
        await asset_state(db, asset, link),
        f"Matched library item to {work.title}"
        if work
        else "Removed the library item's book match",
    )


async def source_target(db, source_id, *, lock=False):
    work_id = await db.scalar(
        select(WorkMetadataSource.work_id).where(WorkMetadataSource.id == source_id)
    )
    if not work_id:
        raise HTTPException(404, "Catalog source not found")
    canonical = await canonical_work(db, work_id)
    work = await db.get(Work, canonical.id, with_for_update=lock, populate_existing=True)
    source = await db.get(WorkMetadataSource, source_id, populate_existing=True)
    if not work or work.redirect_to:
        raise HTTPException(409, "This book identity changed; open its current record")
    return work, source


def source_state(work, source):
    return {
        "accepted": source.accepted,
        "manual_match": source.manual_match,
        "snapshot": source.snapshot,
        "fetched_at": source.fetched_at.isoformat(),
        "work": {
            **{field: getattr(work, field) for field in FIELDS},
            "metadata_fields": work.metadata_fields,
            "provisional": work.provisional,
            "match_key": work.match_key,
        },
    }


async def detach_source(db, actor_id, source_id, expected_revision):
    work, source = await source_target(db, source_id, lock=True)
    before = source_state(work, source)
    check_revision(before, expected_revision)
    if not source.accepted:
        return None
    source.accepted, source.manual_match = False, True
    fields = dict(work.metadata_fields.get("fields", {}))
    for name, provenance in list(fields.items()):
        if (
            not provenance.get("locked")
            and provenance.get("provider") == source.provider
            and provenance.get("external_id") == source.external_id
        ):
            if name in {"title", "authors"}:
                # Preserve the visible label without presenting it as trusted matching evidence.
                fields[name] = {
                    "value": getattr(work, name),
                    "provider": "unmatched",
                    "locked": False,
                    "reason": "Source removed; verify this book's identity",
                }
            else:
                setattr(work, name, None)
                fields.pop(name)
    work.metadata_fields = {**work.metadata_fields, "fields": fields}
    await db.flush()
    await resolve_fields(db, work, await preferences(db))
    work.provisional = not bool(
        await db.scalar(
            select(WorkMetadataSource.id)
            .where(
                WorkMetadataSource.work_id.in_(family_ids(work.id)),
                WorkMetadataSource.accepted.is_(True),
            )
            .limit(1)
        )
    )
    return await journal(
        db,
        actor_id,
        "source_detach",
        source.id,
        work.id,
        before,
        source_state(work, source),
        f"Stopped using {source.provider} record {source.external_id}",
    )


async def version_target(db, link_id, *, lock=False):
    work_id = await db.scalar(select(ProviderObject.work_id).where(ProviderObject.id == link_id))
    if not work_id:
        raise HTTPException(404, "Catalog version mapping not found")
    canonical = await canonical_work(db, work_id)
    work = await db.get(Work, canonical.id, with_for_update=lock, populate_existing=True)
    link = await db.get(ProviderObject, link_id, populate_existing=True)
    if not work or work.redirect_to or link.kind != "edition" or not link.metadata_source_id:
        raise HTTPException(409, "This version mapping needs a current catalog source")
    source = await db.get(WorkMetadataSource, link.metadata_source_id)
    if not source or not source.accepted:
        raise HTTPException(409, "This catalog source is no longer matched")
    return work, link


def version_state(link):
    return {
        "version_id": str(link.version_id) if link.version_id else None,
        "manual_lock": link.manual_lock,
        "match_status": link.match_status,
        "snapshot": link.snapshot,
        "pending_snapshot": link.pending_snapshot,
    }


async def review_version(db, actor_id, link_id, decision, expected_revision):
    work, link = await version_target(db, link_id, lock=True)
    before = version_state(link)
    check_revision(before, expected_revision)
    if not link.pending_snapshot or link.match_status != "needs-review":
        raise HTTPException(409, "This version no longer has a pending change")
    proposed = EditionData.model_validate(link.pending_snapshot)
    if decision == "separate":
        version = Version(
            work_id=link.work_id,
            medium=proposed.medium,
            title=proposed.title,
            language=proposed.language,
            narrators=proposed.narrators,
            abridged=proposed.abridged,
            publication_year=proposed.publication_year,
            identifiers=proposed.identifiers,
        )
        db.add(version)
        await db.flush()
        link.version_id, link.snapshot, link.manual_lock = (
            version.id,
            proposed.model_dump(mode="json"),
            False,
        )
        link.match_status = "matched"
    else:
        link.manual_lock, link.match_status = True, "manual"
    link.pending_snapshot = None
    return await journal(
        db,
        actor_id,
        "version_review",
        link.id,
        work.id,
        before,
        version_state(link),
        "Accepted changed catalog evidence as a separate version"
        if decision == "separate"
        else "Kept and protected the current catalog version",
    )


async def change_state(db, change, *, lock=False):
    if change.kind == "work_merge":
        from app.domain.work_merges import merge_change_state

        return await merge_change_state(db, change, lock=lock)
    if change.kind == "asset_match":
        asset, link = await asset_target(db, change.entity_id, lock=lock)
        current = await asset_state(db, asset, link)
        # Older ordinary-match journal records predate collection evidence.
        for key in ("observed_files", "containment"):
            if key not in change.after:
                current.pop(key)
        return current, (asset, link)
    if change.kind == "source_detach":
        work, source = await source_target(db, change.entity_id, lock=lock)
        return source_state(work, source), (work, source)
    work, link = await version_target(db, change.entity_id, lock=lock)
    return version_state(link), (work, link)


async def undo_change(db, actor_id, change_id):
    change = await db.get(IdentityChange, change_id)
    if not change:
        raise HTTPException(404, "Correction not found")
    if change.undone_at:
        return
    # Acquire the domain lock before locking its journal row, matching normal writes.
    current, targets = await change_state(db, change, lock=True)
    await db.refresh(change, with_for_update=True)
    if change.undone_at:
        return
    latest = await db.scalar(
        select(IdentityChange.id)
        .where(IdentityChange.entity_id == change.entity_id, IdentityChange.undone_at.is_(None))
        .order_by(IdentityChange.sequence.desc())
        .limit(1)
    )
    if latest != change.id or current != change.after:
        raise HTTPException(
            409,
            "Later changes prevent this undo. Review the current match "
            "or undo the latest correction first.",
        )
    before = change.before
    if change.kind == "work_merge":
        from app.domain.work_merges import restore_merge

        await restore_merge(db, change)
    elif change.kind == "asset_match":
        asset, link = targets
        asset.containment = before.get("containment")
        restored_work = (
            await db.get(Work, uuid_or_none(before["work_id"])) if before["work_id"] else None
        )
        if restored_work and restored_work.redirect_to:
            raise HTTPException(
                409, "The former book was merged. Choose its current identity instead."
            )
        link.work_id, link.version_id = (
            uuid_or_none(before["work_id"]),
            uuid_or_none(before["link_version_id"]),
        )
        link.manual_lock, link.match_status = before["manual_lock"], before["link_status"]
        link.snapshot = {**link.snapshot, **before["accepted_evidence"]}
        asset.version_id, asset.full_content, asset.match_status = (
            uuid_or_none(before["version_id"]),
            before["full_content"],
            before["match_status"],
        )
        await db.execute(delete(AssetContains).where(AssetContains.asset_id == asset.id))
        db.add_all(
            [
                AssetContains(
                    asset_id=asset.id,
                    work_id=UUID(row["work_id"]),
                    verified=row["verified"],
                    part_index=row.get("part_index"),
                    part_total=row.get("part_total"),
                )
                for row in before["coverage"]
            ]
        )
    elif change.kind == "source_detach":
        work, source = targets
        source.accepted, source.manual_match = before["accepted"], before["manual_match"]
        source.snapshot, source.fetched_at = (
            before["snapshot"],
            datetime.fromisoformat(before["fetched_at"]),
        )
        for field, value in before["work"].items():
            setattr(work, field, value)
    else:
        _, link = targets
        for field, value in before.items():
            setattr(link, field, uuid_or_none(value) if field == "version_id" else value)
    change.undone_at, change.undone_by = datetime.now(UTC), actor_id
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action="identity.correction.undone",
            entity_id=change.entity_id,
            detail={"change_id": str(change.id)},
        )
    )
    await db.flush()
