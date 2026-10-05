import base64
import hashlib

from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.nzb_descriptor import inspect_nzb
from app.adapters.torrent_descriptor import inspect_torrent
from app.db.models import AuditEvent, SourceArtifact, SourceConnection, User
from app.db.session import session_factory
from app.domain.operations import transaction_lock
from app.domain.source_network import source_call
from app.security import decrypt_secrets, encrypt_secrets


async def member(db, user_id):
    user = await db.get(User, user_id, populate_existing=True)
    if not user or not user.active:
        raise HTTPException(401, "Your account is no longer active")
    if user.role == "viewer":
        raise HTTPException(403, "This account has read-only access")


async def resolve_mam(user_id, source_id, *, expected_generation=None, use_wedge=False):
    async with session_factory()() as db:
        await member(db, user_id)
    artifact, generation = await source_call(
        user_id,
        "resolve",
        {"source_id": source_id, "use_wedge": True} if use_wedge else source_id,
        with_generation=True,
        expected_generation=expected_generation,
    )
    return await persist_artifact(user_id, source_id, artifact, generation, "mam")


async def persist_artifact(
    user_id, source_id, artifact, generation, source_key, *, expected_downloader=None
):
    if getattr(artifact.release, "protocol", None) == "nzb":
        descriptor = inspect_nzb(artifact.content)
        secret_key = "nzb"
    else:
        descriptor = await inspect_torrent(artifact.content)
        secret_key = "torrent"
    digest = hashlib.sha256(artifact.content).hexdigest()
    if digest != descriptor.artifact_sha256:
        raise AdapterError(FailureKind.PARSER, "Download identity changed during inspection.")
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"source:{source_key}")
        await member(db, user_id)
        source = await db.get(SourceConnection, source_key)
        if not source or not source.enabled or source.generation != generation:
            raise HTTPException(
                409, "Source settings changed while inspecting the release. Resolve it again."
            )
        if expected_downloader:
            from app.domain.downloaders import SETTINGS_LOCK, connection_or_404

            await transaction_lock(db, SETTINGS_LOCK)
            downloader = await connection_or_404(db, expected_downloader[0])
            if not downloader.enabled or downloader.credential_generation != expected_downloader[1]:
                raise HTTPException(409, "Downloader changed while inspecting metadata; retry")
        existing = await db.scalar(
            select(SourceArtifact).where(
                SourceArtifact.owner_id == user_id,
                SourceArtifact.source_key == source_key,
                SourceArtifact.source_id == source_id,
                SourceArtifact.source_generation == generation,
                SourceArtifact.sha256 == digest,
            )
        )
        if existing:
            return existing.id
        row = SourceArtifact(
            owner_id=user_id,
            source_key=source_key,
            source_id=source_id,
            source_generation=generation,
            sha256=digest,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets(
                {secret_key: base64.b64encode(artifact.content).decode()}
            ),
            release_snapshot=artifact.release.model_dump(mode="json"),
        )
        db.add(row)
        await db.flush()
        db.add(AuditEvent(actor_id=user_id, action="source.artifact.inspected", entity_id=row.id))
        return row.id


async def persist_file_list(user_id, release, generation):
    """Store a Soulseek file list. There is no torrent to probe."""
    from app.adapters.slskd import file_list_descriptor

    descriptor, content = file_list_descriptor(release)
    digest = hashlib.sha256(content).hexdigest()
    if digest != descriptor.artifact_sha256:
        raise AdapterError(FailureKind.PARSER, "Soulseek file list identity changed during save.")
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, "source:slskd")
        await member(db, user_id)
        source = await db.get(SourceConnection, "slskd")
        if not source or not source.enabled or source.generation != generation:
            raise HTTPException(
                409, "Source settings changed while saving the Soulseek folder. Search again."
            )
        existing = await db.scalar(
            select(SourceArtifact).where(
                SourceArtifact.owner_id == user_id,
                SourceArtifact.source_key == "slskd",
                SourceArtifact.source_id == release.source_id,
                SourceArtifact.source_generation == generation,
                SourceArtifact.sha256 == digest,
            )
        )
        if existing:
            return existing.id
        row = SourceArtifact(
            owner_id=user_id,
            source_key="slskd",
            source_id=release.source_id,
            source_generation=generation,
            sha256=digest,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(content).decode()}),
            release_snapshot=release.model_dump(mode="json"),
        )
        db.add(row)
        await db.flush()
        db.add(AuditEvent(actor_id=user_id, action="source.artifact.inspected", entity_id=row.id))
        return row.id


def artifact_bytes(row):
    """Verified bytes for internal dispatch or explicit owner-authorized torrent export."""
    try:
        secrets = decrypt_secrets(row.encrypted_content)
        encoded = secrets.get("torrent") or secrets.get("nzb")
        content = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise ValueError("Digest mismatch")
        return content
    except Exception as error:
        raise AdapterError(
            FailureKind.PARSER, "Stored torrent integrity could not be verified."
        ) from error
