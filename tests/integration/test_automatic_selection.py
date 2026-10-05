# ruff: noqa: F811
import base64
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.mam import MAMRelease
from app.adapters.torrent_descriptor import inspect_torrent
from app.db.models import (
    AcquisitionReservation,
    AcquisitionSelection,
    DownloadAttempt,
    Operation,
    ReleaseBlock,
    SourceArtifact,
    SourceConnection,
    SourceResult,
    User,
    Work,
)
from app.domain import automatic_selection as automatic
from app.domain.release_blocklist import release_keys
from app.domain.release_profiles import ProfileSnapshot, ReleasePreferences
from app.jobs.retry import SourceSearchRetry
from app.security import encrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.torrent_fixture import torrent_bytes

pytestmark = pytest.mark.integration


async def test_large_selection_batches_blocklist_reads_without_losing_scope(
    client, database, source, catalog, tmp_path
):
    operation = await start(client, source)
    async with database() as db, db.begin():
        original = await db.get(SourceResult, source["result"])
        origin = Work(
            title="Earlier catalog identity", authors=["Writer"], redirect_to=catalog["work"]
        )
        unrelated = Work(title="Different book", authors=["Writer"])
        db.add_all([origin, unrelated])
        await db.flush()
        for index in range(120):
            release = source["release"].model_copy(update={"source_id": f"bulk-{index}"})
            db.add(
                SourceResult(
                    owner_id=original.owner_id,
                    operation_id=original.operation_id,
                    source_key=original.source_key,
                    source_generation=original.source_generation,
                    expires_at=original.expires_at,
                    encrypted_reference=original.encrypted_reference,
                    release_snapshot=release.model_dump(mode="json"),
                )
            )
            if index < 4:
                source_key, keys = release_keys(release)
                db.add(
                    ReleaseBlock(
                        work_id=unrelated.id if index == 3 else origin.id,
                        medium="ebook" if index == 2 else "audio",
                        active=index != 1,
                        source=source_key,
                        title=release.title,
                        identities=keys,
                        release_key=keys[0].removeprefix("release:"),
                        reason="Previously rejected",
                        actor_id=original.owner_id,
                        automatic=True,
                    )
                )
    async with database() as db:
        saved = await db.get(Operation, UUID(operation["id"]))
        work = await db.get(Work, catalog["work"])
        calls = []

        def count(*args):
            calls.append(1)

        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", count)
        started = time.perf_counter()
        try:
            ranked = await automatic.candidates(
                db,
                saved,
                work,
                ProfileSnapshot.model_validate(saved.payload["profile"]),
                saved.payload["requirements"],
                None,
            )
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count)
        print(f"121 candidates: {len(calls)} SQL statements in {elapsed:.4f}s")
        (tmp_path / "selection-metrics.json").write_text(
            json.dumps(
                {
                    "candidates": len(ranked),
                    "sql_statements": len(calls),
                    "elapsed_seconds": elapsed,
                }
            )
        )
        assert len(ranked) == 121
        assert {
            release.source_id
            for _, _, release, problems in ranked
            if "This release is blocklisted for this book and medium" in problems
        } == {"bulk-0"}
        assert len(calls) <= 4, f"Candidate ranking issued {len(calls)} SQL statements"


@pytest.mark.parametrize("refreshed_count", [100, 0])
async def test_automatic_popularity_uses_same_source_counts_and_freezes_selected_observation(
    client, database, source, monkeypatch, refreshed_count
):
    criteria = ["format", "source", "popularity", "seeders"]
    response = await client.post(
        "/api/acquisition/profiles",
        json={"name": "Source popularity", "preferences": {"criteria": criteria}},
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    second, artifact_id, release = await additional_candidate(
        database, source, source_id="502", seeders=2
    )
    original = source["release"].model_copy(update={"snatches": 20})
    popular = release.model_copy(update={"snatches": 100})
    async with database() as db, db.begin():
        search = await db.get(Operation, source["search"])
        search.payload = {**search.payload, "profile": profile}
        for result_id, art_id, value in [
            (source["result"], source["artifact"], original),
            (second, artifact_id, popular),
        ]:
            (await db.get(SourceResult, result_id)).release_snapshot = value.model_dump(mode="json")
            (await db.get(SourceArtifact, art_id)).release_snapshot = value.model_dump(mode="json")
    calls = []

    async def resolve(owner, row, use_wedge=False):
        calls.append(row.id)
        return (
            (artifact_id, popular.model_copy(update={"snatches": refreshed_count}))
            if row.id == second
            else (source["artifact"], original)
        )

    monkeypatch.setattr(automatic, "resolve_candidate", resolve)
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    if refreshed_count == 0:
        interim = await detail(client, operation["id"])
        assert interim["status"] == "queued"
        assert "Inspected release evidence changed the ranking" in interim["message"]
        await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "completed", value
    assert calls == ([second] if refreshed_count else [second, source["result"]])
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(value["selection_id"]))
        frozen = selection.frozen
        assert frozen["profile"]["preferences"]["criteria"] == criteria
        assert frozen["automatic_selection"]["source_popularity"] == {
            "origin": "mam",
            "metric": "completed_downloads",
            "value": refreshed_count or 20,
        }
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 0
    await client.put(
        f"/api/acquisition/profiles/{profile['id']}",
        json={"name": "Changed later", "expected_generation": 1, "preferences": {}},
    )
    async with database() as db:
        assert (await db.get(AcquisitionSelection, UUID(value["selection_id"]))).frozen == frozen


