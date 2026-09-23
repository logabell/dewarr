"""A release identity is scoped to a source/indexer, work family, and medium."""

from sqlalchemy import select

from app.db.models import ReleaseBlock
from app.domain.work_graph import canonical_work, family_ids
from app.importing.naming import fingerprint


def release_keys(release, descriptor=None):
    if hasattr(release, "model_dump"):
        release = release.model_dump(mode="json")
    source = release["source"] + (
        ":" + str(release["indexer_id"]) if release.get("indexer_id") else ""
    )
    keys = {"release:" + fingerprint({"source": source, "id": release["source_id"]})}
    for field in ("infohash_v1", "infohash_v2", "artifact_sha256"):
        if value := (descriptor or {}).get(field):
            keys.add(field + ":" + value.lower())
    return source, sorted(keys)


async def blocked(db, work_id, medium, release, descriptor=None):
    _, keys = release_keys(release, descriptor)
    entries = await db.scalars(
        select(ReleaseBlock).where(
            ReleaseBlock.work_id.in_(family_ids(work_id)),
            ReleaseBlock.medium == medium,
            ReleaseBlock.active.is_(True),
        )
    )
    return next((row for row in entries if set(row.identities).intersection(keys)), None)


async def add(db, selection, reason, actor_id, *, automatic=True):
    work = await canonical_work(db, selection.frozen["origin_work_id"])
    release = selection.frozen["release"]
    source, keys = release_keys(release, selection.frozen["descriptor"])
    key = fingerprint({"source": source, "id": release["source_id"]})
    # Callers hold the acquisition lock for every transfer member.
    row = await db.scalar(
        select(ReleaseBlock).where(
            ReleaseBlock.work_id == work.id,
            ReleaseBlock.medium == selection.frozen["requirements"]["medium"],
            ReleaseBlock.release_key == key,
        )
    )
    if not row:
        row = ReleaseBlock(
            work_id=work.id,
            medium=selection.frozen["requirements"]["medium"],
            release_key=key,
            source=source,
            title=release["title"][:1000],
            identities=keys,
        )
        db.add(row)
    row.reason, row.actor_id, row.automatic, row.active = reason[:300], actor_id, automatic, True
    return row
