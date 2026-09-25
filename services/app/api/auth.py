import asyncio
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.api.dependencies import COOKIE, CurrentUser, Database, client_host, require_origin
from app.config import get_settings
from app.db.models import AuditEvent, LibraryGrant, LoginSession, PermissionRole, RateLimit, User
from app.domain import library_access
from app.domain.permissions import (
    ADMIN,
    AUTOMATE,
    CATALOG,
    MANAGE_USERS,
    PRESETS,
    access_label,
    bits_from_names,
    effective_permissions,
    has,
    names_from_bits,
    preset_bits,
    sync_user_permissions,
    unauthorized_grant,
)
from app.recovery import active_restore, restore_pending
from app.security import (
    csrf_token,
    decrypt_secrets,
    encrypt_secrets,
    hash_password,
    token_hash,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["authentication"])
HANDOFF = "book_handoff"
FINISH = "/api/auth/finish"


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=256)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.lower()


class BootstrapInput(Credentials):
    display_name: str = Field(min_length=1, max_length=120)


class UserInput(Credentials):
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "member", "viewer", "requester", "approver"] = "member"
    permissions: list[str] | None = None
    role_id: UUID | None = None
    library_ids: list[UUID] | None = Field(default=None, max_length=1000)


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    display_name: str
    role: str
    active: bool = True
    can_automate: bool
    permissions: list[str]
    access_label: str
    permission_role_id: str | None = None
    onboarding_status: str = "pending"
    library_ids: list[UUID] | None = None


class AuthView(BaseModel):
    user: UserView
    csrf_token: str
    recovery: bool = False


class SetupView(BaseModel):
    needs_setup: bool


async def named_user_view(db: Database, user: User) -> UserView:
    role_name = None
    if user.permission_role_id:
        role_name = await db.scalar(
            select(PermissionRole.name).where(PermissionRole.id == user.permission_role_id)
        )
    return user_view(user, role_name)


def user_view(user: User, role_name: str | None = None, *, library_ids=None) -> UserView:
    return UserView(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        active=user.active,
        can_automate=user.can_automate,
        permissions=names_from_bits(effective_permissions(user)),
        access_label=access_label(user, role_name),
        permission_role_id=str(user.permission_role_id) if user.permission_role_id else None,
        onboarding_status=(user.onboarding or {}).get("status", "pending"),
        library_ids=library_ids,
    )


def require_user_manager(user: User) -> None:
    if not has(user, MANAGE_USERS):
        raise HTTPException(403, "You cannot manage accounts")


def guard_grant_scope(actor: User, permissions: int, *, current: int | None = None) -> None:
    message = unauthorized_grant(actor, permissions, current=current)
    if message:
        raise HTTPException(403, message)


def guard_admin_target(actor: User, user: User) -> None:
    if user.role == "admin" and actor.role != "admin":
        raise HTTPException(403, "Only an administrator can change an administrator")


async def guard_admin_loss(db: Database, updates: list[tuple[User, int]]) -> None:
    demoted = {
        user.id
        for user, permissions in updates
        if user.role == "admin" and user.active and not permissions & ADMIN
    }
    if not demoted:
        return
    # Held until commit so two administrators cannot both pass the count below.
    await db.execute(text("SELECT pg_advisory_xact_lock(720003)"))
    remaining = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.active.is_(True), User.id.not_in(demoted))
    )
    if not remaining:
        raise HTTPException(409, "Keep at least one active administrator")


async def enforce_auth_budget(db: Database, key: str) -> None:
    now = datetime.now(UTC)
    # One atomic upsert counts concurrent attempts without a separate locking read.
    # Commit independently so failed authentication still consumes its budget.
    resets_at = now + timedelta(minutes=10)
    expired = RateLimit.resets_at <= now
    result = await db.execute(
        insert(RateLimit)
        .values(key=key, count=1, resets_at=resets_at)
        .on_conflict_do_update(
            index_elements=[RateLimit.key],
            set_={
                "count": case((expired, 1), else_=RateLimit.count + 1),
                "resets_at": case((expired, resets_at), else_=RateLimit.resets_at),
            },
        )
        .returning(RateLimit.count, RateLimit.resets_at)
    )
    count, until = result.one()
    await db.commit()
    if count > 15:
        retry_after = max(1, math.ceil((until - datetime.now(UTC)).total_seconds()))
        raise HTTPException(
            429,
            "Too many sign-in attempts. Try again when the sign-in limit resets",
            headers={"Retry-After": str(retry_after)},
        )


