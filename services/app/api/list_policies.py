from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.dependencies import Database, Member
from app.api.requests import TargetView
from app.domain import list_monitoring
from app.domain import list_policies as policies
from app.domain.acquisition import RequestOptions, RequestSpec
from app.domain.automatic_routes import AutomaticRoutes
from app.domain.list_requests import owner_context
from app.domain.list_series import SeriesPlanView
from app.domain.release_profiles import PreferenceOverrides, ProfileSnapshot
from app.domain.request_constraints import DownloadConstraints

router = APIRouter(prefix="/lists/{list_id}/acquisition", tags=["list-policies"])


class PolicyConfiguration(BaseModel):
    mode: str
    specification: RequestSpec
    scope_options: RequestOptions | None = None
    preference_overrides: PreferenceOverrides | None = None
    profile: ProfileSnapshot
    downloader_id: UUID | None
    downloader_generation: int | None
    routes: dict[str, policies.PolicyRoute]
    alternate_downloader_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    alternate_downloader_generation: int | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    alternate_routes: dict[str, policies.PolicyRoute] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    route_options: AutomaticRoutes | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    request_constraints: DownloadConstraints | None = None


class ListPolicyView(BaseModel):
    id: UUID
    revision: int
    generation: int
    active: bool
    configuration: PolicyConfiguration
    baseline_at: datetime
    message: str
    counts: dict[str, int]


class ActivationRecord(BaseModel):
    work_id: UUID
    title: str
    targets: list[TargetView]
    selected: bool
    series_scope: SeriesPlanView | None = None


class ActivationView(BaseModel):
    id: UUID
    status: str
    message: str
    expires_at: datetime
    configuration: PolicyConfiguration
    records: list[ActivationRecord]
    counts: dict[str, int] = Field(default_factory=dict)
    total: int
    selected: int
    offset: int
    limit: int


class RevisionInput(BaseModel):
    expected_revision: int = Field(ge=1)


class MonitoredBook(BaseModel):
    id: UUID
    work_id: UUID
    title: str
    state: str
    message: str
    intent_id: UUID | None
    next_check_at: datetime | None
    series_request_id: UUID | None = None
    series_external_id: str | None = None
    series_scope_issue: SeriesPlanView | None = None


class MonitoringPage(BaseModel):
    items: list[MonitoredBook]
    total: int
    offset: int
    limit: int


async def view(db, policy):
    rows = await list_monitoring.projection(db, policy)
    counts = dict(
        (
            await db.execute(
                select(rows.c.state, func.count())
                .where(rows.c.position == 1)
                .group_by(rows.c.state)
            )
        ).all()
    )
    return ListPolicyView(
        id=policy.id,
        revision=policy.revision,
        generation=policy.generation,
        active=policy.active,
        configuration=policy.configuration,
        baseline_at=policy.baseline_at,
        message=policy.message,
        counts=counts,
    )


def preview_view(operation, offset=0, limit=50):
    data = operation.payload
    return ActivationView(
        id=operation.id,
        status=operation.status,
        message=operation.message,
        expires_at=data["expires_at"],
        configuration=data["configuration"],
        records=data["records"][offset : offset + limit],
        total=len(data["records"]),
        counts=data.get("counts", {}),
        selected=sum(r["selected"] for r in data["records"]),
        offset=offset,
        limit=limit,
    )


@router.get("", response_model=ListPolicyView | None)
async def detail(list_id: UUID, user: Member, db: Database):
    await owner_context(db, user.id, list_id)
    policy = await policies.current_policy(db, list_id)
    return await view(db, policy) if policy else None


@router.post("/preview", response_model=ActivationView, status_code=201)
async def preview(
    list_id: UUID,
    body: policies.ListPolicyInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    operation = await policies.preview(db, user, list_id, body, idempotency_key)
    result = preview_view(operation)
    await db.commit()
    return result


@router.get("/previews/{identifier}", response_model=ActivationView)
async def saved_preview(
    list_id: UUID,
    identifier: UUID,
    user: Member,
    db: Database,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    return preview_view(await policies.owned_preview(db, user, list_id, identifier), offset, limit)


@router.post("/previews/{identifier}/activate", response_model=ListPolicyView)
async def activate(list_id: UUID, identifier: UUID, user: Member, db: Database):
    operation = await policies.owned_preview(db, user, list_id, identifier)
    policy = await policies.activate(db, user, operation)
    result = await view(db, policy)
    await db.commit()
    return result


@router.post("/pause", response_model=ListPolicyView)
async def pause(list_id: UUID, body: RevisionInput, user: Member, db: Database):
    policy = await policies.pause(db, user, list_id, body.expected_revision)
    result = await view(db, policy)
    await db.commit()
    return result


@router.get("/books", response_model=MonitoringPage)
async def books(
    list_id: UUID,
    user: Member,
    db: Database,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    await owner_context(db, user.id, list_id)
    policy = await policies.current_policy(db, list_id)
    if not policy:
        return MonitoringPage(items=[], total=0, offset=offset, limit=limit)
    rows = await list_monitoring.projection(db, policy)
    page = (
        (
            await db.execute(
                select(rows)
                .where(rows.c.position == 1)
                .order_by(rows.c.created_at.desc(), rows.c.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    total = await db.scalar(select(func.count()).select_from(rows).where(rows.c.position == 1))
    return MonitoringPage(
        items=[
            MonitoredBook(
                **row,
                **{
                    key: row["progress"].get(key)
                    for key in ("series_request_id", "series_external_id", "series_scope_issue")
                },
            )
            for row in page
        ],
        total=total,
        offset=offset,
        limit=limit,
    )
