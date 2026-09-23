from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Identity as SQLIdentity,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Identity, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member', 'viewer')"),
        Index(
            "users_email_key",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )
    username: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(20), default="member")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    can_automate: Mapped[bool] = mapped_column(Boolean, default=False)
    # Null keeps the legacy role preset. Explicit bits are the Seerr-style grant set.
    permissions: Mapped[int | None] = mapped_column(BigInteger)
    permission_role_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("permission_roles.id", ondelete="SET NULL")
    )
    onboarding: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=lambda: {"status": "pending", "step": 0, "skipped": []},
        server_default='{"status":"pending","step":0,"skipped":[]}',
    )


class PermissionRole(Identity, Base):
    __tablename__ = "permission_roles"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    permissions: Mapped[int] = mapped_column(BigInteger)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OidcIdentity(Base):
    __tablename__ = "oidc_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    issuer: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OidcProvider(Base):
    __tablename__ = "oidc_provider"
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("match_existing IN ('off', 'email', 'username')"),
        CheckConstraint("default_role IN ('member', 'viewer')"),
        CheckConstraint("signing_algorithm IN ('RS256', 'ES256')"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(80), default="your identity provider")
    issuer: Mapped[str] = mapped_column(String(300), default="")
    authorization_endpoint: Mapped[str] = mapped_column(Text, default="")
    token_endpoint: Mapped[str] = mapped_column(Text, default="")
    userinfo_endpoint: Mapped[str] = mapped_column(Text, default="")
    jwks_uri: Mapped[str] = mapped_column(Text, default="")
    client_id: Mapped[str] = mapped_column(String(200), default="")
    encrypted_secret: Mapped[str | None] = mapped_column(Text)
    signing_algorithm: Mapped[str] = mapped_column(String(20), default="RS256")
    match_existing: Mapped[str] = mapped_column(String(20), default="off")
    auto_register: Mapped[bool] = mapped_column(Boolean, default=False)
    default_role: Mapped[str] = mapped_column(String(20), default="member")
    group_claim: Mapped[str] = mapped_column(String(80), default="")
    group_scope: Mapped[str] = mapped_column(String(80), default="")
    admin_group: Mapped[str] = mapped_column(String(120), default="")
    member_group: Mapped[str] = mapped_column(String(120), default="")
    viewer_group: Mapped[str] = mapped_column(String(120), default="")


