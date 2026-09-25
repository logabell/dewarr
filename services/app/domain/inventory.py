import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from app.adapters.audiobookshelf import IDENTITY_ISSUES, ABSItem, Audiobookshelf
from app.adapters.contracts import AdapterError, FailureKind, ResponseTooLarge
from app.adapters.grimmory import Grimmory
from app.adapters.http import JsonEndpoint
from app.config import get_settings
from app.db.models import (
    AssetContains,
    Integration,
    InventoryAbsence,
    InventoryItemState,
    InventoryObservation,
    InventoryRun,
    Library,
    LibraryAsset,
    LibraryReadIssue,
    Operation,
    ProviderObject,
)
from app.db.session import session_factory
from app.domain.catalog_titles import base_title, display_title
from app.domain.identity import (
    item_part,
    normalized,
    resolve_abs_version,
    resolve_abs_work,
    version_changed,
)
from app.domain.library_review import review_counts, review_message
from app.domain.operations import transaction_lock
from app.security import decrypt_secrets


class LeaseLost(Exception):
    pass


async def fence(db, integration_id, token, generation):
    integration = await db.scalar(
        select(Integration).where(Integration.id == integration_id).with_for_update()
    )
    if (
        not integration
        or not integration.enabled
        or integration.lease_token != token
        or integration.credential_generation != generation
    ):
        raise LeaseLost()
    integration.lease_until = datetime.now(UTC) + timedelta(minutes=3)
    return integration


