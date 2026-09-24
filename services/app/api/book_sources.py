from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.contracts import AdapterError
from app.adapters.source_releases import SourceRelease
from app.adapters.source_releases import release_value as parse_release
from app.api.audiobookbay import resolve as resolve_abb
from app.api.dependencies import CurrentUser, Database, Member
from app.api.metadata import adapter_http_error
from app.api.operations import OperationView
from app.api.prowlarr import resolve as resolve_prowlarr
from app.api.source_artifacts import SourceArtifactView, artifact_view
from app.db.models import AcquisitionIntent, Operation, SourceConnection, SourceResult
from app.domain import release_download_status, series_preparation
from app.domain.book_sources import SearchInput, accessible_work, checked, refresh_search, start
from app.domain.release_profiles import (
    ProfileSnapshot,
    ReleaseAssessment,
    assess_release,
    ranking_key,
)
from app.domain.request_constraints import constrained_preferences
from app.domain.request_preferences import owned_request
from app.domain.source_artifacts import resolve_mam

router = APIRouter(tags=["book-sources"])


class SearchSourceView(BaseModel):
    key: str
    name: str
    state: str
    count: int = 0
    message: str
    has_more: bool = False
    observed_at: datetime | None = None
    query: str | None = None


class SearchQueryEvidence(BaseModel):
    kind: str
    provider: str
    external_id: str
    record_id: UUID
    member_id: str | None = None
    observed_at: datetime


class SearchQueryView(BaseModel):
    key: str
    kind: str
    query: str
    evidence: list[SearchQueryEvidence]


class SearchQueryPlan(BaseModel):
    queries: list[SearchQueryView]
    warnings: list[str]


class RankedReleaseView(BaseModel):
    id: UUID
    release: SourceRelease
    assessment: ReleaseAssessment
    expires_at: datetime
    current_connection: bool
    query_keys: list[str] = Field(default_factory=list)
    download: release_download_status.ReleaseDownloadStatus | None = None


class CatalogPreparationItem(BaseModel):
    external_id: str
    name: str
    state: str
    message: str


class CatalogPreparationView(BaseModel):
    state: str
    message: str
    items: list[CatalogPreparationItem]
    warnings: list[str]


class BookSearchView(BaseModel):
    id: UUID
    work_id: UUID
    request_id: UUID | None = None
    query: str
    query_plan: SearchQueryPlan | None = None
    catalog_preparation: CatalogPreparationView | None = None
    medium: str
    offset: int
    status: str
    message: str
    stale_identity: bool
    profile: ProfileSnapshot
    sources: list[SearchSourceView]
    items: list[RankedReleaseView]
    expires_at: datetime


async def view(db, user, operation_id):
    operation, changed = await refresh_search(db, operation_id, user.id)
    payload = deepcopy(operation.payload)
    preparation = payload.get("catalog_preparation")
    profile = ProfileSnapshot.model_validate(payload["profile"])
    preferences = profile.preferences
    request_id = payload.get("command", {}).get("request_id")
    if request_id:
        intent = await db.get(AcquisitionIntent, UUID(request_id))
        if not intent or intent.owner_id != user.id:
            raise HTTPException(404, "Request not found")
        preferences = constrained_preferences(preferences, intent.specification)
    connections = {s.key: s for s in await db.scalars(select(SourceConnection))}
    rows = list(
        await db.scalars(
            select(SourceResult)
            .where(SourceResult.operation_id == operation.id)
            .order_by(SourceResult.created_at, SourceResult.id)
        )
    )
    downloads = await release_download_status.for_releases(
        db, user.id, UUID(payload["work"]["id"]), [row.release_snapshot for row in rows]
    )
    ranked = []
    work = {**payload["work"], "identifiers": payload.get("identifiers", [])}
    for row in rows:
        release = parse_release(row.source_key, row.release_snapshot)
        connection = connections.get(row.source_key)
        ranked.append(
            RankedReleaseView(
                id=row.id,
                release=release,
                download=downloads.get(release_download_status.identity(release)),
                assessment=assess_release(release, work, preferences, payload["medium"]),
                expires_at=row.expires_at,
                query_keys=row.query_keys if not changed else [],
                current_connection=bool(
                    not changed
                    and connection
                    and connection.enabled
                    and connection.generation == row.source_generation
                    and row.expires_at > datetime.now(UTC)
                ),
            )
        )
    ranked.sort(key=lambda item: ranking_key(item.release, item.assessment, preferences))
    response = BookSearchView(
        id=operation.id,
        work_id=payload["work"]["id"],
        request_id=payload.get("command", {}).get("request_id"),
        query=payload["query"],
        query_plan=payload.get("query_plan")
        if not changed and not (preparation and preparation["state"] in series_preparation.ACTIVE)
        else None,
        catalog_preparation=CatalogPreparationView(
            state=preparation["state"],
            message=preparation["message"],
            warnings=preparation["warnings"],
            items=[
                CatalogPreparationItem(
                    external_id=item["external_id"],
                    name=item["name"],
                    state=item.get(
                        "state",
                        "pending"
                        if preparation["state"] in series_preparation.ACTIVE
                        else "unavailable",
                    ),
                    message=item.get(
                        "message",
                        "Waiting to load series metadata"
                        if preparation["state"] in series_preparation.ACTIVE
                        else "Series metadata was not loaded",
                    ),
                )
                for item in (preparation["dependencies"] or preparation["references"])
            ],
        )
        if preparation and not changed
        else None,
        medium=payload["medium"],
        offset=payload["offset"],
        status=operation.status,
        message=operation.message,
        stale_identity=changed,
        profile=profile,
        sources=[
            SearchSourceView(key=k, **{**v, **({"query": None} if changed else {})})
            for k, v in payload["sources"].items()
        ],
        items=ranked,
        expires_at=payload["expires_at"],
    )
    await db.commit()
    return response


