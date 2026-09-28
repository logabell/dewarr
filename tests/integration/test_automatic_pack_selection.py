# ruff: noqa: F811
import base64
from uuid import UUID

import pytest
from sqlalchemy import select

from app.adapters.torrent_descriptor import inspect_torrent
from app.db.models import (
    AcquisitionSelection,
    CatalogSeries,
    Operation,
    SeriesMembership,
    SourceArtifact,
    SourceResult,
    Work,
)
from app.domain import automatic_selection as automatic
from app.domain.release_profiles import ProfileSnapshot, ReleasePreferences
from app.security import encrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_automatic_dispatch import authorized  # noqa: F401
from tests.integration.test_automatic_selection import detail, source, start  # noqa: F401
from tests.pack_fixture import catalog as pack_catalog
from tests.torrent_fixture import torrent_bytes

pytestmark = pytest.mark.integration


@pytest.fixture
async def series_pack(database, admin, source, catalog):
    async with database() as db, db.begin():
        second = Work(title="Roads", authors=["Writer"])
        db.add(second)
        await db.flush()
        second_id = second.id
    series = await pack_catalog(database, admin["id"], [catalog["work"], second_id])
    raw = torrent_bytes(
        name=b"Coast",
        files=[{b"length": 12, b"path": [b"Harbor.m4b"]}, {b"length": 12, b"path": [b"Roads.m4b"]}],
    )
    descriptor = await inspect_torrent(raw)
    release = source["release"].model_copy(
        update={"title": "Coast", "raw_title": "Coast Books 1-2", "size_bytes": 24}
    )
    source.update(release=release, descriptor=descriptor)
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, source["artifact"])
        artifact.sha256 = descriptor.artifact_sha256
        artifact.descriptor = descriptor.model_dump(mode="json")
        artifact.encrypted_content = encrypt_secrets({"torrent": base64.b64encode(raw).decode()})
        artifact.release_snapshot = release.model_dump(mode="json")
        (await db.get(SourceResult, source["result"])).release_snapshot = release.model_dump(
            mode="json"
        )
    return series


async def test_catalog_pack_prepares_once_with_explicit_corroboration_not_extra_requests(
    client, database, source, series_pack
):
    operation = await start(client, source)
    assert "maximum_bytes" not in operation
    assert "maximum_pack_bytes" not in operation
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "completed", value
    assert "maximum_bytes" not in value
    assert len(value["decisions"][0]["coverage"]["members"]) == 2
    async with database() as db:
        selected = await db.get(AcquisitionSelection, UUID(value["selection_id"]))
        proof = selected.frozen["automatic_selection"]
        assert proof["coverage"]["evidence"] == "catalog-and-manifest"
        assert proof["pack_catalog"]["series"][0]["id"] == str(series_pack)
        assert selected.frozen["profile"]["preferences"]["maximum_bytes"] is None
    await automatic.run(UUID(operation["id"]))
    assert len(source["resolver"].calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "private",
        "unpublished",
        "compilation",
        "duplicate-position",
        "unknown-position",
        "wrong-author",
    ],
)
async def test_missing_or_changed_series_evidence_cannot_authorize_pack(
    client, database, admin, source, series_pack, change
):
    operation = await start(client, source)

    async def alter():
        async with database() as db, db.begin():
            series = await db.get(CatalogSeries, series_pack)
            members = list(
                await db.scalars(
                    select(SeriesMembership)
                    .where(SeriesMembership.series_id == series.id)
                    .order_by(SeriesMembership.external_id)
                )
            )
            if change == "generation":
                series.generation += 1
            elif change == "private":
                pass
            elif change == "wrong-author":
                (await db.get(Work, members[1].work_id)).authors = ["Changed"]
            else:
                values = {
                    "unpublished": {"release_date": "2999-01-01"},
                    "compilation": {"compilation": True},
                    "duplicate-position": {"position": "1"},
                    "unknown-position": {"position": None},
                }
                members[1].snapshot = {**members[1].snapshot, **values[change]}
            if change == "private":
                from app.db.models import User
                from app.security import hash_password

                other = User(
                    username="private-owner",
                    display_name="Private owner",
                    password_hash=hash_password("another-long-password"),
                    role="member",
                )
                db.add(other)
                await db.flush()
                series.owner_id = other.id

    source["resolver"].callback = alter
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "held", value
    async with database() as db:
        assert not list(await db.scalars(select(AcquisitionSelection)))


async def test_disabled_pack_preference_rejects_before_fetch(client, database, source, series_pack):
    async with database() as db, db.begin():
        row = await db.get(Operation, source["search"])
        # Keep search and current personal preferences equivalent.
        from app.db.models import AcquisitionDefaults

        db.add(
            AcquisitionDefaults(
                key=f"user:{row.owner_id}",
                owner_id=row.owner_id,
                preferences={"prefer_series_packs": False},
            )
        )
        row.payload = {
            **row.payload,
            "profile": ProfileSnapshot(
                preferences=ReleasePreferences(prefer_series_packs=False)
            ).model_dump(mode="json"),
        }
    operation = await start(client, source)
    assert "maximum_pack_bytes" not in operation
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "held" and not source["resolver"].calls
    assert any("disabled" in reason for reason in value["decisions"][0]["reasons"])