class PlexIdentity(Base):
    __tablename__ = "plex_identities"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    plex_user_id: Mapped[str] = mapped_column(String(20), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlexLogin(Base):
    __tablename__ = "plex_login"
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("default_role IN ('member', 'viewer')"),
        CheckConstraint("client_id <> ''"),
        CheckConstraint("NOT enabled OR machine_id <> ''"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    client_id: Mapped[str] = mapped_column(String(36))
    machine_id: Mapped[str] = mapped_column(String(80), default="")
    server_name: Mapped[str] = mapped_column(String(120), default="")
    auto_register: Mapped[bool] = mapped_column(Boolean, default=False)
    default_role: Mapped[str] = mapped_column(String(20), default="member")


class RestoreCheckpoint(Identity, Base):
    __tablename__ = "restore_checkpoints"
    __table_args__ = (
        Index(
            "uq_restore_checkpoint_active", "active", unique=True, postgresql_where=text("active")
        ),
    )
    operator_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    backup_id: Mapped[UUID] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)


class RecoveryQueueFence(Base):
    __tablename__ = "recovery_queue_fences"
    checkpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("restore_checkpoints.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    job_id_through: Mapped[int] = mapped_column(BigInteger)
    job_count: Mapped[int] = mapped_column(BigInteger)
    subject_counts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    approval_version: Mapped[int] = mapped_column(Integer, server_default="0")


class RecoveryQueueSubject(Base):
    __tablename__ = "recovery_queue_subjects"
    __table_args__ = (Index("ix_recovery_queue_subject_lookup", "kind", "subject_id"),)
    checkpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("restore_checkpoints.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    # Intentionally no subject FK: deleting ordinary history must not erase this boundary.
    subject_id: Mapped[UUID] = mapped_column(primary_key=True)


class RateLimit(Base):
    __tablename__ = "rate_limits"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecoveryScan(Identity, Base):
    __tablename__ = "recovery_scans"
    __table_args__ = (CheckConstraint("state IN ('queued', 'running', 'completed', 'held')"),)
    checkpoint_id: Mapped[UUID] = mapped_column(ForeignKey("restore_checkpoints.id"), index=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    context_digest: Mapped[str | None] = mapped_column(String(64))
    run_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class RecoveryFinding(Identity, Base):
    __tablename__ = "recovery_findings"
    __table_args__ = (UniqueConstraint("scan_id", "position"),)
    scan_id: Mapped[UUID] = mapped_column(
        ForeignKey("recovery_scans.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    domain: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(600))
    message: Mapped[str] = mapped_column(String(600))
    entity_id: Mapped[UUID | None] = mapped_column()
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Work(Identity, Base):
    __tablename__ = "works"
    __table_args__ = (
        CheckConstraint("redirect_to IS NULL OR redirect_to != id", name="work_redirect_not_self"),
    )
    title: Mapped[str] = mapped_column(String(600), index=True)
    authors: Mapped[list[str]] = mapped_column(JSONB, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(20))
    cover_url: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(Integer)
    provisional: Mapped[bool] = mapped_column(Boolean, default=True)
    redirect_to: Mapped[UUID | None] = mapped_column(ForeignKey("works.id"), index=True)
    metadata_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    catalog_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    catalog_owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    match_key: Mapped[str | None] = mapped_column(String(64), index=True)


class CatalogSeries(Identity, Base):
    __tablename__ = "catalog_series"
    __table_args__ = (UniqueConstraint("owner_id", "provider", "external_id"),)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(600))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))


class SeriesMembership(Identity, Base):
    __tablename__ = "series_memberships"
    __table_args__ = (UniqueConstraint("series_id", "external_id"),)
    series_id: Mapped[UUID] = mapped_column(
        ForeignKey("catalog_series.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(200))
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    present: Mapped[bool] = mapped_column(Boolean, default=True)


class Version(Identity, Base):
    __tablename__ = "versions"
    __table_args__ = (
        CheckConstraint("medium IN ('ebook', 'audio', 'print', 'unknown')", name="version_medium"),
        CheckConstraint(
            "recording_kind IS NULL OR recording_kind IN ('narrated', 'dramatized', 'full_cast')",
            name="version_recording_kind",
        ),
    )
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    medium: Mapped[str] = mapped_column(String(10))
    title: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(20))
    narrators: Mapped[list[str]] = mapped_column(JSONB, default=list)
    abridged: Mapped[bool | None] = mapped_column(Boolean)
    publication_year: Mapped[int | None] = mapped_column(Integer)
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # How an audio version was recorded. Parts of one dramatization share a version.
    recording_kind: Mapped[str | None] = mapped_column(String(20))


class Representation(Identity, Base):
    __tablename__ = "representations"
    version_id: Mapped[UUID] = mapped_column(ForeignKey("versions.id"), index=True)
    format: Mapped[str] = mapped_column(String(40))
    technical: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ProviderObject(Identity, Base):
    __tablename__ = "provider_objects"
    __table_args__ = (UniqueConstraint("provider", "kind", "external_id"),)
    provider: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(300))
    work_id: Mapped[UUID | None] = mapped_column(ForeignKey("works.id"), index=True)
    version_id: Mapped[UUID | None] = mapped_column(ForeignKey("versions.id"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    match_status: Mapped[str] = mapped_column(String(30), default="unresolved")
    manual_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_source_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("work_metadata_sources.id"), index=True
    )
    pending_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Integration(Identity, Base):
    __tablename__ = "integrations"
    kind: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    base_url: Mapped[str] = mapped_column(Text)
    encrypted_secrets: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(40), default="untested")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_generation: Mapped[int] = mapped_column(Integer, default=0)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceConnection(Base):
    __tablename__ = "source_connections"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    base_url: Mapped[str] = mapped_column(Text)
    proxy_url: Mapped[str | None] = mapped_column(Text)
    encrypted_secrets: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="untested")
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    automation_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class AcquisitionDefaults(Base):
    __tablename__ = "acquisition_defaults"
    __table_args__ = (
        CheckConstraint(
            "(key = 'installation' AND owner_id IS NULL) OR "
            "(owner_id IS NOT NULL AND key = 'user:' || owner_id::text)"
        ),
    )
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AcquisitionProfile(Identity, Base):
    __tablename__ = "acquisition_profiles"
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB)


class SourceResult(Identity, Base):
    __tablename__ = "source_results"
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(40), ForeignKey("source_connections.key"))
    source_generation: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    encrypted_reference: Mapped[str] = mapped_column(Text)
    release_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    query_keys: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")


