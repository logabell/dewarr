from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update

from app.adapters.audiobookshelf import IDENTITY_ISSUES, ABSItem, Audiobookshelf
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.grimmory import Grimmory
from app.config import get_settings
from app.db.models import (
    AssetContains,
    Integration,
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
        item["id"]: (item.get("updatedAt"), item.get("isMissing"), item.get("isInvalid"))
        for item in items
    }


async def read_library(client, external_library_id, run_id, integration_id, token, generation):
    seen, expected, page = {}, None, 0
    while True:
        records, total = await client.page(external_library_id, page)
        if expected is not None and expected != total:
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library size changed during sync. Try again."
            )
        expected = total
        fingerprint = summary_fingerprint(records)
        if len(fingerprint) != len(records) or seen.keys() & fingerprint.keys():
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library pagination repeated an item. Sync was held."
            )
        expanded = await client.expanded(list(fingerprint)) if fingerprint else []
        if any(item.library_id != external_library_id for item in expanded):
            raise AdapterError(
                FailureKind.UNCERTAIN, "A library item moved during sync. Try again."
            )
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, generation)
            db.add_all(
                [
                    InventoryObservation(
                        run_id=run_id,
                        library_external_id=external_library_id,
                        item_external_id=item.id,
                        snapshot=item.model_dump(mode="json"),
                    )
                    for item in expanded
                ]
            )
        seen.update(fingerprint)
        if len(seen) == total:
            break
        page += 1
    return seen, expected


async def verify_library(
    client, external_library_id, seen, expected, integration_id, token, generation
):
    # ABS pagination is not a transactional snapshot. Verify membership and update
    # markers again before publishing this run or inferring an absence.
    second, page = {}, 0
    while True:
        records, total = await client.page(external_library_id, page)
        fingerprint = summary_fingerprint(records)
        if (
            total != expected
            or len(fingerprint) != len(records)
            or second.keys() & fingerprint.keys()
        ):
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library changed during verification. Try again."
            )
        second.update(fingerprint)
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, generation)
        if len(second) == total:
            break
        page += 1
    if second != seen:
        raise AdapterError(FailureKind.UNCERTAIN, "Library changed during verification. Try again.")


async def collect_library(client, external_library_id, run_id, integration_id, token, generation):
    seen, expected = await read_library(
        client, external_library_id, run_id, integration_id, token, generation
    )
    await verify_library(
        client, external_library_id, seen, expected, integration_id, token, generation
    )
    return set(seen)


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


