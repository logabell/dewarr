"""Durable, owner-scoped source searches with independently persisted source outcomes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text

from app.adapters.audiobookbay import ABBSearch
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.mam import MAMSearch
from app.adapters.prowlarr import ProwlarrSearch
from app.adapters.source_releases import SOURCE_NAMES
from app.config import get_settings
from app.db.models import Operation, SourceArtifact, SourceConnection, SourceResult, User, Work
from app.db.session import session_factory
from app.domain import series_preparation, source_queries
from app.domain.audiobookbay_network import abb_call
from app.domain.operations import transaction_lock
from app.domain.prowlarr_network import prowlarr_call
from app.domain.release_profiles import PreferenceOverrides
from app.domain.request_preferences import for_intent, owned_request
from app.domain.source_network import source_call
from app.domain.visibility import visible_work
from app.domain.work_graph import canonical_work
from app.jobs.queue import enqueue
from app.jobs.retry import SourceSearchRetry
from app.security import encrypt_secrets

MAX_INDEXERS = 20


class SearchInput(BaseModel):
    q: str | None = Field(default=None, min_length=1, max_length=300)
    medium: str = Field(default="all", pattern="^(all|ebook|audio)$")
    profile_id: UUID | None = None
    profile_generation: int | None = Field(default=None, ge=0)
    profile_effective_revision: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )
    request_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    preference_overrides: PreferenceOverrides = Field(
        default_factory=PreferenceOverrides, exclude_if=lambda value: not value.model_fields_set
    )
    offset: int = Field(default=0, ge=0, le=10000)


async def accessible_work(db, user, identifier):
    canonical = await canonical_work(db, identifier)
    work = await db.scalar(select(Work).where(Work.id == canonical.id, visible_work(user)))
    if not work:
        raise HTTPException(404, "Book not found")
    return work


def identity(work):
    return {"id": str(work.id), "title": work.title, "authors": work.authors}


async def start(db, user, work_id, body, key, *, pack_origin=None, only_sources=None):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Source searches are paused for recovery")
    work = await accessible_work(db, user, work_id)
    command = {"work_id": str(work.id), **body.model_dump(mode="json")}
    if pack_origin:
        command["pack_origin"] = pack_origin
    await transaction_lock(db, f"operation:{user.id}:{key}")
    existing = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if existing:
        if existing.kind != "sources.search" or existing.payload.get("command") != command:
            raise HTTPException(409, "This search command was already used for different options")
        return existing
    intent = await owned_request(db, user, body.request_id, work.id) if body.request_id else None
    profile = await for_intent(db, user, intent, body)
    query = (body.q if body.q is not None else source_queries.default_query(work)).strip()
    if not query:
        raise HTTPException(422, "Enter a source-search query")
    identifiers = await source_queries.edition_identifiers(db, work, body.medium)
    query_plan = await source_queries.plan(db, user, work, query, profile.preferences.search_series)
    if pack_origin:
        # Accepted children inspect the already selected artifact. A tracker
        # outage or disappearance of its listing must not force another search
        # or torrent fetch for bytes we already have permission to use.
        from app.domain.pack_expansion import require_origin

        await require_origin(db, user.id, pack_origin)
        artifact = await db.get(SourceArtifact, UUID(pack_origin["artifact_id"]))
        if (
            not artifact
            or artifact.owner_id != user.id
            or artifact.sha256 != pack_origin["artifact_sha256"]
        ):
            raise HTTPException(409, "The selected pack artifact is no longer available")
        expiry = datetime.now(UTC) + timedelta(minutes=25)
        operation = Operation(
            owner_id=user.id,
            kind="sources.search",
            idempotency_key=key,
            status="completed",
            message="Using the original selected pack; no new source query",
            payload={
                "command": command,
                "work": identity(work),
                "query": query,
                "identifiers": identifiers,
                "query_plan": query_plan,
                "medium": body.medium,
                "offset": 0,
                "profile": profile.model_dump(mode="json"),
                "sources": {
                    artifact.source_key: {
                        "state": "completed",
                        "name": "Selected pack",
                        "count": 1,
                        "message": "Saved source evidence; not a refreshed tracker observation",
                        "generation": artifact.source_generation,
                    }
                },
                "workers": {},
                "expires_at": expiry.isoformat(),
            },
        )
        db.add(operation)
        await db.flush()
        db.add(
            SourceResult(
                owner_id=user.id,
                operation_id=operation.id,
                source_key=artifact.source_key,
                source_generation=artifact.source_generation,
                release_snapshot=artifact.release_snapshot,
                encrypted_reference=encrypt_secrets({}),
                expires_at=expiry,
            )
        )
        await db.flush()
        return operation
    connections = {
        s.key: s
        for s in await db.scalars(
            select(SourceConnection).where(SourceConnection.enabled.is_(True))
        )
    }
    sources = {
        key: {
            "state": "queued",
            "name": SOURCE_NAMES[key],
            "count": 0,
            "message": "Waiting for a worker",
            "generation": row.generation,
        }
        for key, row in connections.items()
        if key in SOURCE_NAMES and (only_sources is None or key in only_sources)
    }
    for native in ("mam", "audiobookbay"):
        if native not in sources:
            continue
        sources[native].update(query_key="book", query=query)
        for term in query_plan["queries"][1:]:
            sources[native + ":" + term["key"]] = {
                **sources[native],
                "query_key": term["key"],
                "query": term["query"],
            }
    operation = Operation(
        owner_id=user.id,
        kind="sources.search",
        idempotency_key=key,
        payload={
            "command": command,
            "work": identity(work),
            "query": query,
            "identifiers": identifiers,
            "query_plan": query_plan,
            "medium": body.medium,
            "offset": body.offset,
            "profile": profile.model_dump(mode="json"),
            "sources": sources,
            "workers": {},
            "expires_at": (datetime.now(UTC) + timedelta(minutes=25)).isoformat(),
        },
        message="Searching connected sources"
        if sources
        else "Connect MAM, AudiobookBay, Prowlarr, or Soulseek to search releases",
        status="queued" if sources else "completed",
    )
    preparation = (
        await series_preparation.plan(db, user, work, profile.preferences.allows_series_packs)
        if sources
        else None
    )
    if preparation:
        operation.payload = {**operation.payload, "catalog_preparation": preparation}
    db.add(operation)
    await db.flush()
    if preparation and preparation["state"] in series_preparation.ACTIVE:
        operation.message = preparation["message"]
        operation.job_id = await enqueue(db, series_preparation.KIND, search_id=str(operation.id))
    else:
        await enqueue_sources(db, operation)
    return operation


async def enqueue_sources(db, operation):
    payload = deepcopy(operation.payload)
    for source in sorted(payload["sources"].keys() & SOURCE_NAMES.keys()):
        job = await enqueue(db, "sources.search", operation_id=str(operation.id), source=source)
        payload["workers"][source] = {"job_id": job, "attempts": 0}
        if operation.job_id is None:
            operation.job_id = job
    operation.payload = payload


async def launch(db, operation, user, work):
    """Freeze the final query evidence only after prerequisite catalog observation."""
    payload = deepcopy(operation.payload)
    profile = payload["profile"]["preferences"]
    plan = await source_queries.plan(
        db, user, work, payload["query"], profile.get("search_series", True)
    )
    payload["query_plan"] = plan
    roots = {key: value for key, value in payload["sources"].items() if key in SOURCE_NAMES}
    payload["sources"] = roots
    for native in ("mam", "audiobookbay"):
        if native not in roots:
            continue
        roots[native].update(query_key="book", query=payload["query"])
        for term in plan["queries"][1:]:
            roots[native + ":" + term["key"]] = {
                **roots[native],
                "query_key": term["key"],
                "query": term["query"],
            }
    payload["expires_at"] = (datetime.now(UTC) + timedelta(minutes=25)).isoformat()
    operation.payload = payload
    operation.job_id = None
    await enqueue_sources(db, operation)
    refresh_status(operation, operation.payload)


async def checked(db, identifier, user_id=None):
    operation = await db.get(Operation, identifier, populate_existing=True)
    if (
        not operation
        or operation.kind != "sources.search"
        or (user_id and operation.owner_id != user_id)
    ):
        raise HTTPException(404, "Source search not found")
    user = await db.get(User, operation.owner_id, populate_existing=True)
    if not user or not user.active:
        raise HTTPException(401, "This search account is no longer active")
    work = await accessible_work(db, user, UUID(operation.payload["work"]["id"]))
    changed = identity(work) != operation.payload["work"]
    preparation = operation.payload.get("catalog_preparation")
    if "query_plan" in operation.payload and not (
        preparation and preparation["state"] in series_preparation.ACTIVE
    ):
        current = await source_queries.plan(
            db,
            user,
            work,
            operation.payload["query"],
            operation.payload["profile"]["preferences"].get("search_series", True),
        )
        changed = changed or not source_queries.same_scope(current, operation.payload["query_plan"])
    return operation, changed


def refresh_status(operation, payload):
    preparation = payload.get("catalog_preparation")
    if preparation and preparation["state"] in series_preparation.ACTIVE:
        operation.status, operation.message = "running", preparation["message"]
        operation.payload = payload
        return
    states = [s["state"] for s in payload["sources"].values()]
    operation.status = (
        "completed" if all(s in {"completed", "failed"} for s in states) else "running"
    )
    operation.message = (
        ("Search finished with source errors" if "failed" in states else "Source search completed")
        if operation.status == "completed"
        else "Searching connected sources"
    )
    operation.payload = payload


async def update_unit(identifier, source, token, unit, changes, hits=None, generation=None):
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"source-search:{identifier}")
        operation, changed = await checked(db, identifier)
        payload = deepcopy(operation.payload)
        if payload["workers"][source].get("token") != str(token):
            return False
        payload["workers"][source]["until"] = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
        if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
            raise HTTPException(409, "Search expired. Start a new source search.")
        if changed:
            raise HTTPException(409, "Catalog identity changed. Start a new source search.")
        if generation is not None:
            await transaction_lock(db, f"source:{source}")
            connection = await db.get(SourceConnection, source, populate_existing=True)
            if not connection or not connection.enabled or connection.generation != generation:
                raise HTTPException(409, "Source settings changed. Start a new search.")
        if hits is not None:
            now = datetime.now(UTC)
            await db.execute(delete(SourceResult).where(SourceResult.expires_at <= now))
            hits = list(
                {
                    (release.source, release.indexer_id, release.source_id): (release, reference)
                    for release, reference in hits
                }.values()
            )
            changes = {**changes, "count": len(hits)}
            existing = {
                (
                    r.release_snapshot["source"],
                    r.release_snapshot.get("indexer_id"),
                    r.release_snapshot["source_id"],
                ): r
                for r in await db.scalars(
                    select(SourceResult).where(SourceResult.operation_id == operation.id)
                )
            }
            query_key = payload["sources"].get(unit, {}).get("query_key", "book")
            for release, reference in hits:
                key = (release.source, release.indexer_id, release.source_id)
                if key in existing:
                    row = existing[key]
                    row.query_keys = sorted(set(row.query_keys) | {query_key})
                    continue
                db.add(
                    SourceResult(
                        owner_id=operation.owner_id,
                        source_key=source,
                        source_generation=generation,
                        operation_id=operation.id,
                        expires_at=datetime.fromisoformat(payload["expires_at"]),
                        encrypted_reference=encrypt_secrets({"link": reference}),
                        release_snapshot=release.model_dump(mode="json"),
                        query_keys=[query_key],
                    )
                )
        payload["sources"][unit] = {**payload["sources"].get(unit, {}), **changes}
        refresh_status(operation, payload)
        return True


async def fail_worker(identifier, source, token, message):
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"source-search:{identifier}")
        operation = await db.get(Operation, identifier)
        if not operation:
            return
        payload = deepcopy(operation.payload)
        if payload["workers"][source].get("token") != str(token):
            return
        for key, unit in payload["sources"].items():
            if (key == source or key.startswith(source + ":")) and unit["state"] not in {
                "completed",
                "failed",
            }:
                unit.update(state="failed", message=message)
        payload["workers"][source].pop("token", None)
        refresh_status(operation, payload)


async def run(identifier, source):
    if get_settings().recovery_mode:
        raise SourceSearchRetry(60)
    token = uuid4()
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"source-search:{identifier}")
        operation = await db.get(Operation, identifier, populate_existing=True)
        if (
            not operation
            or operation.kind != "sources.search"
            or source not in operation.payload["workers"]
        ):
            return
        payload = deepcopy(operation.payload)
        unit_states = [
            unit["state"]
            for key, unit in payload["sources"].items()
            if key == source or key.startswith(source + ":")
        ]
        if all(state in {"completed", "failed"} for state in unit_states):
            return
        worker = payload["workers"][source]
        if worker.get("token") and datetime.fromisoformat(worker["until"]) > datetime.now(UTC):
            raise SourceSearchRetry(
                max(
                    1,
                    int(
                        (
                            datetime.fromisoformat(worker["until"]) - datetime.now(UTC)
                        ).total_seconds()
                    )
                    + 1,
                )
            )
        worker.update(
            token=str(token),
            until=(datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
            attempts=worker["attempts"] + 1,
        )
        operation.payload = payload
        owner_id = operation.owner_id
    try:
        async with session_factory()() as db:
            _, changed = await checked(db, identifier)
            if changed:
                raise HTTPException(409, "Catalog identity changed. Start a new search.")
        if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
            raise HTTPException(409, "Search expired. Start a new search.")
        generation = payload["sources"][source]["generation"]
        if source == "slskd":
            from app.domain.slskd_connection import search as slskd_search

            try:
                if not await update_unit(
                    identifier,
                    source,
                    token,
                    source,
                    {"state": "running", "message": "Searching Soulseek"},
                ):
                    return
                releases, generation = await slskd_search(
                    owner_id,
                    {
                        "q": payload["sources"][source].get("query", payload["query"]),
                        "title": payload["work"]["title"],
                        "authors": payload["work"]["authors"],
                        "observed_at": datetime.now(UTC),
                    },
                    expected_generation=generation,
                )
                if not await update_unit(
                    identifier,
                    source,
                    token,
                    source,
                    {
                        "state": "completed",
                        "message": "Results received",
                        "has_more": False,
                        "observed_at": datetime.now(UTC).isoformat(),
                    },
                    [(release, None) for release in releases],
                    generation,
                ):
                    return
            except AdapterError as error:
                if error.kind == FailureKind.RATE_LIMIT:
                    raise
                await update_unit(
                    identifier, source, token, source, {"state": "failed", "message": str(error)}
                )
            return
        if source in {"mam", "audiobookbay"}:
            for unit, state in payload["sources"].items():
                if not (unit == source or unit.startswith(source + ":")) or state["state"] in {
                    "completed",
                    "failed",
                }:
                    continue
                try:
                    if not await update_unit(
                        identifier,
                        source,
                        token,
                        unit,
                        {"state": "running", "message": "Searching " + SOURCE_NAMES[source]},
                    ):
                        return
                    if source == "audiobookbay":
                        if payload["medium"] == "ebook":
                            await update_unit(
                                identifier,
                                source,
                                token,
                                unit,
                                {
                                    "state": "completed",
                                    "message": "AudiobookBay supplies audiobooks only",
                                    "has_more": False,
                                },
                                [],
                                generation,
                            )
                            continue
                        page, generation = await abb_call(
                            owner_id,
                            "search",
                            ABBSearch(
                                q=state.get("query", payload["query"]),
                                page=payload["offset"] // 50 + 1,
                            ),
                            expected_generation=generation,
                        )
                    else:
                        page, generation = await source_call(
                            owner_id,
                            "search",
                            MAMSearch(
                                q=state.get("query", payload["query"]),
                                medium=payload["medium"],
                                language_ids=[],
                                offset=payload["offset"],
                                limit=50,
                            ),
                            with_generation=True,
                            expected_generation=generation,
                        )
                    if not await update_unit(
                        identifier,
                        source,
                        token,
                        unit,
                        {
                            "state": "completed",
                            "message": "Results received",
                            "has_more": page.has_more,
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                        [(release, None) for release in page.items],
                        generation,
                    ):
                        return
                except AdapterError as error:
                    if error.kind == FailureKind.RATE_LIMIT:
                        raise
                    if not await update_unit(
                        identifier, source, token, unit, {"state": "failed", "message": str(error)}
                    ):
                        return
        else:
            if payload["sources"][source]["state"] != "completed":
                indexers, generation = await prowlarr_call(
                    owner_id, "indexers", expected_generation=generation
                )
                eligible = [
                    i
                    for i in indexers
                    if i.enabled
                    and i.supports_search
                    and not i.excluded
                    and (not i.categories or bool(set(i.categories) & {3000, 3030, 7000, 7020}))
                ]
                async with session_factory()() as db, db.begin():
                    await transaction_lock(db, f"source-search:{identifier}")
                    operation, changed = await checked(db, identifier)
                    current = deepcopy(operation.payload)
                    if current["workers"][source].get("token") != str(token):
                        return
                    if changed:
                        raise HTTPException(409, "Catalog identity changed. Start a new search.")
                    queries = current.get("query_plan", {}).get("queries") or [
                        {"key": "book", "query": current["query"]}
                    ]
                    for indexer in eligible[:MAX_INDEXERS]:
                        for term in queries:
                            suffix = "" if term["key"] == "book" else ":" + term["key"]
                            current["sources"][f"prowlarr:{indexer.id}{suffix}"] = {
                                "state": "queued",
                                "name": indexer.name,
                                "generation": generation,
                                "indexer_id": indexer.id,
                                "paging": indexer.supports_pagination,
                                "count": 0,
                                "message": "Waiting to search",
                                "query_key": term["key"],
                                "query": term["query"],
                            }
                    current["sources"][source].update(
                        state="completed",
                        message="Indexer discovery complete"
                        if len(eligible) <= MAX_INDEXERS
                        else (
                            f"Search limited to {MAX_INDEXERS} indexers; "
                            "use direct source search for others"
                        ),
                    )
                    refresh_status(operation, current)
                    payload = current
            for unit, state in payload["sources"].items():
                if not unit.startswith("prowlarr:") or state["state"] in {"completed", "failed"}:
                    continue
                try:
                    if payload["offset"] and not state["paging"]:
                        raise AdapterError(
                            FailureKind.UNSUPPORTED, "This indexer does not support further pages"
                        )
                    if not await update_unit(
                        identifier,
                        source,
                        token,
                        unit,
                        {"state": "running", "message": "Searching this indexer"},
                    ):
                        return
                    batch, generation = await prowlarr_call(
                        owner_id,
                        "search",
                        ProwlarrSearch(
                            q=state.get("query", payload["query"]),
                            medium=payload["medium"],
                            indexer_id=state["indexer_id"],
                            offset=payload["offset"],
                            limit=50,
                        ),
                        expected_generation=generation,
                    )
                    await update_unit(
                        identifier,
                        source,
                        token,
                        unit,
                        {
                            "state": "completed",
                            "count": len(batch.hits),
                            "has_more": batch.returned_count >= 50 and state["paging"],
                            "message": (
                                "Results received; empty responses can also "
                                "indicate upstream failure"
                            ),
                            "observed_at": datetime.now(UTC).isoformat(),
                        },
                        [(h.release, h.reference) for h in batch.hits],
                        generation,
                    )
                except AdapterError as error:
                    if error.kind == FailureKind.RATE_LIMIT:
                        raise
                    await update_unit(
                        identifier, source, token, unit, {"state": "failed", "message": str(error)}
                    )
    except AdapterError as error:
        if error.kind == FailureKind.RATE_LIMIT and worker["attempts"] < 5:
            async with session_factory()() as db, db.begin():
                await transaction_lock(db, f"source-search:{identifier}")
                operation = await db.get(Operation, identifier)
                current = deepcopy(operation.payload)
                if current["workers"][source].get("token") != str(token):
                    return
                current["workers"][source].pop("token", None)
                for key, state in current["sources"].items():
                    if (key == source or key.startswith(source + ":")) and state["state"] in {
                        "queued",
                        "running",
                    }:
                        state.update(state="queued", message="Waiting for source rate limit")
                refresh_status(operation, current)
            raise SourceSearchRetry(error.retry_after or 5) from error
        await fail_worker(identifier, source, token, str(error))
    except HTTPException as error:
        await fail_worker(identifier, source, token, str(error.detail))


async def refresh_search(db, operation_id, user_id):
    await transaction_lock(db, f"source-search:{operation_id}")
    operation, changed = await checked(db, operation_id, user_id)
    await series_preparation.repair(db, operation)
    payload = deepcopy(operation.payload)
    # Queue truth repairs exhausted workers; do not infer failure from slow polling.
    for source, worker in payload["workers"].items():
        status = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": worker["job_id"]},
        )
        if status in {"failed", "aborted", "succeeded"} or status is None:
            for key, unit in payload["sources"].items():
                if (key == source or key.startswith(source + ":")) and unit["state"] not in {
                    "completed",
                    "failed",
                }:
                    unit.update(
                        state="failed",
                        message="Search worker stopped. Start a new search to retry.",
                    )
                    worker.pop("token", None)
    refresh_status(operation, payload)
    return operation, changed