@pytest.fixture
async def source(client, database, admin, catalog, selection_route, monkeypatch):
    raw = torrent_bytes(name=b"Harbor", files=[{b"length": 12, b"path": [b"Harbor.m4b"]}])
    descriptor = await inspect_torrent(raw)
    release = MAMRelease(
        source_id="501",
        title="Harbor",
        raw_title="Harbor",
        authors=["Writer"],
        medium="audio",
        language="en",
        formats=["m4b"],
        size_bytes=12,
        seeders=9,
        protocol="torrent",
        observed_at=datetime.now(UTC),
    )
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        artifact.sha256 = descriptor.artifact_sha256
        artifact.descriptor = descriptor.model_dump(mode="json")
        artifact.encrypted_content = encrypt_secrets({"torrent": base64.b64encode(raw).decode()})
        artifact.release_snapshot = release.model_dump(mode="json")
        search = Operation(
            owner_id=UUID(admin["id"]),
            kind="sources.search",
            idempotency_key="auto-search-fixture",
            status="completed",
            payload={
                "work": {"id": str(catalog["work"]), "title": "Harbor", "authors": ["Writer"]},
                "profile": ProfileSnapshot(preferences=ReleasePreferences()).model_dump(
                    mode="json"
                ),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=25)).isoformat(),
            },
        )
        db.add(search)
        await db.flush()
        result = SourceResult(
            owner_id=UUID(admin["id"]),
            operation_id=search.id,
            source_key="mam",
            source_generation=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=25),
            encrypted_reference=encrypt_secrets({"link": None}),
            release_snapshot=release.model_dump(mode="json"),
        )
        db.add(result)
        await db.flush()
        body = {
            k: v
            for k, v in selection_route.items()
            if k not in {"artifact_id", "confirmed_work_id"}
        }
        body["search_id"] = str(search.id)
        values = {
            "body": body,
            "artifact": artifact.id,
            "result": result.id,
            "search": search.id,
            "release": release,
            "descriptor": descriptor,
        }

    class Resolver:
        calls = []
        callback = None
        failure = None

        async def __call__(self, owner, row, use_wedge=False):
            self.calls.append(row.id)
            if self.callback:
                await self.callback()
            if self.failure:
                raise self.failure
            return values["artifact"], values["release"]

    resolver = Resolver()
    values["resolver"] = resolver
    monkeypatch.setattr(automatic, "resolve_candidate", resolver)
    return values


async def start(client, source, key="auto-select-fixture"):
    result = await client.post(
        "/api/acquisition/automatic-selections",
        json=source["body"],
        headers={"Idempotency-Key": key},
    )
    assert result.status_code == 202, result.text
    return result.json()


async def detail(client, identifier):
    result = await client.get(f"/api/acquisition/automatic-selections/{identifier}")
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize(
    "filename", ["Chapter 37.mp3", "Harbor Chapter 01.mp3", "Harbor (Part 1 of 3).m4b"]
)
async def test_single_chapter_torrent_is_held_before_preparation(
    client, database, source, filename
):
    raw = torrent_bytes(name=b"Harbor", files=[{b"length": 12, b"path": [filename.encode()]}])
    descriptor = await inspect_torrent(raw)
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, source["artifact"])
        artifact.sha256 = descriptor.artifact_sha256
        artifact.descriptor = descriptor.model_dump(mode="json")
        artifact.encrypted_content = encrypt_secrets({"torrent": base64.b64encode(raw).decode()})
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "held", value
    assert any(
        "one part" in reason for decision in value["decisions"] for reason in decision["reasons"]
    )
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 0
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 0