class SourceArtifact(Identity, Base):
    __tablename__ = "source_artifacts"
    __table_args__ = (
        UniqueConstraint("owner_id", "source_key", "source_id", "source_generation", "sha256"),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    source_key: Mapped[str] = mapped_column(ForeignKey("source_connections.key"))
    source_id: Mapped[str] = mapped_column(String(200))
    source_generation: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    encrypted_content: Mapped[str] = mapped_column(Text)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSONB)
    release_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Library(Identity, Base):
    __tablename__ = "libraries"
    __table_args__ = (UniqueConstraint("integration_id", "external_id"),)
    integration_id: Mapped[UUID] = mapped_column(ForeignKey("integrations.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    last_complete_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    accessible: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    scope_fingerprint: Mapped[str | None] = mapped_column(String(64))


class LibraryGrant(Base):
    __tablename__ = "library_grants"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    library_id: Mapped[UUID] = mapped_column(ForeignKey("libraries.id"), primary_key=True)


class LibraryAsset(Identity, Base):
    __tablename__ = "library_assets"
    __table_args__ = (
        UniqueConstraint("library_id", "external_id", "medium"),
        CheckConstraint("medium IN ('ebook', 'audio')"),
    )
    library_id: Mapped[UUID] = mapped_column(ForeignKey("libraries.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    version_id: Mapped[UUID | None] = mapped_column(ForeignKey("versions.id"))
    medium: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(40), default="stale")
    full_content: Mapped[bool] = mapped_column(Boolean, default=False)
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_generation: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str | None] = mapped_column(String(600))
    match_status: Mapped[str] = mapped_column(
        String(40), default="unresolved", server_default="unresolved"
    )
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    containment: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    read_issues: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")


class LibraryReadIssue(Identity, Base):
    """A backend item Dewarr could not read well enough to record as a library asset."""

    __tablename__ = "library_read_issues"
    __table_args__ = (UniqueConstraint("library_id", "external_id"),)
    library_id: Mapped[UUID] = mapped_column(ForeignKey("libraries.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(600))
    authors: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    path: Mapped[str | None] = mapped_column(Text)
    reasons: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetContains(Base):
    __tablename__ = "asset_contains"
    __table_args__ = (
        CheckConstraint(
            "(part_index IS NULL AND part_total IS NULL)"
            " OR (part_index BETWEEN 1 AND part_total AND part_total BETWEEN 2 AND 20)",
            name="asset_contains_part",
        ),
    )
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("library_assets.id"), primary_key=True)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), primary_key=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # This item is part N of M of the book, not the whole book.
    part_index: Mapped[int | None] = mapped_column(Integer)
    part_total: Mapped[int | None] = mapped_column(Integer)


class PartCombine(Identity, Base):
    """Folding the separate part items of one recording into one library book."""

    __tablename__ = "part_combines"
    __table_args__ = (
        UniqueConstraint("library_id", "version_id"),
        CheckConstraint(
            "state IN ('skipped', 'combining', 'combined', 'separating', 'separated',"
            " 'needs-attention')",
            name="part_combines_state",
        ),
    )
    library_id: Mapped[UUID] = mapped_column(ForeignKey("libraries.id"), index=True)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("versions.id"), index=True)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"))
    state: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(500))
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))
    destination_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_destinations.id"))
    part_asset_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    combined_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("library_assets.id"))
    # Frozen folder plan and progress. The recovery journal itself lives in private staging.
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BookList(Identity, Base):
    __tablename__ = "book_lists"
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)


class ListEntry(Identity, Base):
    __tablename__ = "list_entries"
    __table_args__ = (UniqueConstraint("list_id", "work_id"),)
    list_id: Mapped[UUID] = mapped_column(ForeignKey("book_lists.id", ondelete="CASCADE"))
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    locally_added: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class ListAcquisitionPolicy(Identity, Base):
    __tablename__ = "list_acquisition_policies"
    list_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("book_lists.id", ondelete="SET NULL"), unique=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    baseline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    message: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))