@router.post(
    "/catalog/works/{work_id}/source-searches", response_model=BookSearchView, status_code=202
)
async def begin(
    work_id: UUID,
    body: SearchInput,
    user: CurrentUser,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    operation = await start(db, user, work_id, body, idempotency_key)
    await db.commit()
    return await view(db, user, operation.id)


@router.get("/catalog/works/{work_id}/source-searches/latest", response_model=BookSearchView | None)
async def latest(work_id: UUID, user: CurrentUser, db: Database, request_id: UUID | None = None):
    work = await accessible_work(db, user, work_id)
    filters = []
    if request_id:
        await owned_request(db, user, request_id, work.id)
        filters.append(Operation.payload["command"]["request_id"].astext == str(request_id))
    operation = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == "sources.search",
            Operation.payload["work"]["id"].astext == str(work.id),
            *filters,
        )
        .order_by(Operation.created_at.desc(), Operation.id)
        .limit(1)
    )
    return await view(db, user, operation.id) if operation else None


@router.get("/source-searches/{search_id}", response_model=BookSearchView)
async def search_detail(search_id: UUID, user: CurrentUser, db: Database):
    return await view(db, user, search_id)


@router.post(
    "/source-searches/{search_id}/results/{result_id}/artifact", response_model=SourceArtifactView
)
async def inspect(search_id: UUID, result_id: UUID, user: Member, db: Database):
    operation, changed = await checked(db, search_id, user.id)
    row = await db.get(SourceResult, result_id)
    if not row or row.owner_id != user.id or row.operation_id != operation.id:
        raise HTTPException(404, "Source result not found")
    if changed or row.expires_at <= datetime.now(UTC):
        raise HTTPException(409, "Search results changed or expired. Search again.")
    connection = await db.get(SourceConnection, row.source_key)
    if not connection or not connection.enabled or connection.generation != row.source_generation:
        raise HTTPException(409, "Source connection changed. Search again.")
    if row.source_key == "audiobookbay":
        return await resolve_abb(result_id, user, db)
    if row.source_key == "prowlarr":
        return await resolve_prowlarr(result_id, user, db)
    owner_id, generation, source_id = (
        user.id,
        row.source_generation,
        row.release_snapshot["source_id"],
    )
    await db.rollback()
    try:
        identifier = await resolve_mam(owner_id, source_id, expected_generation=generation)
    except AdapterError as error:
        raise adapter_http_error(error) from error
    return await artifact_view(db, identifier, owner_id)


@router.post(
    "/source-searches/{search_id}/results/{result_id}/download",
    response_model=OperationView,
    status_code=202,
)
async def download_release(
    search_id: UUID,
    result_id: UUID,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=160),
    use_wedge: bool = Query(False),
):
    from app.domain.quick_add import selected_release

    operation = await selected_release(
        db, user, search_id, result_id, idempotency_key, use_wedge=use_wedge
    )
    await db.flush()
    await db.refresh(operation)
    response = OperationView.model_validate(operation)
    await db.commit()
    return response