async def apply_item(db, library, item, generation, integration_id, seen):
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
    client,
    run_id,
    library_info,
    integration_id,
    token,
    credential_generation,
    scope,
    seen,
    locations,
):
    async with session_factory()() as db, db.begin():
        await fence(db, integration_id, token, credential_generation)
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
    offset = 0
    while True:
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, credential_generation)
            records = (
                await db.scalars(
                    select(InventoryObservation)
                    .where(
                        InventoryObservation.run_id == run_id,
                        InventoryObservation.library_external_id == library_info["id"],
                    )
                    .order_by(InventoryObservation.item_external_id)
                    .offset(offset)
                    .limit(100)
                )
            ).all()
            if not records:
                break
            library = await db.get(Library, library_id)
            for record in sorted(records, key=lambda record: normalized(record.snapshot["title"])):
                await apply_item(
                    db,
                    library,
                    ABSItem.model_validate(record.snapshot),
                    generation,
                    integration_id,
                    seen,
                )
        offset += len(records)
    # Absence confirmation uses direct read-only lookups outside any transaction.
    async with session_factory()() as db:
        absent = (
            await db.scalars(
                select(LibraryAsset).where(
                    LibraryAsset.library_id == library_id,
                    LibraryAsset.seen_generation != generation,
                )
            )
        ).all()
        candidates = [
            (asset.id, asset.external_id, asset.medium, asset.missing_since) for asset in absent
        ]
    confirmed, moved = set(), set()
    now = datetime.now(UTC)
    for asset_id, external, medium, since in candidates:
        destination = locations.get(external)
        if destination and destination != library_info["id"]:
            detail = await client.item(external)
            if detail.library_id != destination:
                raise AdapterError(FailureKind.UNCERTAIN, "A moved item changed during sync.")
            moved.add(asset_id)
            async with session_factory()() as db, db.begin():
                await fence(db, integration_id, token, credential_generation)
            continue
        if not same_scope or not since or (now - since).total_seconds() < 300:
            continue
        try:
            detail = await client.item(external)
            if getattr(detail, "unreadable", False):
                async with session_factory()() as db, db.begin():
                    await fence(db, integration_id, token, credential_generation)
                continue
            if detail.library_id == library_info["id"] and not getattr(detail, medium):
                confirmed.add(asset_id)
            elif external not in seen:
                raise AdapterError(
                    FailureKind.UNCERTAIN, "An omitted library item still exists. Sync was held."
                )
        except AdapterError as error:
            if error.kind == FailureKind.NOT_FOUND:
                confirmed.add(asset_id)
            else:
                raise
        async with session_factory()() as db, db.begin():
            await fence(db, integration_id, token, credential_generation)
    async with session_factory()() as db, db.begin():
        await fence(db, integration_id, token, credential_generation)
        library = await db.get(Library, library_id)
        for asset_id, _, _, _ in candidates:
            asset = await db.get(LibraryAsset, asset_id)
            if asset.state == "intentionally-removed":
                continue
            if asset_id in moved:
                asset.state = "moved"
                asset.missing_since = None
            elif not same_scope:
                asset.state = "scope-unavailable"
            else:
                asset.missing_since = asset.missing_since or now
                asset.state = "missing-confirmed" if asset_id in confirmed else "missing-suspected"
        # An item that left the library has nothing left to review.
        await db.execute(
            update(LibraryReadIssue)
            .where(
                LibraryReadIssue.library_id == library_id,
                LibraryReadIssue.resolved_at.is_(None),
                LibraryReadIssue.last_seen_at < published_at,
            )
            .values(resolved_at=now)
        )
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
            capabilities, scope = await client.authorize()
            libraries = await client.libraries()
            seen_by_library, locations, pending = {}, {}, {}
            batched = hasattr(client, "refresh_snapshot")
            for library in libraries:
                if batched:
                    fingerprint, expected = await read_library(
                        client, library["id"], run_id, integration_id, token, generation
                    )
                    pending[library["id"]] = (fingerprint, expected)
                    seen = set(fingerprint)
                else:
                    seen = await collect_library(
                        client, library["id"], run_id, integration_id, token, generation
                    )
                if locations.keys() & seen:
                    raise AdapterError(
                        FailureKind.UNCERTAIN, "An item appeared in multiple libraries."
                    )
                locations.update({item_id: library["id"] for item_id in seen})
                seen_by_library[library["id"]] = seen
            if batched:
                await client.refresh_snapshot()
                for library in libraries:
                    fingerprint, expected = pending[library["id"]]
                    await verify_library(
                        client,
                        library["id"],
                        fingerprint,
                        expected,
                        integration_id,
                        token,
                        generation,
                    )
            for library in libraries:
                seen = seen_by_library[library["id"]]
                # Recheck permissions and library identity before publishing removals.
                _, current_scope = await client.authorize()
                if current_scope != scope:
                    raise AdapterError(
                        FailureKind.PERMISSION,
                        "Account access changed during sync. Run a fresh sync.",
                    )
                await publish_library(
                    client,
                    run_id,
                    library,
                    integration_id,
                    token,
                    generation,
                    scope,
                    seen,
                    locations,
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
                "book_count": len(locations),
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
            operation.payload = {**operation.payload, "review": review}
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
                        if error.kind
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
            await db.execute(
                delete(InventoryObservation).where(InventoryObservation.run_id == run_id)
            )