async def start_session(user: User, db: Database) -> str:
    settings = get_settings()
    checkpoint = await active_restore(db)
    recovering = await restore_pending(db)
    if recovering and (user.role != "admin" or (checkpoint and checkpoint.operator_id != user.id)):
        raise HTTPException(
            423, "Only the designated recovery operator can sign in during restore review"
        )
    token = secrets.token_urlsafe(48)
    db.add(
        LoginSession(
            token_hash=token_hash(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.session_hours),
        )
    )
    await db.commit()
    return token


def write_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )


def write_handoff(response: Response, token: str) -> None:
    response.set_cookie(
        HANDOFF,
        encrypt_secrets({"token": token}),
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        max_age=60,
        path=FINISH,
    )


async def establish_session(user: User, db: Database, response: Response) -> AuthView:
    token = await start_session(user, db)
    write_session_cookie(response, token)
    return AuthView(
        user=await named_user_view(db, user),
        csrf_token=csrf_token(token),
        recovery=await restore_pending(db),
    )


@router.get("/setup", response_model=SetupView)
async def setup_status(db: Database) -> SetupView:
    return SetupView(needs_setup=not bool(await db.scalar(select(func.count()).select_from(User))))


@router.post("/bootstrap", response_model=AuthView, status_code=201)
async def bootstrap(body: BootstrapInput, request: Request, response: Response, db: Database):
    require_origin(request)
    await enforce_auth_budget(db, "bootstrap")
    encoded = await asyncio.to_thread(hash_password, body.password)
    await db.execute(text("SELECT pg_advisory_xact_lock(720001)"))
    if await db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "Setup is already complete")
    user = User(
        username=body.username,
        display_name=body.display_name,
        password_hash=encoded,
        role="admin",
        can_automate=True,
    )
    sync_user_permissions(user, preset_bits("admin"))
    db.add(user)
    await db.flush()
    db.add(AuditEvent(actor_id=user.id, action="admin.bootstrapped", entity_id=user.id))
    return await establish_session(user, db, response)


@router.post("/login", response_model=AuthView)
async def login(body: Credentials, request: Request, response: Response, db: Database):
    require_origin(request)
    await enforce_auth_budget(db, "ip:" + token_hash(client_host(request)))
    await enforce_auth_budget(db, "login:" + token_hash(body.username))
    user = await db.scalar(select(User).where(User.username == body.username))
    encoded = user.password_hash if user and user.active else None
    await db.rollback()  # Password hashing must not hold a connection/transaction open.
    if not await asyncio.to_thread(verify_password, body.password, encoded):
        raise HTTPException(401, "Username or password is incorrect")
    user = await db.scalar(
        select(User).where(User.username == body.username, User.active.is_(True))
    )
    if not user or user.password_hash != encoded:
        raise HTTPException(401, "Username or password is incorrect")
    return await establish_session(user, db, response)


@router.get("/me", response_model=AuthView)
async def me(request: Request, user: CurrentUser, db: Database):
    return AuthView(
        user=await named_user_view(db, user),
        csrf_token=csrf_token(request.cookies[COOKIE]),
        recovery=await restore_pending(db),
    )


@router.get("/finish")
async def finish(request: Request):
    raw = request.cookies.get(HANDOFF)
    token = None
    if raw:
        try:
            payload = decrypt_secrets(raw)
        except (InvalidToken, ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("token"), str):
            token = payload["token"]
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(
        HANDOFF, path=FINISH, secure=get_settings().cookie_secure, samesite="lax"
    )
    if token:
        write_session_cookie(response, token)
    return response


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, user: CurrentUser, db: Database):
    await db.execute(
        delete(LoginSession).where(LoginSession.token_hash == token_hash(request.cookies[COOKIE]))
    )
    await db.commit()
    response.delete_cookie(COOKIE, path="/", secure=get_settings().cookie_secure, samesite="strict")


@router.get("/users", response_model=list[UserView])
async def users(actor: CurrentUser, db: Database):
    require_user_manager(actor)
    roles = {role.id: role.name for role in (await db.scalars(select(PermissionRole))).all()}
    grants: dict[UUID, list[UUID]] = {}
    if actor.role == "admin":
        for user_id, library_id in await db.execute(
            select(LibraryGrant.user_id, LibraryGrant.library_id).order_by(LibraryGrant.library_id)
        ):
            grants.setdefault(user_id, []).append(library_id)
    return [
        user_view(
            user,
            roles.get(user.permission_role_id),
            library_ids=grants.get(user.id, []) if actor.role == "admin" else None,
        )
        for user in (await db.scalars(select(User).order_by(User.username))).all()
    ]


