"""Resolve missing catalog editions before automatic import, outside file transactions."""

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from app.adapters.catalog_providers import Hardcover, OpenLibrary
from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    AuditEvent,
    AutomaticImport,
    CatalogAccount,
    DownloadAttempt,
    DownloadInspection,
    Operation,
    Version,
    WorkMetadataSource,
)
from app.db.session import session_factory
from app.domain import download_reviews
from app.domain.catalog_metadata import attach_source, preferences
from app.domain.catalog_network import CatalogGateway
from app.domain.catalog_titles import display_title, identity_authors, parse_title_labels
from app.domain.hardcover_matching import MatchEvidence as BookEvidence
from app.domain.hardcover_matching import compatible
from app.domain.identity import normalized, work_key
from app.domain.operations import transaction_lock
from app.domain.work_graph import canonical_work, family_ids, graph_lock
from app.importing.grouping import current_grouping
from app.importing.match_evidence import MatchEvidence
from app.importing.matching import candidate_evidence
from app.importing.naming import fingerprint
from app.jobs.queue import enqueue
from app.jobs.retry import CatalogRetry
from app.security import decrypt_secrets

TERMINAL = {"completed", "failed", "needs-review", "cancelled"}
MAX_EDITION_PAGES = 10


async def context(db, row, group_key, *, lock=False):
    from app.importing.automatic import check_policy

    if lock:
        await download_reviews.lock_principals(db, row.inspection_id)
    await check_policy(db, row, lock=lock)
    attempt = await db.get(DownloadAttempt, row.attempt_id)
    selection = await db.get(AcquisitionSelection, attempt.selection_id)
    requester, _, _ = await download_reviews.requester_authority(db, selection)
    inspection = await db.get(DownloadInspection, row.inspection_id)
    await download_reviews.validate_inspection(db, inspection.id, lock=lock)
    if inspection.state != "ready":
        raise HTTPException(409, "Inspection is no longer ready")
    grouping_revision, grouping = await current_grouping(db, inspection)
    if not any(group.key == group_key for group in grouping.groups):
        raise HTTPException(409, "File grouping changed")
    work = await canonical_work(db, UUID(selection.frozen["origin_work_id"]))
    if lock:
        await db.refresh(work, with_for_update=True)
        await transaction_lock(db, "metadata-preferences")
        await transaction_lock(db, f"catalog-account:{requester.id}")
    if work.metadata_fields.get("identity_rejected") or not work_key(work.title, work.authors):
        raise HTTPException(409, "Book identity needs review")
    settings = await preferences(db)
    if not settings.automatic_edition_lookup:
        raise HTTPException(409, "Automatic metadata lookup is disabled")
    account = await db.get(CatalogAccount, requester.id, populate_existing=True)
    sources = list(
        await db.scalars(
            select(WorkMetadataSource)
            .where(WorkMetadataSource.work_id.in_(family_ids(work.id)))
            .order_by(WorkMetadataSource.id)
        )
    )
    inputs = {
        "work_id": str(work.id),
        "title": work.title,
        "authors": work.authors,
        "language": work.language,
        "group_key": group_key,
        "inspection_revision": inspection.snapshot["revision"],
        "grouping_revision": grouping_revision,
        "requester_id": str(requester.id),
        "requirements": selection.frozen["requirements"],
        "account_generation": account.generation if account and account.enabled else None,
        "settings": settings.model_dump(mode="json"),
        "endpoints": [get_settings().hardcover_url, get_settings().openlibrary_url],
        "sources": [
            {
                "provider": source.provider,
                "external_id": source.external_id,
                "accepted": source.accepted,
                "revision": fingerprint(source.snapshot),
            }
            for source in sources
        ],
    }
    return inputs, requester, work, account


async def pending(db, row):
    identifier = row.evidence.get("catalog_resolution")
    if not identifier:
        return False
    operation = await db.get(Operation, UUID(identifier), populate_existing=True)
    if operation.status in TERMINAL:
        return False
    job_status = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": operation.job_id},
    )
    if job_status in {"todo", "doing"}:
        return True
    operation.status = "failed"
    operation.message = "Catalog resolution stopped; use administrator file review"
    return False