async def test_changed_catalog_before_dispatch_holds_existing_attempt_without_submission(
    client, database, series_pack, authorized
):
    from app.db.models import DownloadAttempt
    from app.domain import download_attempts as downloads

    saved = await start(client, authorized)
    await automatic.run(UUID(saved["id"]))
    from app.domain import automatic_packs

    await automatic_packs.run(UUID(saved["id"]))
    value = await detail(client, saved["id"])
    assert value["download_id"], value
    async with database() as db, db.begin():
        (await db.get(CatalogSeries, series_pack)).generation += 1
    from fastapi import HTTPException

    from app.domain import automatic_dispatch

    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(value["selection_id"]))
        with pytest.raises(HTTPException, match="Series coverage changed"):
            await automatic_dispatch.require_selection(db, selection)
    await downloads.run(UUID(value["download_id"]))
    async with database() as db:
        attempt = await db.get(DownloadAttempt, UUID(value["download_id"]))
        assert attempt.state == "held" and not attempt.external_may_exist
    assert not authorized["qbit"].calls


async def test_eligible_pack_is_preferred_to_more_seeded_single_book(
    client, database, source, series_pack
):
    async with database() as db, db.begin():
        original = await db.get(SourceResult, source["result"])
        single = SourceResult(
            owner_id=original.owner_id,
            operation_id=original.operation_id,
            source_key=original.source_key,
            source_generation=original.source_generation,
            expires_at=original.expires_at,
            encrypted_reference=original.encrypted_reference,
            release_snapshot={
                **original.release_snapshot,
                "title": "Harbor",
                "raw_title": "Harbor",
                "source_id": "999",
                "seeders": 9999,
            },
        )
        db.add(single)
    saved = await start(client, source)
    await automatic.run(UUID(saved["id"]))
    value = await detail(client, saved["id"])
    assert value["status"] == "completed", value
    assert source["resolver"].calls == [source["result"]]
    assert next(d for d in value["decisions"] if d["selected"])["coverage"]


async def test_legacy_operation_cannot_gain_pack_scope_on_replay(
    client, database, source, series_pack
):
    saved = await start(client, source)
    async with database() as db, db.begin():
        operation = await db.get(Operation, UUID(saved["id"]))
        payload = dict(operation.payload)
        payload.pop("pack_catalog")
        operation.payload = payload
    await automatic.run(UUID(saved["id"]))
    value = await detail(client, saved["id"])
    assert value["status"] == "held" and not source["resolver"].calls


async def test_manifest_reranking_uses_target_format_instead_of_sibling_format(
    client, database, source, series_pack, monkeypatch
):
    from copy import deepcopy

    async with database() as db, db.begin():
        original_result = await db.get(SourceResult, source["result"])
        original_artifact = await db.get(SourceArtifact, source["artifact"])
        resolved = {}
        for index, target_extension in enumerate(["mp3", "m4b"]):
            raw = torrent_bytes(
                name=b"Coast",
                files=[
                    {b"length": 12, b"path": [f"Harbor.{target_extension}".encode()]},
                    {b"length": 12, b"path": [b"Roads.m4b"]},
                ],
            )
            descriptor = await inspect_torrent(raw)
            release = source["release"].model_copy(
                update={
                    "source_id": "501" if index == 0 else "902",
                    "formats": ["mp3", "m4b"] if index == 0 else ["m4b"],
                    "seeders": 100 if index == 0 else 5,
                }
            )
            artifact = (
                original_artifact
                if index == 0
                else SourceArtifact(
                    owner_id=original_artifact.owner_id,
                    source_key="mam",
                    source_id="902",
                    source_generation=1,
                )
            )
            artifact.sha256 = descriptor.artifact_sha256
            artifact.descriptor = descriptor.model_dump(mode="json")
            artifact.encrypted_content = encrypt_secrets(
                {"torrent": base64.b64encode(raw).decode()}
            )
            artifact.release_snapshot = release.model_dump(mode="json")
            result = (
                original_result
                if index == 0
                else SourceResult(
                    owner_id=original_result.owner_id,
                    operation_id=original_result.operation_id,
                    source_key="mam",
                    source_generation=1,
                    expires_at=original_result.expires_at,
                    encrypted_reference=original_result.encrypted_reference,
                )
            )
            result.release_snapshot = deepcopy(artifact.release_snapshot)
            db.add_all([artifact, result])
            await db.flush()
            resolved[result.id] = (artifact.id, release)
        winning_result = result.id
    calls = []

    async def resolve(owner, row, use_wedge=False):
        calls.append(row.id)
        return resolved[row.id]

    monkeypatch.setattr(automatic, "resolve_candidate", resolve)
    saved = await start(client, source)
    await automatic.run(UUID(saved["id"]))
    assert (await detail(client, saved["id"]))["status"] == "queued"
    await automatic.run(UUID(saved["id"]))
    value = await detail(client, saved["id"])
    assert value["status"] == "completed", value
    assert calls == [source["result"], winning_result]
    assert next(d for d in value["decisions"] if d["selected"])["result_id"] == str(winning_result)
