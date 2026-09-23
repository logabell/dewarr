"""Bounded secondary catalog lookup, with no database transaction across provider I/O."""

import asyncio
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from app.adapters.catalog_providers import OpenLibrary
from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import AuditEvent, Operation, User, Work, WorkMetadataSource
from app.db.session import session_factory
from app.domain.catalog_metadata import attach_source, preferences
from app.domain.catalog_network import CatalogGateway
from app.domain.catalog_titles import identity_authors, parse_title_labels
from app.domain.corrections import revision
from app.domain.hardcover_matching import MatchEvidence, compatible
from app.domain.identity import work_key
from app.domain.visibility import visible_work
from app.domain.work_graph import family_ids
from app.jobs.queue import enqueue
from app.jobs.retry import CatalogRetry

TERMINAL = {"completed", "cancelled", "needs-review", "failed"}
FIELDS = ("description", "publication_year", "cover_url")


async def effective_status(db, operation):
    if operation.status in TERMINAL:
        return operation.status
    status = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id = :id"),
        {"id": operation.job_id},
    )
    if status in {"todo", "doing"}:
        return operation.status
    # The worker commits completion before its queue acknowledgement. Re-read to avoid
    # reporting a failure if that commit landed between the first read and queue check.
    await db.refresh(operation)
    return operation.status if operation.status in TERMINAL else "failed"


async def proposal(db, work, settings):
    fields = work.metadata_fields.get("fields", {})
    missing = [
        field
        for field in FIELDS
        if getattr(work, field) in (None, "") and not fields.get(field, {}).get("locked")
    ]
    if (
        not settings.automatic_enrichment
        or settings.primary != "hardcover"
        or work.redirect_to
        or work.metadata_fields.get("identity_rejected")
        or not work_key(work.title, work.authors)
        or not missing
    ):
        return None
    sources = (
        await db.scalars(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id.in_(family_ids(work.id)))
        )
    ).all()
    # A rejected secondary match is a durable decision, not a prompt to try another alias.
    if any(source.provider == "openlibrary" for source in sources):
        return None
    primary = [source for source in sources if source.provider == "hardcover" and source.accepted]
    if len(primary) != 1:
        return None
    source = primary[0]
    return {
        "work_id": str(work.id),
        "source_id": str(source.id),
        "source_revision": revision(source.snapshot),
        "identity": work_key(work.title, work.authors),
        "settings": settings.model_dump(mode="json"),
        "missing": missing,
    }


async def schedule_enrichment(db, user, work, *, retry=False):
    if get_settings().recovery_mode:
        return None
    # Callers already hold the work lock; also enforce it for domain callers.
    work = await db.get(Work, work.id, with_for_update=True, populate_existing=True)
    plan = await proposal(db, work, await preferences(db))
    if not plan:
        return None
    key = "enrichment:" + revision(plan)
    previous = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == "metadata.enrich",
            Operation.payload["plan_key"].astext == key,
        )
        .order_by(Operation.created_at.desc(), Operation.id)
        .limit(1)
    )
    if previous:
        status = await effective_status(db, previous)
        if status == "failed" and previous.status not in TERMINAL:
            previous.status = "failed"
            previous.message = "The metadata worker ended before completion; retry the lookup"
        if not retry or previous.status not in TERMINAL:
            return previous
    operation = Operation(
        owner_id=user.id,
        kind="metadata.enrich",
        idempotency_key=key + (":" + uuid4().hex if retry else ""),
        payload={**plan, "plan_key": key},
        message="Waiting to check Open Library for missing book details",
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, "metadata.enrich", operation_id=str(operation.id))
    await db.flush()
    await db.refresh(operation)
    return operation