def summary_fingerprint(items: list[dict]) -> dict[str, tuple]:
    return {
        item["id"]: (
            item.get("updatedAt"),
            item.get("isMissing"),
            item.get("isInvalid"),
            item.get("_summary_hash")
            or hashlib.sha256(
                json.dumps(
                    {
                        key: item.get(key)
                        for key in ("path", "media", "numFiles", "size", "mtimeMs", "ctimeMs")
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        for item in items
    }


# Bump when the item parser or identity rules need a complete detail refresh.
INVENTORY_SCHEMA = 2
DETAIL_REFRESH = timedelta(hours=24)
APPLY_BATCH = 100
SNAPSHOT_BYTES = 4 * 1024 * 1024


async def details(client, library_id, records):
    if hasattr(client, "inventory_items"):
        async for item in client.inventory_items(library_id, records):
            yield item
    elif records:
        for item in await client.expanded([row["id"] for row in records]):
            yield item


async def read_library(
    client, external_library_id, run_id, integration_id, token, generation, scope
):
    count, expected, page = 0, None, 0
    while True:
        records, total = await client.page(external_library_id, page)
        fingerprint = summary_fingerprint(records)
        if (expected is not None and expected != total) or count + len(records) > total:
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library size changed during sync. Try again."
            )
        if len(fingerprint) != len(records) or (not records and count != total):
            raise AdapterError(FailureKind.UNCERTAIN, "Library pagination did not make progress.")
        expected = total
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, generation)
            repeated = await db.scalar(
                select(InventoryObservation.item_external_id)
                .where(
                    InventoryObservation.run_id == run_id,
                    InventoryObservation.item_external_id.in_(fingerprint),
                )
                .limit(1)
            )
            if repeated:
                raise AdapterError(
                    FailureKind.UNCERTAIN,
                    "Library pagination repeated an item or moved it between libraries.",
                )
            cached = {
                row.item_external_id: row
                for row in await db.scalars(
                    select(InventoryItemState).where(
                        InventoryItemState.integration_id == integration_id,
                        InventoryItemState.library_external_id == external_library_id,
                        InventoryItemState.item_external_id.in_(fingerprint),
                        InventoryItemState.credential_generation == generation,
                        InventoryItemState.scope_fingerprint == scope,
                        InventoryItemState.schema_version == INVENTORY_SCHEMA,
                        InventoryItemState.checked_at > datetime.now(UTC) - DETAIL_REFRESH,
                    )
                )
            }
            # Only ABS currently exposes a source revision suitable for reuse.
            # Grimmory's summary hash omits some file/track evidence.
            summary_rows = {row["id"]: row for row in records}
            reused = {
                key
                for key, marker in fingerprint.items()
                if isinstance(client, Audiobookshelf)
                and isinstance(summary_rows[key].get("path"), str)
                and isinstance(summary_rows[key].get("media"), dict)
                and marker[0] is not None
                and key in cached
                and cached[key].source_marker == list(marker)
            }
            db.add_all(
                [
                    InventoryObservation(
                        run_id=run_id,
                        library_external_id=external_library_id,
                        item_external_id=key,
                        snapshot={"observed_media": cached[key].observed_media},
                        source_marker=list(fingerprint[key]),
                        reused=True,
                    )
                    for key in reused
                ]
            )
        pending, pending_bytes = [], 0
        async for item in details(
            client, external_library_id, [row for row in records if row["id"] not in reused]
        ):
            if item.library_id != external_library_id or item.id not in fingerprint:
                raise AdapterError(
                    FailureKind.UNCERTAIN, "A library item moved during sync. Try again."
                )
            snapshot = item.model_dump(mode="json")
            snapshot_bytes = len(json.dumps(snapshot).encode())
            if pending and pending_bytes + snapshot_bytes > SNAPSHOT_BYTES:
                async with session_factory()() as db, db.begin():
                    await fence(db, integration_id, token, generation)
                    db.add_all(pending)
                pending, pending_bytes = [], 0
            pending.append(
                InventoryObservation(
                    run_id=run_id,
                    library_external_id=external_library_id,
                    item_external_id=item.id,
                    snapshot=snapshot,
                    snapshot_bytes=snapshot_bytes,
                    source_marker=list(fingerprint[item.id]),
                )
            )
            pending_bytes += snapshot_bytes
            if len(pending) >= 25:
                async with session_factory()() as db, db.begin():
                    await fence(db, integration_id, token, generation)
                    db.add_all(pending)
                pending, pending_bytes = [], 0
        if pending:
            async with session_factory()() as db, db.begin():
                await fence(db, integration_id, token, generation)
                db.add_all(pending)
        count += len(records)
        if count == total:
            break
        page += 1
    async with session_factory()() as db:
        staged = await db.scalar(
            select(func.count())
            .select_from(InventoryObservation)
            .where(
                InventoryObservation.run_id == run_id,
                InventoryObservation.library_external_id == external_library_id,
            )
        )
        if staged != count:
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library item details did not match the census."
            )
    return count


async def verify_library(
    client, external_library_id, run_id, expected, integration_id, token, generation
):
    # Persist verification membership, so neither pass retains every ID in RAM.
    count, page = 0, 0
    while True:
        records, total = await client.page(external_library_id, page)
        fingerprint = summary_fingerprint(records)
        if (
            total != expected
            or len(fingerprint) != len(records)
            or count + len(records) > expected
            or (not records and count != expected)
        ):
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library changed during verification. Try again."
            )
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, generation)
            staged = (
                await db.execute(
                    select(
                        InventoryObservation.item_external_id,
                        InventoryObservation.source_marker,
                        InventoryObservation.verified,
                    ).where(
                        InventoryObservation.run_id == run_id,
                        InventoryObservation.library_external_id == external_library_id,
                        InventoryObservation.item_external_id.in_(fingerprint),
                    )
                )
            ).all()
            if len(staged) != len(records) or any(
                row.verified or row.source_marker != list(fingerprint[row.item_external_id])
                for row in staged
            ):
                raise AdapterError(
                    FailureKind.UNCERTAIN, "Library changed during verification. Try again."
                )
            await db.execute(
                update(InventoryObservation)
                .where(
                    InventoryObservation.run_id == run_id,
                    InventoryObservation.library_external_id == external_library_id,
                    InventoryObservation.item_external_id.in_(fingerprint),
                )
                .values(verified=True)
            )
        count += len(records)
        if count == expected:
            return
        page += 1