class ListAcquisitionBook(Identity, Base):
    __tablename__ = "list_acquisition_books"
    __table_args__ = (UniqueConstraint("policy_id", "work_id"),)
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("list_acquisition_policies.id"))
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"))
    generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="baseline")
    message: Mapped[str] = mapped_column(Text)
    intent_id: Mapped[UUID | None] = mapped_column(ForeignKey("acquisition_intents.id"))
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ListCsvImport(Identity, Base):
    __tablename__ = "list_csv_imports"
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    list_id: Mapped[UUID] = mapped_column(
        ForeignKey("book_lists.id", ondelete="CASCADE"), index=True
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    selected_rows: Mapped[list[int] | None] = mapped_column(JSONB)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ListCatalogBinding(Identity, Base):
    __tablename__ = "list_catalog_bindings"
    __table_args__ = (UniqueConstraint("owner_id", "identity_key"),)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    identity_key: Mapped[str] = mapped_column(String(90))
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    assertion: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ListSubscription(Identity, Base):
    __tablename__ = "list_subscriptions"
    provider: Mapped[str] = mapped_column(
        String(20), default="goodreads", server_default="goodreads"
    )
    list_id: Mapped[UUID] = mapped_column(
        ForeignKey("book_lists.id", ondelete="CASCADE"), unique=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    encrypted_config: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    state: Mapped[str] = mapped_column(String(20), default="idle")
    message: Mapped[str] = mapped_column(Text, default="Ready to observe Goodreads shelf additions")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    run_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ListWritebackPolicy(Base):
    __tablename__ = "list_writeback_policies"
    list_id: Mapped[UUID] = mapped_column(
        ForeignKey("book_lists.id", ondelete="CASCADE"), primary_key=True
    )
    subscription_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("list_subscriptions.id", ondelete="SET NULL")
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    account_generation: Mapped[int] = mapped_column(Integer)
    remote_owner_id: Mapped[int] = mapped_column(Integer)
    external_list_id: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ListWritebackLease(Base):
    __tablename__ = "list_writeback_leases"
    # Remote identity serializes multiple local lists/accounts targeting the same list.
    target: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"))
    token: Mapped[UUID] = mapped_column()
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ListComparisonRow(Identity, Base):
    __tablename__ = "list_comparison_rows"
    __table_args__ = (UniqueConstraint("comparison_id", "position"),)
    comparison_id: Mapped[UUID] = mapped_column(
        ForeignKey("operations.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    work_id: Mapped[UUID | None] = mapped_column(ForeignKey("works.id"))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ListObservation(Identity, Base):
    __tablename__ = "list_observations"
    __table_args__ = (UniqueConstraint("subscription_id", "external_id"),)
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("list_subscriptions.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(80))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    present: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Operation(Identity, Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key"),
        Index(
            "ix_operations_list_writeback",
            text("(payload->>'list_id')"),
            "created_at",
            postgresql_where=text("kind = 'lists.writeback'"),
        ),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40), default="queued")
    message: Mapped[str] = mapped_column(Text, default="Waiting for a worker")
    job_id: Mapped[int | None] = mapped_column(Integer)
    integration_id: Mapped[UUID | None] = mapped_column(ForeignKey("integrations.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditEvent(Identity, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[UUID | None] = mapped_column(index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class IdentityChange(Identity, Base):
    __tablename__ = "identity_changes"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('asset_match', 'source_detach', 'version_review', 'work_merge')",
            name="identity_changes_kind_check",
        ),
    )
    sequence: Mapped[int] = mapped_column(BigInteger, SQLIdentity(), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[UUID] = mapped_column(index=True)
    work_id: Mapped[UUID | None] = mapped_column(ForeignKey("works.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    before: Mapped[dict[str, Any]] = mapped_column(JSONB)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(String(500))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class InventoryRun(Identity, Base):
    __tablename__ = "inventory_runs"
    integration_id: Mapped[UUID] = mapped_column(ForeignKey("integrations.id"), index=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), index=True)
    credential_generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="collecting")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InventoryObservation(Base):
    __tablename__ = "inventory_observations"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_runs.id", ondelete="CASCADE"), primary_key=True
    )
    library_external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    item_external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)


Index(
    "ix_works_title_trgm",
    Work.title,
    postgresql_using="gin",
    postgresql_ops={"title": "gin_trgm_ops"},
)


class CatalogAccount(Base):
    __tablename__ = "catalog_accounts"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    encrypted_token: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="untested")
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suggest_series_gaps: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    series_gap_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SeriesGapDismissal(Base):
    __tablename__ = "series_gap_dismissals"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SeriesGapBaseline(Base):
    """First observation of a series; later published gaps can be marked new."""

    __tablename__ = "series_gap_baselines"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    baselined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SeriesGapSighting(Identity, Base):
    __tablename__ = "series_gap_sightings"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", "external_id", "work_id"),
        Index(
            "ix_series_gap_sightings_unseen",
            "user_id",
            postgresql_where=text("seen_at IS NULL"),
        ),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(200))
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GoodreadsAccount(Base):
    __tablename__ = "goodreads_accounts"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_config: Mapped[str] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StorygraphAccount(Base):
    __tablename__ = "storygraph_accounts"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_config: Mapped[str] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MetadataSettings(Base):
    __tablename__ = "metadata_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ProviderCache(Base):
    __tablename__ = "provider_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProviderBudget(Base):
    __tablename__ = "provider_budgets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    next_request_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkMetadataSource(Identity, Base):
    __tablename__ = "work_metadata_sources"
    __table_args__ = (UniqueConstraint("work_id", "provider", "external_id"),)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(200), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    manual_match: Mapped[bool] = mapped_column(Boolean, default=False)