async def test_best_eligible_candidate_is_prepared_once_with_frozen_limits_and_provenance(
    client, database, source
):
    operation = await start(client, source)
    assert (await start(client, source))["id"] == operation["id"]
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "completed" and value["selection_id"]
    assert value["inspections"] == 1
    await automatic.run(UUID(operation["id"]))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 0
        selected = await db.get(AcquisitionSelection, UUID(value["selection_id"]))
        assert "Automatically eligible" in selected.frozen["verification"]
        assert selected.frozen["profile"]["preferences"]["maximum_bytes"] is None
        assert selected.frozen["automatic_selection"]["result_id"] == str(source["result"])
    assert len(source["resolver"].calls) == 1


async def test_cancelled_preparation_does_not_keep_release_selected(
    client, database, source, catalog
):
    from app.domain.release_download_status import for_releases, identity

    source["body"] = {**source["body"], "result_id": str(source["result"])}
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "completed" and value["selection_id"]
    async with database() as db:
        owner = (await db.get(Operation, UUID(operation["id"]))).owner_id
        before = await for_releases(db, owner, catalog["work"], [source["release"]])
        assert before[identity(source["release"])].prevent_download
    cancelled = await client.delete(f"/api/acquisition/selections/{value['selection_id']}")
    assert cancelled.status_code == 200, cancelled.text
    async with database() as db:
        after = await for_releases(db, owner, catalog["work"], [source["release"]])
        status = after[identity(source["release"])]
        assert status.state == "cancelled" and not status.prevent_download
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 0
    retry = await start(client, source, key="select-after-cancellation")
    await automatic.run(UUID(retry["id"]))
    assert (await detail(client, retry["id"]))["status"] == "completed"


@pytest.mark.parametrize("change", ["author", "unknown-seeds", "language", "pack", "recording"])
async def test_ineligible_candidates_remain_reviewable_without_fetching_torrents(
    client, database, source, catalog, change
):
    from app.db.models import AcquisitionIntent
    from app.domain.acquisition import RequestSpec
    from app.domain.corrections import revision

    async with database() as db, db.begin():
        row = await db.get(SourceResult, source["result"])
        fields = {
            "author": {"authors": ["Other Writer"]},
            "unknown-seeds": {"seeders": None},
            "language": {"language": "fr"},
            "pack": {"raw_title": "Harbor Books 1-3"},
        }
        if change == "recording":
            intent = await db.get(AcquisitionIntent, UUID(source["body"]["intent_id"]))
            spec = RequestSpec.model_validate(intent.specification).model_copy(
                update={"audio_version_id": catalog["versions"][1]}
            )
            intent.specification = spec.model_dump(mode="json")
            intent.fingerprint = revision(intent.specification)
        else:
            row.release_snapshot = {**row.release_snapshot, **fields[change]}
        if change == "language":
            intent = await db.get(AcquisitionIntent, UUID(source["body"]["intent_id"]))
            intent.specification = {**intent.specification, "language": "en"}
            intent.fingerprint = revision(intent.specification)
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "held" and value["decisions"][0]["reasons"]
    assert source["resolver"].calls == []


async def test_manifest_pack_evidence_is_retained_when_no_candidate_qualifies(
    client, database, source
):
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, source["artifact"])
        desc = dict(artifact.descriptor)
        desc["files"] = [*desc["files"], {"index": 1, "path": "Harbor/Roads.m4b", "size_bytes": 12}]
        desc["content_bytes"] = 24
        artifact.descriptor = desc
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "held" and value["inspections"] == 1
    assert any("track sequence" in r for r in value["decisions"][0]["reasons"])
    assert len(source["resolver"].calls) == 1