async def record_read_issue(db, library, item, now):
    issue = await db.scalar(
        select(LibraryReadIssue).where(
            LibraryReadIssue.library_id == library.id,
            LibraryReadIssue.external_id == item.id,
        )
    )
    if item.unreadable:
        if not issue:
            issue = LibraryReadIssue(library_id=library.id, external_id=item.id)
            db.add(issue)
        issue.title, issue.authors, issue.path = item.title, item.authors, item.path
        issue.reasons, issue.last_seen_at, issue.resolved_at = item.read_issues, now, None
    elif issue and not issue.resolved_at:
        issue.resolved_at = now


async def apply_item(db, library, item, generation, integration_id, seen, *, kind=None):
    now = datetime.now(UTC)
    await record_read_issue(db, library, item, now)
    if getattr(item, "unreadable", False):
        # Keep the previous observation. A later successful read can replace it.
        for medium in ("ebook", "audio"):
            asset = await db.scalar(
                select(LibraryAsset).where(
                    LibraryAsset.library_id == library.id,
                    LibraryAsset.external_id == item.id,
                    LibraryAsset.medium == medium,
                )
            )
            if asset:
                asset.last_seen_at, asset.seen_generation = now, generation
        return
    # Without a readable title or author, Dewarr cannot tell which book this is.
    held = bool(IDENTITY_ISSUES.intersection(item.read_issues))
    for medium in ("ebook", "audio"):
        files = getattr(item, medium)
        if not files:
            continue
        if kind is None:
            kind = await db.scalar(select(Integration.kind).where(Integration.id == integration_id))
        namespace = f"{'grimmory' if kind == 'grimmory' else 'abs'}:{integration_id}"
        link = await db.scalar(
            select(ProviderObject).where(
                ProviderObject.provider == namespace,
                ProviderObject.kind == f"item:{medium}",
                ProviderObject.external_id == item.id,
            )
        )
        asset = await db.scalar(
            select(LibraryAsset).where(
                LibraryAsset.library_id == library.id,
                LibraryAsset.external_id == item.id,
                LibraryAsset.medium == medium,
            )
        )
        if not link and item.old_id and item.old_id not in seen:
            link = await db.scalar(
                select(ProviderObject).where(
                    ProviderObject.provider == namespace,
                    ProviderObject.kind == f"item:{medium}",
                    ProviderObject.external_id == item.old_id,
                )
            )
            if link:
                link.external_id = item.id
                asset = await db.scalar(
                    select(LibraryAsset).where(
                        LibraryAsset.library_id == library.id,
                        LibraryAsset.external_id == item.old_id,
                        LibraryAsset.medium == medium,
                    )
                )
                if asset:
                    asset.external_id = item.id
        if asset and asset.state == "intentionally-removed":
            # A part folded into a combined book stays retired while its old entry lingers.
            asset.last_seen_at, asset.seen_generation = now, generation
            continue
        if not link:
            link = ProviderObject(provider=namespace, kind=f"item:{medium}", external_id=item.id)
            db.add(link)
        if held and asset and link.work_id:
            # Keep the match made from earlier readable data until the backend reads cleanly.
            asset.last_seen_at, asset.seen_generation = now, generation
            asset.read_issues = item.read_issues
            asset.state = "missing-suspected" if item.missing or item.invalid else "present"
            asset.missing_since = (
                (asset.missing_since or now) if item.missing or item.invalid else None
            )
            continue
        # Serialize same-title resolution across independent backend connections.
        # Edition labels and parts share a lock with the short title so both cannot create a book.
        identity = display_title(base_title(item.title)) or normalized(item.title)
        await transaction_lock(db, "identity:" + identity)
        previous_work_id = link.work_id
        if held or (asset and asset.containment):
            work = None
        else:
            work = await resolve_abs_work(db, item, link)
        if not held and not (asset and asset.containment) and version_changed(item, link, medium):
            work = None
            link.match_status = "needs-review"
        if not asset:
            asset = LibraryAsset(library_id=library.id, external_id=item.id, medium=medium)
            db.add(asset)
            await db.flush()
        asset.title, asset.metadata_snapshot = item.title, item.model_dump(mode="json")
        asset.read_issues = item.read_issues
        previous_files = asset.files or []
        observed_files = [file.model_dump() for file in files]
        asset.last_seen_at, asset.seen_generation = now, generation
        asset.match_status = link.match_status if work else "needs-review"
        asset.full_content = bool(work and getattr(item, f"full_{medium}"))
        asset.state = "missing-suspected" if item.missing or item.invalid else "present"
        asset.missing_since = (asset.missing_since or now) if item.missing or item.invalid else None
        supplementary = medium == "ebook" and item.ebook_supplementary
        if work and not supplementary:
            version = await resolve_abs_version(db, work, item, medium, link)
            if medium == "ebook" and asset.version_id == version.id and item.full_ebook:
                # ABS selects one primary ebook. Additional complete formats are
                # known only from our reviewed import, never from nearby files.
                observed = {file.path: file.model_dump() for file in item.library_files}
                primary_paths = {file["path"] for file in observed_files}
                for previous in previous_files:
                    current = observed.get(previous["path"])
                    if (
                        previous.get("import_verified")
                        and current
                        and previous.get("inode") is not None
                        and previous.get("modified") is not None
                        and all(
                            previous.get(key) == current.get(key)
                            for key in ("path", "size", "format", "inode", "modified")
                        )
                    ):
                        if previous["path"] in primary_paths:
                            for file in observed_files:
                                if file["path"] == previous["path"]:
                                    file["import_verified"] = True
                        else:
                            observed_files.append({**current, "import_verified": True})
            asset.version_id = version.id
            if previous_work_id and previous_work_id != work.id:
                previous = await db.get(AssetContains, (asset.id, previous_work_id))
                if previous:
                    previous.verified = False
            coverage = await db.get(AssetContains, (asset.id, work.id))
            if not coverage:
                coverage = AssetContains(asset_id=asset.id, work_id=work.id, verified=True)
                db.add(coverage)
            else:
                coverage.verified = True
            if not link.manual_lock:
                # A manual match keeps the part number the reviewer chose.
                coverage.part_index, coverage.part_total = item_part(item) or (None, None)
            link.snapshot = item.model_dump(mode="json")
        else:
            if supplementary:
                # Retain the supporting-file observation, but do not manufacture
                # an ebook edition or verified work coverage from it.
                asset.version_id = None
                asset.full_content = False
                link.snapshot = item.model_dump(mode="json")
            await db.execute(
                update(AssetContains)
                .where(AssetContains.asset_id == asset.id)
                .values(verified=False)
            )
        # Assign after awaited queries: autoflush must not persist only the primary
        # before additional verified formats are appended to an ordinary JSON list.
        asset.files = observed_files
        if asset.containment:
            from app.domain.containment import reconcile

            await reconcile(db, asset, link, item)