async def schedule(db, row, matches):
    if row.evidence.get("catalog_resolution"):
        return await pending(db, row)
    candidates = [
        match
        for match in matches
        if match.status != "matched"
        and match.evidence.identifiers
        and not match.evidence.issues
        and not match.truncated
        and not any(candidate.identifier_match for candidate in match.candidates)
    ]
    if len(candidates) != 1:
        return False
    match = candidates[0]
    try:
        inputs, requester, work, _ = await context(db, row, match.group_key)
    except HTTPException:
        return False
    facts = match.evidence
    if {display_title(title) for title in facts.titles} != {display_title(work.title)} or (
        facts.authors != [sorted(normalized(name) for name in work.authors)]
    ):
        return False
    inspection = await db.get(DownloadInspection, row.inspection_id)
    _, grouping = await current_grouping(db, inspection)
    medium = next(group.medium for group in grouping.groups if group.key == match.group_key)
    if medium != inputs["requirements"]["medium"]:
        return False
    operation = Operation(
        owner_id=requester.id,
        kind="metadata.resolve-import",
        idempotency_key=f"import-catalog:{row.id}",
        message="Looking up the edition for this completed download",
        payload={
            "automatic_id": str(row.id),
            "inputs": inputs,
            "facts": facts.model_dump(mode="json"),
            "medium": medium,
        },
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, "metadata.resolve-import", operation_id=str(operation.id))
    row.evidence = {**row.evidence, "catalog_resolution": str(operation.id)}
    row.message = "Looking up missing catalog edition details before import"
    return True


def matching_editions(book, facts, medium):
    work = SimpleNamespace(
        id=UUID(int=0),
        title=book.title,
        authors=book.authors,
        language=book.language,
        metadata_fields={},
    )
    result = []
    for edition in book.editions:
        if edition.medium != medium:
            continue
        version = Version(
            id=UUID(int=1),
            work_id=work.id,
            medium=edition.medium,
            title=edition.title,
            language=edition.language,
            narrators=edition.narrators,
            abridged=edition.abridged,
            publication_year=edition.publication_year,
            identifiers=edition.identifiers,
        )
        candidate = candidate_evidence(facts, version, work, work, False)
        if candidate.identifier_match and not candidate.conflicts:
            result.append(edition)
    return result


async def provider_lookup(provider, inputs, facts, medium, token):
    scope = (
        f"{inputs['requester_id']}:{inputs['account_generation']}"
        if provider == "hardcover"
        else "public"
    )
    async with CatalogGateway(
        provider, scope, token if provider == "hardcover" else None
    ) as gateway:
        adapter = (
            Hardcover(gateway.request) if provider == "hardcover" else OpenLibrary(gateway.request)
        )
        try:
            async with asyncio.timeout(90):
                evidence = BookEvidence(title=inputs["title"], authors=inputs["authors"])
                linked = [
                    source["external_id"]
                    for source in inputs["sources"]
                    if source["provider"] == provider and source["accepted"]
                ]
                if not linked:
                    kept = identity_authors(inputs["authors"])[0] or inputs["authors"]
                    query = " ".join([parse_title_labels(inputs["title"]).title, *kept])
                    page = await adapter.search(query, 1)
                    if page.has_more:
                        return None, "needs-review", "Catalog search needs disambiguation"
                    linked = list(
                        {book.external_id for book in page.items if compatible(evidence, book)}
                    )
                if len(linked) != 1:
                    return (
                        None,
                        "needs-review" if linked else "completed",
                        "No unique catalog work was found",
                    )
                if any(
                    source["provider"] == provider
                    and source["external_id"] == linked[0]
                    and not source["accepted"]
                    for source in inputs["sources"]
                ):
                    return None, "needs-review", "This catalog match was explicitly rejected"
                book = await adapter.fetch(linked[0])
                if not compatible(evidence, book) or (
                    book.canonical_id and book.canonical_id != book.external_id
                ):
                    return None, "needs-review", "Catalog work identity needs review"
                expected = work_key(book.title, book.authors)
                editions = {edition.external_id: edition for edition in book.editions}
                more = book.editions_more
                for page_number in range(1, MAX_EDITION_PAGES):
                    if not more:
                        break
                    page = await adapter.fetch(book.external_id, page_number * 50)
                    if work_key(page.title, page.authors) != expected or (
                        page.canonical_id and page.canonical_id != page.external_id
                    ):
                        return (
                            None,
                            "needs-review",
                            "Catalog identity changed during edition lookup",
                        )
                    for edition in page.editions:
                        if edition.external_id in editions:
                            return (
                                None,
                                "needs-review",
                                "Catalog edition pages overlap; review the incomplete lookup",
                            )
                        editions[edition.external_id] = edition
                    more = page.editions_more
                if gateway.stale:
                    raise AdapterError(
                        FailureKind.UNAVAILABLE, "Catalog lookup returned stale data"
                    )
                if more:
                    return (
                        None,
                        "needs-review",
                        "This catalog exceeds the automatic edition lookup limit",
                    )
                book = book.model_copy(
                    update={"editions": list(editions.values()), "editions_more": False}
                )
                matches = matching_editions(book, facts, medium)
                if len(matches) != 1:
                    return (
                        None,
                        "needs-review" if matches else "completed",
                        "No unique compatible catalog edition was found",
                    )
                return book, "completed", "Catalog edition resolved from provider and file evidence"
        except TimeoutError as error:
            raise AdapterError(FailureKind.TIMEOUT, "Catalog edition lookup timed out") from error
        except AdapterError as error:
            if error.kind == FailureKind.PARSER:
                await gateway.invalidate()
            raise