@pytest.mark.parametrize("change", ["cancel", "source", "actor", "catalog", "expired", "withdrawn"])
async def test_changes_during_inspection_cannot_prepare_stale_selection(
    client, database, source, admin, catalog, change
):
    from app.db.models import AcquisitionReason

    operation = await start(client, source)

    async def callback():
        if change == "cancel":
            result = await client.post(
                f"/api/acquisition/automatic-selections/{operation['id']}/cancel"
            )
            assert result.status_code == 200
            return
        async with database() as db, db.begin():
            if change == "source":
                (await db.get(SourceConnection, "mam")).generation += 1
            elif change == "actor":
                (await db.get(User, UUID(admin["id"]))).role = "viewer"
            elif change == "catalog":
                (await db.get(Work, catalog["work"])).title = "A changed title"
            elif change == "expired":
                (await db.get(SourceResult, source["result"])).expires_at = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
            else:
                (await db.scalar(select(AcquisitionReason))).active = False

    source["resolver"].callback = callback
    await automatic.run(UUID(operation["id"]))
    async with database() as db:
        assert (await db.get(Operation, UUID(operation["id"]))).status in {"held", "cancelled"}
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 0


async def test_source_cooldown_retains_command_and_can_resume(client, database, source):
    operation = await start(client, source)
    source["resolver"].failure = AdapterError(
        FailureKind.RATE_LIMIT, "Cooling down", retry_after=123
    )
    with pytest.raises(SourceSearchRetry) as wait:
        await automatic.run(UUID(operation["id"]))
    assert wait.value.retry_after == 123
    source["resolver"].failure = None
    await automatic.run(UUID(operation["id"]))
    assert (await detail(client, operation["id"]))["status"] == "completed"


async def test_route_failure_rolls_back_selection_mutations(client, database, source):
    source["body"]["destination_revision"] = "0" * 64
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    assert (await detail(client, operation["id"]))["status"] == "held"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 0
        assert (await db.scalar(select(AcquisitionReservation))).state == "planned"


async def additional_candidate(database, source, *, source_id, seeders=10, format="m4b"):
    release = source["release"].model_copy(
        update={"source_id": source_id, "seeders": seeders, "formats": [format]}
    )
    raw = torrent_bytes(
        name=b"Harbor", files=[{b"length": 12, b"path": [f"Harbor.{format}".encode()]}]
    )
    descriptor = await inspect_torrent(raw)
    async with database() as db, db.begin():
        original = await db.get(SourceResult, source["result"])
        row = SourceResult(
            owner_id=original.owner_id,
            operation_id=original.operation_id,
            source_key="mam",
            source_generation=1,
            expires_at=original.expires_at,
            encrypted_reference=encrypt_secrets({"link": None}),
            release_snapshot=release.model_dump(mode="json"),
        )
        artifact = SourceArtifact(
            owner_id=original.owner_id,
            source_key="mam",
            source_id=source_id,
            source_generation=1,
            sha256=descriptor.artifact_sha256,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(raw).decode()}),
            release_snapshot=release.model_dump(mode="json"),
        )
        db.add_all([row, artifact])
        await db.flush()
        return row.id, artifact.id, release


@pytest.mark.parametrize("failure", ["collection", "parser", "unsupported"])
async def test_rejected_first_candidate_falls_back_to_next_and_retains_decisions(
    client, database, source, monkeypatch, failure
):
    second, artifact_id, release = await additional_candidate(
        database, source, source_id="502", seeders=2
    )
    calls = []

    async def resolve(owner, row, use_wedge=False):
        calls.append(row.id)
        if row.id == second:
            return artifact_id, release
        if failure != "collection":
            raise AdapterError(FailureKind(failure), "Fixture artifact unavailable")
        async with database() as db, db.begin():
            artifact = await db.get(SourceArtifact, source["artifact"])
            artifact.descriptor = {
                **artifact.descriptor,
                "files": [{"index": 0, "path": "Harbor/books 1-3.m4b", "size_bytes": 12}],
            }
        return source["artifact"], source["release"]

    monkeypatch.setattr(automatic, "resolve_candidate", resolve)
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    assert (await detail(client, operation["id"]))["status"] == "queued"
    await automatic.run(UUID(operation["id"]))
    result = await detail(client, operation["id"])
    assert result["status"] == "completed" and result["artifact_id"] == str(artifact_id)
    assert result["inspections"] == 2
    assert result["decisions"][0]["reasons"]
    if failure != "collection":
        assert "Fixture artifact unavailable" in result["decisions"][0]["reasons"]
    assert calls == [source["result"], second]