async def lookup(title, authors, language):
    async with CatalogGateway("openlibrary", "public") as gateway:
        adapter = OpenLibrary(gateway.request)
        try:
            async with asyncio.timeout(60):
                # Quoted field queries avoid turning a title's punctuation into query operators.
                def phrase(value):
                    return '"' + value.replace("\\", " ").replace('"', " ") + '"'

                evidence = MatchEvidence(title=title, authors=authors, language=language)
                search_title = parse_title_labels(title).title
                search_author = (identity_authors(authors)[0] or authors)[0]
                page = await adapter.search(
                    f"title:{phrase(search_title)} author:{phrase(search_author)}", 1
                )
                candidates = {
                    item.external_id: item for item in page.items if compatible(evidence, item)
                }
                if gateway.stale:
                    raise AdapterError(FailureKind.UNAVAILABLE, "Secondary catalog is unavailable")
                # One page is the automatic lookup budget. An incomplete search cannot prove
                # uniqueness; leave further disambiguation to the existing explicit-match UI.
                if page.has_more or len(candidates) > 1:
                    return None, "needs-review", "Several possible matches need a catalog review"
                if not candidates:
                    return None, "completed", "No confident secondary catalog match was found"
                book = await adapter.fetch(next(iter(candidates)))
                if gateway.stale:
                    raise AdapterError(FailureKind.UNAVAILABLE, "Secondary catalog is unavailable")
                if not compatible(evidence, book) or (
                    book.canonical_id and book.canonical_id != book.external_id
                ):
                    return None, "needs-review", "Secondary book details need an explicit match"
                return book, "completed", "Missing metadata checked against Open Library"
        except TimeoutError as error:
            raise AdapterError(FailureKind.TIMEOUT, "Secondary catalog lookup timed out") from error
        except AdapterError as error:
            if error.kind == FailureKind.PARSER:
                await gateway.invalidate()
            raise


async def valid_target(db, operation):
    user = await db.get(User, operation.owner_id)
    if not user or not user.active or user.role == "viewer":
        return None
    work = await db.scalar(
        select(Work)
        .where(Work.id == UUID(operation.payload["work_id"]), visible_work(user))
        .with_for_update()
    )
    if not work:
        return None
    current = await proposal(db, work, await preferences(db))
    if not current:
        return None
    # Manual description/cover edits may reduce missing fields without invalidating the
    # identity decision. The resolver preserves those locks when the lookup completes.
    for field in ("source_id", "source_revision", "identity", "settings"):
        if current[field] != operation.payload[field]:
            return None
    return work


async def enrich(operation_id):
    token = uuid4().hex
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id, with_for_update=True)
        if not operation or operation.kind != "metadata.enrich" or operation.status in TERMINAL:
            return
        if get_settings().recovery_mode:
            operation.status, operation.message = "cancelled", "Metadata lookup paused for recovery"
            return
        work = await valid_target(db, operation)
        if not work:
            operation.status, operation.message = (
                "cancelled",
                "Book access, metadata or preferences changed; lookup was not applied",
            )
            return
        attempts = operation.payload.get("attempts", 0) + 1
        if attempts > 5:
            operation.status, operation.message = (
                "failed",
                "Metadata lookup needs an explicit retry",
            )
            return
        operation.payload = {**operation.payload, "run_token": token, "attempts": attempts}
        operation.status, operation.message = "running", "Checking Open Library for missing details"
        title, authors, language = work.title, work.authors, work.language

    error = None
    try:
        book, status, message = await lookup(title, authors, language)
    except Exception as failure:
        error, book = failure, None
        retryable = not isinstance(failure, AdapterError) or failure.kind in {
            FailureKind.TIMEOUT,
            FailureKind.ROUTE,
            FailureKind.UNAVAILABLE,
            FailureKind.RATE_LIMIT,
        }
        status = "retrying" if retryable and attempts < 5 else "failed"
        message = (
            "Secondary catalog unavailable; a bounded retry is scheduled"
            if status == "retrying"
            else "Secondary lookup could not finish. Existing metadata was preserved"
        )

    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id, with_for_update=True)
        # Concurrent/redelivered read-only lookups may finish out of order. Only the latest
        # claimant can apply its result; a crash after this commit becomes a no-op on retry.
        if not operation or operation.payload.get("run_token") != token:
            return
        work = await valid_target(db, operation)
        if not work or get_settings().recovery_mode:
            operation.status, operation.message = (
                "cancelled",
                "Book access, metadata or preferences changed; lookup was not applied",
            )
            return
        if book:
            try:
                async with db.begin_nested():
                    await attach_source(db, work, book)
            except HTTPException:
                status, message = "needs-review", "The secondary match needs a catalog review"
            else:
                db.add(
                    AuditEvent(
                        actor_id=operation.owner_id,
                        action="metadata.enriched",
                        entity_id=work.id,
                        detail={"provider": book.provider, "operation_id": str(operation.id)},
                    )
                )
        operation.status, operation.message = status, message
    if error and status == "retrying":
        # Do not include provider payloads or arbitrary exception text in worker logs.
        raise CatalogRetry(getattr(error, "retry_after", None)) from None