async def lookup(inputs, facts, medium, token):
    available = (["hardcover"] if token else []) + (["openlibrary"] if medium == "ebook" else [])
    available.sort(key=lambda provider: provider != inputs["settings"]["primary"])
    last_error = None
    for provider in available:
        try:
            result = await provider_lookup(provider, inputs, facts, medium, token)
            if result[0] or result[1] == "needs-review":
                return result
        except AdapterError as error:
            last_error = error
    if last_error:
        raise last_error
    return None, "needs-review", "No compatible edition was found; review catalog matching"


async def resolve(operation_id):
    token = uuid4().hex
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id)
        if not operation or operation.status in TERMINAL:
            return
        identifier = UUID(operation.payload["automatic_id"])
        await transaction_lock(db, f"automatic-import:{identifier}")
        await db.refresh(operation)
        if operation.status in TERMINAL:
            return
        row = await db.get(AutomaticImport, identifier)
        try:
            inputs, _, _, account = await context(db, row, operation.payload["inputs"]["group_key"])
            if row.state != "inspecting" or inputs != operation.payload["inputs"]:
                raise HTTPException(409, "Lookup authority or evidence changed")
        except HTTPException:
            operation.status, operation.message = (
                "cancelled",
                "Catalog lookup authority or evidence changed; review this import",
            )
            await resume(db, row)
            return
        attempts = operation.payload.get("attempts", 0) + 1
        if attempts > 5:
            operation.status, operation.message = (
                "failed",
                "Catalog lookup exhausted its retry budget",
            )
            await resume(db, row)
            return
        operation.payload = {**operation.payload, "run_token": token, "attempts": attempts}
        operation.status = "running"
        credential = (
            decrypt_secrets(account.encrypted_token)["token"]
            if account and account.enabled
            else None
        )
        facts, medium = (
            MatchEvidence.model_validate(operation.payload["facts"]),
            operation.payload["medium"],
        )
    failure = None
    try:
        book, status, message = await lookup(inputs, facts, medium, credential)
    except Exception as error:
        failure, book = error, None
        retryable = not isinstance(error, AdapterError) or error.kind in {
            FailureKind.TIMEOUT,
            FailureKind.ROUTE,
            FailureKind.UNAVAILABLE,
            FailureKind.RATE_LIMIT,
        }
        status = "retrying" if retryable and attempts < 5 else "failed"
        message = (
            "Catalog unavailable; retry scheduled"
            if status == "retrying"
            else "Catalog lookup failed; use file review"
        )
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"automatic-import:{identifier}")
        operation = await db.get(Operation, operation_id, populate_existing=True)
        if operation.payload.get("run_token") != token:
            return
        row = await db.get(AutomaticImport, identifier)
        try:
            await transaction_lock(db, f"inspection-plan:{row.inspection_id}")
            await graph_lock(db)
            current, requester, work, _ = await context(db, row, inputs["group_key"], lock=True)
            if row.state != "inspecting" or current != inputs:
                raise HTTPException(409, "Lookup authority or evidence changed")
            if book:
                async with db.begin_nested():
                    await attach_source(db, work, book)
                    db.add(
                        AuditEvent(
                            actor_id=requester.id,
                            action="metadata.import.resolved",
                            entity_id=work.id,
                            detail={"provider": book.provider, "operation_id": str(operation_id)},
                        )
                    )
        except HTTPException:
            status, message = (
                "needs-review",
                "Catalog lookup authority or identity changed; review this import",
            )
        operation.status, operation.message = status, message
        if status in TERMINAL:
            await resume(db, row)
    if failure and status == "retrying":
        raise CatalogRetry(getattr(failure, "retry_after", None)) from None


async def resume(db, row):
    if row.state == "inspecting":
        operation = await db.get(Operation, row.operation_id)
        operation.job_id = await enqueue(db, "organization.automatic", automatic_id=str(row.id))