class AcquisitionIntent(Identity, Base):
    __tablename__ = "acquisition_intents"
    __table_args__ = (
        UniqueConstraint("owner_id", "work_id", "fingerprint"),
        CheckConstraint(
            "NOT (specification ? 'download_constraints') OR "
            "jsonb_typeof(specification -> 'download_constraints') = 'object'",
            name="ck_request_download_constraints",
        ),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    specification: Mapped[dict[str, Any]] = mapped_column(JSONB)
    release_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AcquisitionReason(Identity, Base):
    __tablename__ = "acquisition_reasons"
    __table_args__ = (
        UniqueConstraint("intent_id", "kind", "reference"),
        CheckConstraint("kind IN ('manual', 'list', 'series')", name="acquisition_reason_kind"),
        CheckConstraint(
            "approval_status IN ('pending', 'approved', 'declined')",
            name="ck_acquisition_reason_approval",
        ),
    )
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_intents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    release_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    list_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("book_lists.id", ondelete="SET NULL"), index=True
    )
    approval_status: Mapped[str] = mapped_column(
        String(20), default="approved", server_default="approved"
    )
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(300))


class AcquisitionReservation(Identity, Base):
    __tablename__ = "acquisition_reservations"
    __table_args__ = (CheckConstraint("state IN ('planned', 'selected', 'committed', 'released')"),)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), index=True)
    destination_id: Mapped[UUID | None] = mapped_column(ForeignKey("libraries.id"))
    scope: Mapped[str] = mapped_column(String(80))
    requirements: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="planned")


class AcquisitionTarget(Identity, Base):
    __tablename__ = "acquisition_targets"
    __table_args__ = (
        UniqueConstraint("intent_id", "slot"),
        CheckConstraint("slot IN ('ebook', 'audio', 'either')"),
        CheckConstraint(
            "state IN ('wanted', 'satisfied', 'awaiting-inventory', 'paused', 'cancelled')"
        ),
    )
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_intents.id"), index=True)
    slot: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(30), default="wanted")
    message: Mapped[str] = mapped_column(String(300))
    reservation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("acquisition_reservations.id"), index=True
    )
    satisfied_asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("library_assets.id"))


class AcquisitionSelection(Identity, Base):
    __tablename__ = "acquisition_selections"
    __table_args__ = (
        UniqueConstraint("owner_id", "command_key"),
        CheckConstraint("state IN ('prepared', 'committed', 'fulfilled', 'cancelled')"),
        Index(
            "uq_acquisition_selected_reservation",
            "reservation_id",
            unique=True,
            postgresql_where=text("state IN ('prepared', 'committed')"),
        ),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_intents.id"), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_targets.id"))
    reservation_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_reservations.id"))
    artifact_id: Mapped[UUID] = mapped_column(ForeignKey("source_artifacts.id"), index=True)
    downloader_id: Mapped[UUID] = mapped_column(ForeignKey("integrations.id"))
    destination_id: Mapped[UUID] = mapped_column(ForeignKey("import_destinations.id"))
    command_key: Mapped[str] = mapped_column(String(200))
    command: Mapped[dict[str, Any]] = mapped_column(JSONB)
    frozen: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="prepared")
    message: Mapped[str] = mapped_column(
        String(300), default="Release selected; download not started"
    )