async def publish_library(
    client, run_id, library_info, integration_id, token, credential_generation, scope
):
    async with session_factory()() as db, db.begin():
        integration = await fence(db, integration_id, token, credential_generation)
        kind = integration.kind
        library = await db.scalar(
            select(Library)
            .where(
                Library.integration_id == integration_id,
                Library.external_id == library_info["id"],
            )
            .with_for_update()
        )
        if not library:
            library = Library(
                integration_id=integration_id,
                external_id=library_info["id"],
                name=library_info["name"],
                accessible=False,
                generation=0,
            )
            db.add(library)
            await db.flush()
        same_scope = library.scope_fingerprint == scope
        library.generation += 1
        library.name = library_info["name"]
        library_id, generation = library.id, library.generation
    published_at = datetime.now(UTC)
    cursor = ""
    while True:
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, credential_generation)
            candidates = (
                await db.execute(
                    select(
                        InventoryObservation.item_external_id,
                        InventoryObservation.snapshot_bytes,
                    )
                    .where(
                        InventoryObservation.run_id == run_id,
                        InventoryObservation.library_external_id == library_info["id"],
                        InventoryObservation.item_external_id > cursor,
                    )
                    .order_by(InventoryObservation.item_external_id)
                    .limit(APPLY_BATCH)
                )
            ).all()
            ids, size = [], 0
            for identifier, length in candidates:
                if ids and size + length > SNAPSHOT_BYTES:
                    break
                ids.append(identifier)
                size += length
            if not ids:
                break
            records = list(
                await db.scalars(
                    select(InventoryObservation)
                    .where(
                        InventoryObservation.run_id == run_id,
                        InventoryObservation.library_external_id == library_info["id"],
                        InventoryObservation.item_external_id.in_(ids),
                    )
                    .order_by(InventoryObservation.item_external_id)
                    .limit(APPLY_BATCH)
                )
            )
            if not records:
                break
            if any(not row.verified for row in records):
                raise AdapterError(FailureKind.UNCERTAIN, "Inventory verification is incomplete.")
            library = await db.get(Library, library_id)
            old_ids = {row.snapshot.get("old_id") for row in records} - {None}
            seen = set(
                await db.scalars(
                    select(InventoryObservation.item_external_id).where(
                        InventoryObservation.run_id == run_id,
                        InventoryObservation.library_external_id == library_info["id"],
                        InventoryObservation.item_external_id.in_(old_ids),
                    )
                )
            )
            # An item may have lost one format while retaining another. Only
            # replay formats from its last detail observation; historical assets
            # for removed formats must still pass through absence confirmation.
            reused = [
                (row.item_external_id, medium)
                for row in records
                if row.reused
                for medium in ("ebook", "audio")
                if medium in row.snapshot["observed_media"]
            ]
            if reused:
                retired = LibraryAsset.state == "intentionally-removed"
                await db.execute(
                    update(LibraryAsset)
                    .where(
                        LibraryAsset.library_id == library_id,
                        tuple_(LibraryAsset.external_id, LibraryAsset.medium).in_(reused),
                    )
                    .values(
                        last_seen_at=published_at,
                        seen_generation=generation,
                        state=case((retired, LibraryAsset.state), else_="present"),
                        missing_since=case((retired, LibraryAsset.missing_since), else_=None),
                    )
                )
            states = []
            for row in sorted(
                (row for row in records if not row.reused),
                key=lambda row: normalized(row.snapshot["title"]),
            ):
                item = ABSItem.model_validate(row.snapshot)
                await apply_item(db, library, item, generation, integration_id, seen, kind=kind)
                if (
                    not item.unreadable
                    and not item.read_issues
                    and not item.missing
                    and not item.invalid
                ):
                    states.append(
                        dict(
                            integration_id=integration_id,
                            library_external_id=library_info["id"],
                            item_external_id=item.id,
                            source_marker=row.source_marker,
                            observed_media=[
                                medium for medium in ("ebook", "audio") if getattr(item, medium)
                            ],
                            credential_generation=credential_generation,
                            scope_fingerprint=scope,
                            schema_version=INVENTORY_SCHEMA,
                            checked_at=published_at,
                        )
                    )
                else:
                    await db.execute(
                        delete(InventoryItemState).where(
                            InventoryItemState.integration_id == integration_id,
                            InventoryItemState.library_external_id == library_info["id"],
                            InventoryItemState.item_external_id == item.id,
                        )
                    )
            if states:
                statement = insert(InventoryItemState).values(states)
                await db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            "integration_id",
                            "library_external_id",
                            "item_external_id",
                        ],
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in (
                                "source_marker",
                                "observed_media",
                                "credential_generation",
                                "scope_fingerprint",
                                "schema_version",
                                "checked_at",
                            )
                        },
                    )
                )
            cursor = records[-1].item_external_id

    # Only bounded identifiers/metadata are loaded. Stage all negative decisions
    # before exposing any of them; a later network failure leaves ownership intact.
    cursor = None
    now = datetime.now(UTC)
    while True:
        async with session_factory()() as db:
            query = (
                select(
                    LibraryAsset.id,
                    LibraryAsset.external_id,
                    LibraryAsset.medium,
                    LibraryAsset.missing_since,
                    InventoryObservation.library_external_id,
                )
                .outerjoin(
                    InventoryObservation,
                    and_(
                        InventoryObservation.run_id == run_id,
                        InventoryObservation.item_external_id == LibraryAsset.external_id,
                    ),
                )
                .where(
                    LibraryAsset.library_id == library_id,
                    LibraryAsset.seen_generation != generation,
                    LibraryAsset.state != "intentionally-removed",
                )
                .order_by(LibraryAsset.id)
                .limit(APPLY_BATCH)
            )
            if cursor:
                query = query.where(LibraryAsset.id > cursor)
            candidates = (await db.execute(query)).all()
        if not candidates:
            break
        for asset_id, external, medium, since, destination in candidates:
            state, missing_since = "missing-suspected", since or now
            if destination and destination != library_info["id"]:
                detail = await client.item(external)
                if detail.library_id != destination:
                    raise AdapterError(FailureKind.UNCERTAIN, "A moved item changed during sync.")
                state, missing_since = "moved", None
            elif not same_scope:
                state = "scope-unavailable"
                missing_since = since
            elif since and (now - since).total_seconds() >= 300:
                try:
                    detail = await client.item(external)
                    if not getattr(detail, "unreadable", False):
                        if detail.library_id == library_info["id"] and not getattr(detail, medium):
                            state = "missing-confirmed"
                        elif destination is None:
                            raise AdapterError(
                                FailureKind.UNCERTAIN,
                                "An omitted library item still exists. Sync was held.",
                            )
                except AdapterError as error:
                    if error.kind != FailureKind.NOT_FOUND:
                        raise
                    state = "missing-confirmed"
            async with session_factory()() as db, db.begin():
                await fence(db, integration_id, token, credential_generation)
                db.add(
                    InventoryAbsence(
                        run_id=run_id, asset_id=asset_id, state=state, missing_since=missing_since
                    )
                )
        cursor = candidates[-1].id
    async with session_factory()() as db, db.begin():
        await fence(db, integration_id, token, credential_generation)
        await db.execute(
            update(LibraryAsset)
            .where(
                LibraryAsset.library_id == library_id,
                LibraryAsset.id == InventoryAbsence.asset_id,
                InventoryAbsence.run_id == run_id,
                # A concurrent import/manual retirement takes precedence over absence.
                LibraryAsset.seen_generation != generation,
                LibraryAsset.state != "intentionally-removed",
            )
            .values(state=InventoryAbsence.state, missing_since=InventoryAbsence.missing_since)
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(LibraryReadIssue)
            .where(
                LibraryReadIssue.library_id == library_id,
                LibraryReadIssue.resolved_at.is_(None),
                LibraryReadIssue.last_seen_at < published_at,
            )
            .values(resolved_at=now)
        )
        await db.execute(
            delete(InventoryItemState).where(
                InventoryItemState.integration_id == integration_id,
                InventoryItemState.library_external_id == library_info["id"],
                ~select(InventoryObservation.item_external_id)
                .where(
                    InventoryObservation.run_id == run_id,
                    InventoryObservation.library_external_id == library_info["id"],
                    InventoryObservation.item_external_id == InventoryItemState.item_external_id,
                )
                .exists(),
            )
        )
        library = await db.get(Library, library_id)
        library.last_complete_sync = now
        library.scope_fingerprint, library.accessible = scope, True