@router.post("/users", response_model=UserView, status_code=201)
async def create_user(body: UserInput, actor: CurrentUser, db: Database):
    require_user_manager(actor)
    permissions = (
        bits_from_names(body.permissions)
        if body.permissions is not None
        else preset_bits(body.role)
    )
    actor_id = actor.id
    await db.rollback()
    encoded = await asyncio.to_thread(hash_password, body.password)
    # Serialize account creation to turn a duplicate into a stable API conflict.
    await db.execute(text("SELECT pg_advisory_xact_lock(720002)"))
    actor = await db.get(User, actor_id, populate_existing=True)
    if not actor or not actor.active:
        raise HTTPException(403, "Account management access changed")
    require_user_manager(actor)
    role = await db.get(PermissionRole, body.role_id) if body.role_id else None
    if body.role_id and not role:
        raise HTTPException(404, "Role not found")
    if role:
        permissions = int(role.permissions)
    guard_grant_scope(actor, permissions)
    if body.library_ids is not None and actor.role != "admin":
        raise HTTPException(403, "Only administrators can change library access")
    if await db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, "That username is already in use")
    user = User(
        username=body.username,
        display_name=body.display_name,
        password_hash=encoded,
        role="member",
    )
    sync_user_permissions(user, permissions, role.id if role else None)
    db.add(user)
    await db.flush()
    libraries = None
    if body.library_ids is not None:
        libraries = await library_access.replace_user_libraries(
            db, actor, user, body.library_ids, []
        )
    db.add(AuditEvent(actor_id=actor_id, action="user.created", entity_id=user.id))
    await db.commit()
    return user_view(user, role.name if role else None, library_ids=libraries)


class AutomationPermissionInput(BaseModel):
    allowed: bool
    expected_allowed: bool


@router.put("/users/{user_id}/automation", response_model=UserView)
async def automation_permission(
    user_id: UUID, body: AutomationPermissionInput, actor: CurrentUser, db: Database
):
    require_user_manager(actor)
    rows = {
        u.id: u
        for u in await db.scalars(
            select(User)
            .where(User.id.in_([actor.id, user_id]))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    actor = rows.get(actor.id)
    if not actor or not actor.active or not has(actor, MANAGE_USERS):
        raise HTTPException(403, "Administrator access changed")
    user = rows.get(user_id)
    if not user:
        raise HTTPException(404, "Account not found")
    if user.role != "member":
        raise HTTPException(422, "List automation grants apply to member accounts")
    guard_admin_target(actor, user)
    if body.allowed and not user.can_automate and not has(actor, AUTOMATE):
        raise HTTPException(403, "You can only grant permissions you already have")
    if user.can_automate != body.expected_allowed:
        raise HTTPException(409, "This permission changed; reload the account")
    user.can_automate = body.allowed
    if user.permissions is not None:
        user.permissions = (
            int(user.permissions) | AUTOMATE if body.allowed else int(user.permissions) & ~AUTOMATE
        )
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="user.automation.changed",
            entity_id=user.id,
            detail={"allowed": body.allowed},
        )
    )
    await db.commit()
    return user_view(user)


class PermissionInfo(BaseModel):
    name: str
    label: str
    description: str
    group: str


class PresetInfo(BaseModel):
    id: str
    label: str
    description: str
    permissions: list[str]


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    permissions: list[str] = Field(min_length=1)


class RoleView(BaseModel):
    id: str
    name: str
    description: str
    permissions: list[str]


class AccessCatalog(BaseModel):
    permissions: list[PermissionInfo]
    presets: list[PresetInfo]
    roles: list[RoleView]


class PermissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permissions: list[str]
    role_id: UUID | None = None
    expected_permissions: list[str]
    expected_role_id: UUID | None = None
    library_ids: list[UUID] | None = Field(default=None, max_length=1000)
    expected_library_ids: list[UUID] | None = Field(default=None, max_length=1000)


def role_view(role: PermissionRole) -> RoleView:
    return RoleView(
        id=str(role.id),
        name=role.name,
        description=role.description,
        permissions=names_from_bits(int(role.permissions)),
    )


@router.get("/access", response_model=AccessCatalog)
async def access_catalog(actor: CurrentUser, db: Database):
    require_user_manager(actor)
    roles = (await db.scalars(select(PermissionRole).order_by(PermissionRole.name))).all()
    return AccessCatalog(
        permissions=[
            PermissionInfo(name=name, label=label, description=description, group=group)
            for name, _bit, label, group, description in CATALOG
        ],
        presets=[
            PresetInfo(
                id=key, label=label, description=description, permissions=names_from_bits(bits)
            )
            for key, label, description, bits in PRESETS
        ],
        roles=[role_view(role) for role in roles],
    )