@pytest.mark.parametrize("format", ["mp3", "m4b"])
async def test_format_preference_precedes_seed_count_then_seed_count_breaks_format_tie(
    client, database, source, monkeypatch, format
):
    second, artifact_id, release = await additional_candidate(
        database, source, source_id="502", seeders=99, format=format
    )
    calls = []

    async def resolve(owner, row, use_wedge=False):
        calls.append(row.id)
        return (
            (artifact_id, release) if row.id == second else (source["artifact"], source["release"])
        )

    monkeypatch.setattr(automatic, "resolve_candidate", resolve)
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    result = await detail(client, operation["id"])
    assert result["status"] == "completed"
    assert calls == [second if format == "m4b" else source["result"]]


async def test_failed_inspections_try_every_candidate_once_across_redelivery(
    client, database, source, monkeypatch
):
    for n in range(5):
        await additional_candidate(database, source, source_id=str(502 + n), seeders=8 - n)
    calls = []

    async def reject(owner, row, use_wedge=False):
        calls.append(row.id)
        raise AdapterError(FailureKind.PARSER, "Fixture parse failure")

    monkeypatch.setattr(automatic, "resolve_candidate", reject)
    operation = await start(client, source)
    for _ in range(7):
        await automatic.run(UUID(operation["id"]))
    result = await detail(client, operation["id"])
    assert result["status"] == "held" and result["inspections"] == 6
    assert len(calls) == len(set(calls)) == 6
    assert sum(d["inspected"] for d in result["decisions"]) == 6
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 0


async def test_resolved_release_from_another_source_result_cannot_be_selected(
    client, database, source
):
    source["release"] = source["release"].model_copy(update={"source_id": "999"})
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    result = await detail(client, operation["id"])
    assert result["status"] == "held" and "does not match" in result["message"]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 0


@pytest.mark.parametrize("second_actual_format", ["m4b", "flac"])
async def test_actual_formats_rerank_candidates_and_reuse_already_inspected_torrents(
    client, database, source, monkeypatch, second_actual_format
):
    second, artifact_id, release = await additional_candidate(
        database, source, source_id="502", seeders=2
    )
    async with database() as db, db.begin():
        for identifier, actual in [
            (source["artifact"], "mp3"),
            (artifact_id, second_actual_format),
        ]:
            artifact = await db.get(SourceArtifact, identifier)
            artifact.descriptor = {
                **artifact.descriptor,
                "files": [{"index": 0, "path": f"Harbor/Harbor.{actual}", "size_bytes": 12}],
            }
    calls = []

    async def resolve(owner, row, use_wedge=False):
        calls.append(row.id)
        return (
            (artifact_id, release) if row.id == second else (source["artifact"], source["release"])
        )

    monkeypatch.setattr(automatic, "resolve_candidate", resolve)
    operation = await start(client, source)
    await automatic.run(UUID(operation["id"]))
    assert (await detail(client, operation["id"]))["status"] == "queued"
    await automatic.run(UUID(operation["id"]))
    await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "completed" and value["inspections"] == 2
    assert calls == [source["result"], second]
    expected = artifact_id if second_actual_format == "m4b" else source["artifact"]
    assert value["artifact_id"] == str(expected)
    assert sum(d["selected"] for d in value["decisions"]) == 1
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(value["selection_id"]))
        assert selection.frozen["automatic_selection"]["inspected_formats"] == [
            "m4b" if second_actual_format == "m4b" else "mp3"
        ]


async def test_ranking_can_inspect_more_than_five_candidates_without_repeating_fetches(
    client, database, source, monkeypatch
):
    resolved = {source["result"]: (source["artifact"], source["release"])}
    for n in range(5):
        row, artifact, release = await additional_candidate(
            database, source, source_id=str(502 + n), seeders=8 - n
        )
        resolved[row] = artifact, release
    async with database() as db, db.begin():
        for artifact_id, _ in resolved.values():
            artifact = await db.get(SourceArtifact, artifact_id)
            artifact.descriptor = {
                **artifact.descriptor,
                "files": [{"index": 0, "path": "Harbor/Harbor.flac", "size_bytes": 12}],
            }
    calls = []

    async def resolve(owner, row, use_wedge=False):
        calls.append(row.id)
        return resolved[row.id]

    monkeypatch.setattr(automatic, "resolve_candidate", resolve)
    operation = await start(client, source)
    for _ in range(7):
        await automatic.run(UUID(operation["id"]))
    value = await detail(client, operation["id"])
    assert value["status"] == "completed" and value["inspections"] == 6
    assert len(calls) == len(set(calls)) == 6
    assert value["artifact_id"] == str(source["artifact"])