async def synchronize(operation_id: UUID, *, client_factory=None):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        operation = await db.scalar(
            select(Operation).where(Operation.id == operation_id).with_for_update()
        )
        if not operation or operation.status in {"completed", "cancelled"}:
            return
        integration = await db.scalar(
            select(Integration).where(Integration.id == operation.integration_id).with_for_update()
        )
        if not integration or not integration.enabled or get_settings().recovery_mode:
            operation.status, operation.message = "cancelled", "Sync paused or connection disabled"
            return
        now = datetime.now(UTC)
        if integration.lease_until and integration.lease_until > now:
            raise AdapterError(FailureKind.UNAVAILABLE, "Another sync is still active")
        integration.lease_token, integration.lease_until = token, now + timedelta(minutes=3)
        library_name = "Grimmory" if integration.kind == "grimmory" else "Audiobookshelf"
        operation.status, operation.message = "running", f"Reading {library_name} library inventory"
        operation.payload = {**operation.payload, "lease_token": str(token)}
        # An expired owner cannot resume its staged snapshot after a new claim.
        abandoned = select(InventoryRun.id).where(
            InventoryRun.integration_id == integration.id,
            InventoryRun.status == "collecting",
        )
        await db.execute(delete(InventoryAbsence).where(InventoryAbsence.run_id.in_(abandoned)))
        await db.execute(
            delete(InventoryObservation).where(InventoryObservation.run_id.in_(abandoned))
        )
        await db.execute(
            update(InventoryRun)
            .where(InventoryRun.id.in_(abandoned))
            .values(status="interrupted", completed_at=now)
        )
        integration_id, generation = integration.id, integration.credential_generation
        endpoint = integration.base_url
        secrets = decrypt_secrets(integration.encrypted_secrets)
        secret = secrets if integration.kind == "grimmory" else secrets["token"]
        kind = integration.kind
        library_name = "Grimmory" if kind == "grimmory" else "Audiobookshelf"
        factory = client_factory or (Grimmory if kind == "grimmory" else Audiobookshelf)
        run = InventoryRun(
            integration_id=integration_id,
            operation_id=operation_id,
            credential_generation=generation,
        )
        db.add(run)
        await db.flush()
        run_id = run.id
    try:
        async with factory(endpoint, secret) as client:
            last_pulse = time.monotonic()

            async def pulse():
                nonlocal last_pulse
                if time.monotonic() - last_pulse >= 30:
                    async with session_factory()() as db, db.begin():
                        await fence(db, integration_id, token, generation)
                    last_pulse = time.monotonic()

            if isinstance(client, JsonEndpoint):
                # A Grimmory census or adaptive detail split can perform many
                # requests before returning one logical page. Keep its lease
                # alive between requests, with no transaction held over I/O.
                client.before_request = pulse
            capabilities, scope = await client.authorize()
            libraries = await client.libraries()
            counts = {}
            for library in libraries:
                counts[library["id"]] = await read_library(
                    client, library["id"], run_id, integration_id, token, generation, scope
                )
            if hasattr(client, "refresh_snapshot"):
                await client.refresh_snapshot()
            for library in libraries:
                await verify_library(
                    client,
                    library["id"],
                    run_id,
                    counts[library["id"]],
                    integration_id,
                    token,
                    generation,
                )
            for library in libraries:
                _, current_scope = await client.authorize()
                if current_scope != scope:
                    raise AdapterError(
                        FailureKind.PERMISSION,
                        "Account access changed during sync. Run a fresh sync.",
                    )
                await publish_library(
                    client, run_id, library, integration_id, token, generation, scope
                )
            _, final_scope = await client.authorize()
            if final_scope != scope:
                raise AdapterError(
                    FailureKind.PERMISSION, "Account access changed during sync. Run a fresh sync."
                )
            current_libraries = await client.libraries()
            if {library["id"] for library in current_libraries} != {
                library["id"] for library in libraries
            }:
                raise AdapterError(
                    FailureKind.UNCERTAIN, "Library access changed during sync. Try again."
                )
        async with session_factory()() as db, db.begin():
            integration = await fence(db, integration_id, token, generation)
            await db.execute(
                update(Library)
                .where(
                    Library.integration_id == integration_id,
                    Library.external_id.not_in([library["id"] for library in libraries]),
                )
                .values(accessible=False)
            )
            integration.status, integration.last_error = "connected", None
            integration.capabilities = {
                **capabilities.model_dump(mode="json"),
                "library_count": len(libraries),
                "book_count": sum(counts.values()),
            }
            integration.last_success_at = datetime.now(UTC)
            integration.next_sync_at = datetime.now(UTC) + timedelta(minutes=30)
            integration.lease_token, integration.lease_until = None, None
            run = await db.get(InventoryRun, run_id)
            run.status, run.completed_at = "completed", datetime.now(UTC)
            review = await review_counts(
                db, select(Library.id).where(Library.integration_id == integration_id)
            )
            operation = await db.get(Operation, operation_id)
            operation.status, operation.message = (
                "completed",
                f"Synced {len(libraries)} {library_name} libraries" + review_message(review),
            )
            reused = await db.scalar(
                select(func.count())
                .select_from(InventoryObservation)
                .where(InventoryObservation.run_id == run_id, InventoryObservation.reused.is_(True))
            )
            operation.payload = {
                **operation.payload,
                "review": review,
                "inventory": {
                    "items": sum(counts.values()),
                    "details_reused": reused,
                    "details_read": sum(counts.values()) - reused,
                },
            }
            await db.execute(delete(InventoryAbsence).where(InventoryAbsence.run_id == run_id))
            await db.execute(
                delete(InventoryObservation).where(InventoryObservation.run_id == run_id)
            )
            from app.domain.library_matching import schedule_library_match
            from app.importing.combine import schedule_library_combine

            await schedule_library_match(db, operation.owner_id, integration_id, run_id)
            if kind == "audiobookshelf":
                await schedule_library_combine(db, operation.owner_id, integration_id, run_id)
    except (AdapterError, LeaseLost) as error:
        async with session_factory()() as db, db.begin():
            integration = await db.get(Integration, integration_id, with_for_update=True)
            operation = await db.get(Operation, operation_id)
            run = await db.get(InventoryRun, run_id)
            run.status, run.completed_at = "failed", datetime.now(UTC)
            if operation.payload.get("lease_token") == str(token):
                operation.status = "cancelled" if isinstance(error, LeaseLost) else "failed"
                operation.message = (
                    "Connection changed during sync; start a new sync"
                    if isinstance(error, LeaseLost)
                    else str(error)
                )
            if integration and integration.lease_token == token:
                integration.lease_token, integration.lease_until = None, None
                if isinstance(error, AdapterError):
                    integration.status, integration.last_error = error.kind.value, str(error)
                    integration.next_sync_at = (
                        None
                        if not isinstance(error, ResponseTooLarge)
                        and error.kind
                        in {FailureKind.AUTHENTICATION, FailureKind.PERMISSION, FailureKind.PARSER}
                        else datetime.now(UTC) + timedelta(minutes=5)
                    )
                    if error.kind in {FailureKind.PERMISSION, FailureKind.AUTHENTICATION}:
                        await db.execute(
                            update(Library)
                            .where(Library.integration_id == integration_id)
                            .values(accessible=False)
                        )
                    library_ids = select(Library.id).where(Library.integration_id == integration_id)
                    await db.execute(
                        update(LibraryAsset)
                        .where(
                            LibraryAsset.library_id.in_(library_ids),
                            LibraryAsset.state == "present",
                        )
                        .values(state="stale")
                    )
            await db.execute(delete(InventoryAbsence).where(InventoryAbsence.run_id == run_id))
            await db.execute(
                delete(InventoryObservation).where(InventoryObservation.run_id == run_id)
            )