@router.post("/roles", response_model=RoleView, status_code=201)
async def create_role(body: RoleInput, actor: CurrentUser, db: Database):
    require_user_manager(actor)
    permissions = bits_from_names(body.permissions)
    guard_grant_scope(actor, permissions)
    name = body.name.strip()
    if await db.scalar(
        select(PermissionRole.id).where(func.lower(PermissionRole.name) == name.lower())
    ):
        raise HTTPException(409, "A role with that name already exists")
    role = PermissionRole(name=name, description=body.description.strip(), permissions=permissions)
    db.add(role)
    await db.flush()
    db.add(AuditEvent(actor_id=actor.id, action="role.created", entity_id=role.id))
    await db.commit()
    return role_view(role)


@router.put("/roles/{role_id}", response_model=RoleView)
async def update_role(role_id: UUID, body: RoleInput, actor: CurrentUser, db: Database):
    require_user_manager(actor)
    permissions = bits_from_names(body.permissions)
    role = await db.get(PermissionRole, role_id, with_for_update=True)
    if not role:
        raise HTTPException(404, "Role not found")
    name = body.name.strip()
    if await db.scalar(
        select(PermissionRole.id).where(
            func.lower(PermissionRole.name) == name.lower(),
            PermissionRole.id != role.id,
        )
    ):
        raise HTTPException(409, "A role with that name already exists")
    members = list(
        await db.scalars(select(User).where(User.permission_role_id == role.id).with_for_update())
    )
    if any(user.role == "admin" for user in members):
        guard_admin_target(actor, next(user for user in members if user.role == "admin"))
    guard_grant_scope(actor, permissions, current=int(role.permissions))
    await guard_admin_loss(db, [(user, permissions) for user in members])
    role.name, role.description, role.permissions = name, body.description.strip(), permissions
    for user in members:
        sync_user_permissions(user, permissions, role.id)
    db.add(AuditEvent(actor_id=actor.id, action="role.updated", entity_id=role.id))
    await db.commit()
    return role_view(role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: UUID, actor: CurrentUser, db: Database):
    require_user_manager(actor)
    role = await db.get(PermissionRole, role_id)
    if not role:
        raise HTTPException(404, "Role not found")
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="role.deleted",
            entity_id=role.id,
            detail={"name": role.name},
        )
    )
    await db.delete(role)
    await db.commit()


@router.put("/users/{user_id}/permissions", response_model=UserView)
async def update_permissions(
    user_id: UUID, body: PermissionInput, actor: CurrentUser, db: Database
):
    require_user_manager(actor)
    if body.library_ids is not None:
        await library_access.lock_access(db)
    rows = {
        user.id: user
        for user in await db.scalars(
            select(User)
            .where(User.id.in_([actor.id, user_id]))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    actor = rows.get(actor.id)
    if not actor or not actor.active or not has(actor, MANAGE_USERS):
        raise HTTPException(403, "Administrator access changed")
    user = rows.get(user_id)
    if not user:
        raise HTTPException(404, "Account not found")
    guard_admin_target(actor, user)
    if set(body.expected_permissions) != set(names_from_bits(effective_permissions(user))):
        raise HTTPException(409, "These permissions changed; reload the account")
    if (
        "expected_role_id" in body.model_fields_set
        and body.expected_role_id != user.permission_role_id
    ):
        raise HTTPException(
            409, "The assigned role changed. Reload the saved settings to review it."
        )
    role = None
    if body.role_id:
        role = await db.get(PermissionRole, body.role_id)
        if not role:
            raise HTTPException(404, "Role not found")
        permissions = int(role.permissions)
    else:
        permissions = bits_from_names(body.permissions)
    guard_grant_scope(actor, permissions, current=effective_permissions(user))
    await guard_admin_loss(db, [(user, permissions)])
    libraries = None
    if body.library_ids is not None:
        libraries = await library_access.replace_user_libraries(
            db, actor, user, body.library_ids, body.expected_library_ids
        )
    # Disabled users expose no effective permissions. A library-only edit must
    # not turn their saved role/permissions into the empty effective set.
    if (
        user.active
        or body.permissions != body.expected_permissions
        or body.role_id != user.permission_role_id
    ):
        sync_user_permissions(user, permissions, role.id if role else None)
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="user.permissions.changed",
            entity_id=user.id,
            detail={
                "permissions": names_from_bits(int(user.permissions or 0)),
                "role_id": str(role.id) if role else None,
            },
        )
    )
    await db.commit()
    return user_view(user, role.name if role else None, library_ids=libraries)
