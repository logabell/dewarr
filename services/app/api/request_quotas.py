"""Quota policy administration and self-service usage."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.dependencies import Admin, CurrentUser, Database
from app.db.models import AuditEvent, PermissionRole, RequestQuotaPolicy, User
from app.domain.operations import transaction_lock
from app.domain.request_quotas import LOCK, Rules, Usage, usage

router = APIRouter(prefix="/request-quotas", tags=["request-quotas"])


class PolicyView(BaseModel):
    scope: str
    rules: Rules


@router.get("/me", response_model=Usage)
async def mine(user: CurrentUser, db: Database):
    return await usage(db, user)


@router.get("/users", response_model=list[Usage])
async def users(
    admin: Admin, db: Database, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    rows = await db.scalars(select(User).order_by(User.username).offset(offset).limit(limit))
    return [await usage(db, user) for user in rows]


@router.get("", response_model=list[PolicyView])
async def policies(admin: Admin, db: Database):
    return [
        PolicyView(scope=row.scope, rules=Rules.model_validate(row.configuration))
        for row in await db.scalars(select(RequestQuotaPolicy).order_by(RequestQuotaPolicy.scope))
    ]


async def validate_scope(db, scope):
    if scope in {"installation", "role:admin", "role:member", "role:viewer"}:
        return
    kind, _, identifier = scope.partition(":")
    try:
        identifier = UUID(identifier)
    except ValueError:
        raise HTTPException(422, "Choose installation, role:<id>, or user:<id>") from None
    model = {"role": PermissionRole, "user": User}.get(kind)
    if not model or not await db.get(model, identifier):
        raise HTTPException(404, "Quota owner not found")


@router.put("/{scope}", response_model=PolicyView)
async def save(scope: str, body: Rules, admin: Admin, db: Database):
    await transaction_lock(db, LOCK)
    await validate_scope(db, scope)
    row = await db.get(RequestQuotaPolicy, scope)
    if row is None:
        row = RequestQuotaPolicy(scope=scope)
        db.add(row)
    row.configuration = body.model_dump(mode="json")
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="request.quota.updated",
            detail={"scope": scope, "rules": row.configuration},
        )
    )
    await db.commit()
    return PolicyView(scope=scope, rules=body)


@router.delete("/{scope}", status_code=204)
async def inherit(scope: str, admin: Admin, db: Database):
    await transaction_lock(db, LOCK)
    row = await db.get(RequestQuotaPolicy, scope)
    if row:
        await db.delete(row)
        db.add(
            AuditEvent(actor_id=admin.id, action="request.quota.inherited", detail={"scope": scope})
        )
    await db.commit()