class DownloadAttempt(Identity, Base):
    __tablename__ = "download_attempts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'preflight', 'submitting', 'uncertain', "
            "'downloading', 'complete', 'held', 'cancelled')"
        ),
        CheckConstraint("NOT external_may_exist OR state != 'cancelled'"),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    selection_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_selections.id"), unique=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    message: Mapped[str] = mapped_column(String(300), default="Waiting to check the downloader")
    external_may_exist: Mapped[bool] = mapped_column(Boolean, default=False)
    endpoint_key: Mapped[str] = mapped_column(String(64))
    run_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    observation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    inspection_id: Mapped[UUID | None] = mapped_column(ForeignKey("download_inspections.id"))


class DownloadMembership(Base):
    """Immutable selections served by a physical transfer, including its representative."""

    __tablename__ = "download_memberships"
    selection_id: Mapped[UUID] = mapped_column(
        ForeignKey("acquisition_selections.id"), primary_key=True
    )
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), index=True)
    join_operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))


class CapacitySettings(Base):
    __tablename__ = "capacity_settings"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    storage_generation: Mapped[int] = mapped_column(BigInteger, default=0)


class DownloadCapacity(Base):
    __tablename__ = "download_capacity"
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), primary_key=True)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    slot_active: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    resources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    import_resources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    observed_mounts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ImportCapacity(Base):
    __tablename__ = "import_capacity"
    entry_id: Mapped[UUID] = mapped_column(ForeignKey("import_entries.id"), primary_key=True)
    resources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    observed_mounts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class DownloadHandoff(Identity, Base):
    __tablename__ = "download_handoffs"
    __table_args__ = (
        Index(
            "uq_active_download_handoff", "attempt_id", unique=True, postgresql_where=text("active")
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), index=True)
    reviewer_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    inspection_id: Mapped[UUID] = mapped_column(ForeignKey("download_inspections.id"), unique=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DownloadRepair(Identity, Base):
    __tablename__ = "download_repairs"
    __table_args__ = (
        UniqueConstraint("actor_id", "command_key"),
        CheckConstraint("state IN ('pending', 'applied', 'held')"),
        Index(
            "uq_pending_download_repair",
            "attempt_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    command_key: Mapped[str] = mapped_column(String(200))
    revision: Mapped[str] = mapped_column(String(64))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    changes: Mapped[list[str]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    message: Mapped[str] = mapped_column(String(300))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DownloadFulfillment(Identity, Base):
    """Historical satisfaction evidence; never a substitute for current inventory."""

    __tablename__ = "download_fulfillments"
    __table_args__ = (UniqueConstraint("attempt_id", "target_id"),)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("acquisition_targets.id"), index=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("library_assets.id"))
    import_entry_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_entries.id"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)


class DownloadIdentityClaim(Identity, Base):
    __tablename__ = "download_identity_claims"
    __table_args__ = (
        Index(
            "uq_active_download_identity",
            "endpoint_key",
            "torrent_hash",
            unique=True,
            postgresql_where=text("active"),
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), index=True)
    endpoint_key: Mapped[str] = mapped_column(String(64))
    torrent_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OrganizationSettings(Base):
    __tablename__ = "organization_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB)


class DownloadInspection(Identity, Base):
    __tablename__ = "download_inspections"
    __table_args__ = (CheckConstraint("state IN ('queued', 'running', 'ready', 'failed')"),)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    source_key: Mapped[str] = mapped_column(String(60))
    source_path: Mapped[str] = mapped_column(Text)
    relative_path: Mapped[str] = mapped_column(String(1024))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    message: Mapped[str] = mapped_column(String(300), default="Waiting to inspect completed files")
    run_token: Mapped[UUID | None] = mapped_column()
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class InspectionGrouping(Identity, Base):
    __tablename__ = "inspection_groupings"
    __table_args__ = (UniqueConstraint("inspection_id", "position"),)
    inspection_id: Mapped[UUID] = mapped_column(ForeignKey("download_inspections.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    revision: Mapped[str] = mapped_column(String(64))
    previous_revision: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)


class FrozenImportPlan(Identity, Base):
    __tablename__ = "frozen_import_plans"
    __table_args__ = (UniqueConstraint("inspection_id", "revision"),)
    inspection_id: Mapped[UUID] = mapped_column(ForeignKey("download_inspections.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ImportStorageSettings(Base):
    __tablename__ = "import_storage_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destinations: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    sources: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    staging_root: Mapped[str | None] = mapped_column(String(1024))


class ImportDestination(Identity, Base):
    __tablename__ = "import_destinations"
    __table_args__ = (
        CheckConstraint("medium IN ('ebook', 'audio')"),
        CheckConstraint("mode IN ('hardlink', 'copy')"),
    )
    root_key: Mapped[str] = mapped_column(String(60), unique=True)
    library_id: Mapped[UUID] = mapped_column(ForeignKey("libraries.id"))
    medium: Mapped[str] = mapped_column(String(10))
    backend_path: Mapped[str] = mapped_column(String(1024))
    mode: Mapped[str] = mapped_column(String(10), default="hardlink")
    seeding_rename: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    client_path: Mapped[str | None] = mapped_column(String(1024))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    probe_operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))
    probe_token: Mapped[UUID | None] = mapped_column()


class AutomaticImportPolicy(Identity, Base):
    __tablename__ = "automatic_import_policies"
    destination_id: Mapped[UUID] = mapped_column(ForeignKey("import_destinations.id"), unique=True)
    approved_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AutomaticImport(Identity, Base):
    __tablename__ = "automatic_imports"
    __table_args__ = (CheckConstraint("state IN ('queued', 'inspecting', 'held', 'importing')"),)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), unique=True)
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("automatic_import_policies.id"))
    policy_generation: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    inspection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("download_inspections.id"), unique=True
    )
    import_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_runs.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    message: Mapped[str] = mapped_column(String(500), default="Waiting for automatic import checks")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class AutomaticImportContinuation(Identity, Base):
    """A later authorized join never overwrites an earlier automatic import/run."""

    __tablename__ = "automatic_import_continuations"
    __table_args__ = (
        CheckConstraint("state IN ('queued', 'inspecting', 'held', 'importing', 'complete')"),
    )
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("download_attempts.id"), index=True)
    join_operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("automatic_import_policies.id"))
    policy_generation: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True)
    inspection_id: Mapped[UUID | None] = mapped_column(ForeignKey("download_inspections.id"))
    import_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_runs.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    message: Mapped[str] = mapped_column(
        String(500), default="Checking the saved transfer before reuse"
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ImportRun(Identity, Base):
    __tablename__ = "import_runs"
    __table_args__ = (UniqueConstraint("owner_id", "command_key"),)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("frozen_import_plans.id"))
    command_key: Mapped[str] = mapped_column(String(200))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ImportEntry(Identity, Base):
    __tablename__ = "import_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "group_id"),
        CheckConstraint(
            "state IN ('queued', 'publishing', 'awaiting-library', 'confirmed', 'held', 'skipped', "
            "'cancelling', 'cancel-held', 'cancelled')"
        ),
        Index(
            "uq_import_reserved_version",
            "destination_id",
            "version_id",
            unique=True,
            postgresql_where=text("reserved"),
        ),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("import_runs.id"), index=True)
    group_id: Mapped[UUID] = mapped_column()
    version_id: Mapped[UUID] = mapped_column(ForeignKey("versions.id"))
    destination_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_destinations.id"))
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="queued")
    message: Mapped[str] = mapped_column(String(500))
    reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    specification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expected_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cover_export: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    run_token: Mapped[UUID | None] = mapped_column()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("library_assets.id"))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class MonitoredRelease(Identity, Base):
    """A followed or quick-added book that waits for its release day before any source search."""

    __tablename__ = "monitored_releases"
    __table_args__ = (
        UniqueConstraint("owner_id", "work_id"),
        CheckConstraint(
            "state IN ('waiting', 'wanted', 'available', 'stopped')",
            name="monitored_release_state",
        ),
        CheckConstraint(
            "basis IN ('audiobook', 'work', 'unknown')",
            name="monitored_release_basis",
        ),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    release_date: Mapped[date | None] = mapped_column(Date)
    basis: Mapped[str] = mapped_column(String(20), default="unknown")
    state: Mapped[str] = mapped_column(String(20), default="waiting")
    round: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"))
    specification: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class DiscoveryLayout(Base):
    __tablename__ = "discovery_layouts"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class DiscoveryFollow(Base):
    __tablename__ = "discovery_follows"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    pinned: Mapped[bool] = mapped_column(Boolean, default=True)
    tracking: Mapped[bool] = mapped_column(Boolean, default=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(600))
