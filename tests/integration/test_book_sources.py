# ruff: noqa: F811
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.db.models import (
    AcquisitionProfile,
    AcquisitionSelection,
    Operation,
    SourceConnection,
    SourceResult,
    User,
    Work,
)
from app.domain import book_sources
from app.domain.book_sources import SearchInput
from app.jobs.retry import SourceSearchRetry
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_mam_sources import configure as configure_mam  # noqa: F401
from tests.integration.test_mam_sources import source_http  # noqa: F401
from tests.integration.test_prowlarr_sources import configure as configure_prowlarr  # noqa: F401
from tests.integration.test_prowlarr_sources import prowlarr_http  # noqa: F401
from tests.mam_fixture import release_row, search_response
from tests.prowlarr_fixture import release

pytestmark = pytest.mark.integration


async def test_popularity_profile_orders_live_source_observations_and_keeps_old_search_frozen(
    client, admin, database, catalog, source_http
):
    await configure_mam(client)
    criteria = ["format", "source", "popularity", "seeders"]
    invalid = await client.post(
        "/api/acquisition/profiles",
        json={
            "name": "Invalid",
            "preferences": {"criteria": ["popularity", "source", "format", "seeders"]},
        },
    )
    assert invalid.status_code == 422
    response = await client.post(
        "/api/acquisition/profiles",
        json={"name": "Popular MAM releases", "preferences": {"criteria": criteria}},
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    source_http["body"] = search_response(
        data=[
            release_row(
                id=1, title="Harbor", author_info='{"1":"Writer"}', seeders=100, times_completed=2
            ),
            release_row(
                id=2, title="Harbor", author_info='{"1":"Writer"}', seeders=1, times_completed=90
            ),
        ],
        found=2,
        total=2,
    )
    saved = await begin(client, catalog, profile_id=profile["id"], profile_generation=1)
    await book_sources.run(UUID(saved["id"]), "mam")
    observed = (await read(client, saved["id"])).json()
    assert [row["release"]["source_id"] for row in observed["items"]] == ["2", "1"]
    assert (
        "MAM reports 90 completed downloads; compared only within MAM"
        in observed["items"][0]["assessment"]["explanation"]
    )
    updated = await client.put(
        f"/api/acquisition/profiles/{profile['id']}",
        json={
            "name": "Seeders first",
            "expected_generation": 1,
            "preferences": {"criteria": ["seeders", "format", "source"]},
        },
    )
    assert updated.status_code == 200
    old = (await read(client, saved["id"])).json()
    assert old["profile"] == observed["profile"]
    assert old["items"] == observed["items"]
    fresh = await begin(
        client, catalog, key="new-popularity-policy", profile_id=profile["id"], profile_generation=2
    )
    await book_sources.run(UUID(fresh["id"]), "mam")
    assert [
        row["release"]["source_id"] for row in (await read(client, fresh["id"])).json()["items"]
    ] == ["1", "2"]


async def begin(client, catalog, key="book-search-fixture", **body):
    response = await client.post(
        f"/api/catalog/works/{catalog['work']}/source-searches",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def read(client, identifier):
    return await client.get(f"/api/source-searches/{identifier}")


async def test_incremental_ranked_private_results_preserve_mam_fields_and_ownership(
    client, admin, database, catalog, source_http, prowlarr_http
):
    await configure_mam(client)
    await configure_prowlarr(client)
    source_http["body"] = search_response(
        data=[release_row(title="Harbor", author_info='{"1":"Writer"}')]
    )
    prowlarr_http["releases"] = [release(title="Harbor")]
    saved = await begin(client, catalog)
    identifier = UUID(saved["id"])
    await book_sources.run(identifier, "mam")
    partial = (await read(client, identifier)).json()
    assert partial["status"] == "running" and len(partial["items"]) == 1
    assert partial["items"][0]["release"]["narrators"] == ["Jordan Lee"]
    assert partial["items"][0]["release"]["series"][0]["name"] == "Harbor Stories"
    await book_sources.run(identifier, "prowlarr")
    await book_sources.run(identifier, "mam")
    complete = (await read(client, identifier)).json()
    assert complete["status"] == "completed" and len(complete["items"]) == 2
    assert complete["items"][0]["release"]["source"] == "mam"
    assert complete["items"][0]["assessment"]["identity"] == "corroborated"
    assert {i["release"]["source"] for i in complete["items"]} == {"mam", "prowlarr"}
    assert "secret_proxy_link" not in str(complete)
    assert (await client.get(f"/api/catalog/works/{catalog['work']}")).json()["availability"][
        "ebook"
    ]
    again = await begin(client, catalog)
    assert again["id"] == saved["id"]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SourceResult)) == 2
        stored = await db.get(Operation, identifier)
        assert len(stored.payload["workers"]) == 2
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name='sources.search'"
                )
            )
            == 2
        )
    source_row = next(i for i in complete["items"] if i["release"]["source"] == "prowlarr")
    artifact = await client.post(
        f"/api/source-searches/{identifier}/results/{source_row['id']}/artifact"
    )
    assert artifact.status_code == 200, artifact.text
    assert artifact.json()["source_key"] == "prowlarr"


async def test_source_failure_retains_completed_sibling(
    client, admin, catalog, source_http, prowlarr_http
):
    await configure_mam(client)
    await configure_prowlarr(client)
    saved = await begin(client, catalog)
    await book_sources.run(UUID(saved["id"]), "mam")
    prowlarr_http["status"] = 503
    await book_sources.run(UUID(saved["id"]), "prowlarr")
    complete = (await read(client, saved["id"])).json()
    assert complete["status"] == "completed" and len(complete["items"]) == 1
    assert any(s["state"] == "failed" and s["key"] == "prowlarr" for s in complete["sources"])


async def test_stale_generation_and_identity_never_publish_new_results(
    client, admin, database, catalog, source_http
):
    await configure_mam(client)
    saved = await begin(client, catalog)
    await configure_mam(client, expected_generation=1)
    await book_sources.run(UUID(saved["id"]), "mam")
    stale = (await read(client, saved["id"])).json()
    assert stale["sources"][0]["state"] == "failed" and not stale["items"]
    assert not source_http["calls"]
    newer = await begin(client, catalog, key="new-search-fixture")
    async with database() as db, db.begin():
        (await db.get(Work, catalog["work"])).title = "Changed title"
    await book_sources.run(UUID(newer["id"]), "mam")
    stale = (await read(client, newer["id"])).json()
    assert stale["stale_identity"] and not stale["items"]


async def test_rate_limit_retry_preserves_progress_and_is_bounded(
    client, admin, database, catalog, source_http
):
    await configure_mam(client)
    saved = await begin(client, catalog)
    identifier = UUID(saved["id"])
    source_http.update(status=429, headers={"Retry-After": "30"})
    with pytest.raises(SourceSearchRetry):
        await book_sources.run(identifier, "mam")
    state = (await read(client, identifier)).json()
    assert state["sources"][0]["state"] == "queued"
    async with database() as db, db.begin():
        connection = await db.get(SourceConnection, "mam")
        connection.blocked_until = None
        connection.next_request_at = None
    source_http.update(status=200, headers={})
    await book_sources.run(identifier, "mam")
    assert len((await read(client, identifier)).json()["items"]) == 1
    await book_sources.run(identifier, "mam")
    assert len(source_http["calls"]) == 2


async def test_active_and_expired_worker_leases_and_exhausted_queue_projection(
    client, admin, database, catalog, prowlarr_http
):
    await configure_prowlarr(client)
    saved = await begin(client, catalog)
    identifier = UUID(saved["id"])
    async with database() as db, db.begin():
        op = await db.get(Operation, identifier)
        payload = deepcopy(op.payload)
        payload["workers"]["prowlarr"].update(
            token=str(uuid4()), until=(datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        )
        op.payload = payload
    with pytest.raises(SourceSearchRetry):
        await book_sources.run(identifier, "prowlarr")
    assert not prowlarr_http["calls"]
    async with database() as db, db.begin():
        op = await db.get(Operation, identifier)
        payload = deepcopy(op.payload)
        payload["workers"]["prowlarr"]["until"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        op.payload = payload
    await book_sources.run(identifier, "prowlarr")
    assert (await read(client, identifier)).json()["status"] == "completed"
    newer = await begin(client, catalog, key="failed-search-fixture")
    async with database() as db, db.begin():
        op = await db.get(Operation, UUID(newer["id"]))
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='aborted' WHERE id=:id"),
            {"id": op.job_id},
        )
    failed = (await read(client, newer["id"])).json()
    assert failed["status"] == "completed" and failed["sources"][0]["state"] == "failed"


async def test_search_owner_and_revocation_checks(client, admin, database, catalog, prowlarr_http):
    await configure_prowlarr(client)
    saved = await begin(client, catalog)
    prowlarr_http["gate"] = asyncio.Event()
    task = asyncio.create_task(book_sources.run(UUID(saved["id"]), "prowlarr"))
    await asyncio.wait_for(prowlarr_http["entered"].wait(), 5)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).active = False
    prowlarr_http["gate"].set()
    await task
    assert (await read(client, saved["id"])).status_code == 401
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SourceResult)) == 0
        operation = await db.get(Operation, UUID(saved["id"]))
        assert operation.status == "completed"


async def test_atomic_search_enqueue_rolls_back_and_profile_access_is_private(
    client, admin, database, catalog
):
    await configure_mam(client)
    async with database() as db:
        actor = await db.get(User, UUID(admin["id"]))
        await book_sources.start(db, actor, catalog["work"], SearchInput(), "rollback-search")
        await db.rollback()
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name='sources.search'"
                )
            )
            == 0
        )
    profile = (
        await client.post("/api/acquisition/profiles", json={"name": "Private preferences"})
    ).json()
    async with database() as db, db.begin():
        other = User(
            username="other", display_name="Other", password_hash="not-a-login", role="member"
        )
        db.add(other)
        await db.flush()
        (await db.get(AcquisitionProfile, UUID(profile["id"]))).owner_id = other.id
    assert len((await client.get("/api/acquisition/profiles")).json()) == 1
    response = await client.post(
        f"/api/catalog/works/{catalog['work']}/source-searches",
        json={"profile_id": profile["id"]},
        headers={"Idempotency-Key": "private-profile-search"},
    )
    assert response.status_code == 404


async def test_profiles_enforce_selection_and_keep_frozen_preferences(
    client, admin, database, selection_route
):
    blocked = (
        await client.post(
            "/api/acquisition/profiles",
            json={"name": "Small transfers", "preferences": {"maximum_bytes": 1}},
        )
    ).json()
    selected = {**selection_route, "profile_id": blocked["id"], "profile_generation": 1}
    assert (await prepare(client, selected)).status_code == 422
    updated = await client.put(
        f"/api/acquisition/profiles/{blocked['id']}",
        json={"name": "My downloads", "preferences": {}, "expected_generation": 1},
    )
    assert updated.status_code == 200
    assert (await prepare(client, selected)).status_code == 409
    selected["profile_generation"] = 2
    accepted = await prepare(client, selected)
    assert accepted.status_code == 201, accepted.text
    async with database() as db:
        frozen = (await db.get(AcquisitionSelection, UUID(accepted.json()["id"]))).frozen["profile"]
    await client.put(
        f"/api/acquisition/profiles/{blocked['id']}",
        json={
            "name": "Changed later",
            "preferences": {"maximum_bytes": 1},
            "expected_generation": 2,
        },
    )
    async with database() as db:
        assert (await db.get(AcquisitionSelection, UUID(accepted.json()["id"]))).frozen[
            "profile"
        ] == frozen


async def test_identity_changed_during_indexer_discovery_reaches_terminal_state(
    client, admin, database, catalog, prowlarr_http
):
    await configure_prowlarr(client)
    saved = await begin(client, catalog)
    prowlarr_http["gate"] = asyncio.Event()
    task = asyncio.create_task(book_sources.run(UUID(saved["id"]), "prowlarr"))
    await asyncio.wait_for(prowlarr_http["entered"].wait(), 5)
    async with database() as db, db.begin():
        (await db.get(Work, catalog["work"])).title = "New identity"
    prowlarr_http["gate"].set()
    await task
    value = (await read(client, saved["id"])).json()
    assert value["status"] == "completed" and value["stale_identity"]
    assert value["sources"][0]["state"] == "failed"


async def test_result_expiry_and_other_owner_cannot_inspect(
    client, admin, database, catalog, prowlarr_http
):
    await configure_prowlarr(client)
    saved = await begin(client, catalog)
    await book_sources.run(UUID(saved["id"]), "prowlarr")
    value = (await read(client, saved["id"])).json()
    identifier = value["items"][0]["id"]
    async with database() as db, db.begin():
        (await db.get(SourceResult, UUID(identifier))).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert (
        await client.post(f"/api/source-searches/{saved['id']}/results/{identifier}/artifact")
    ).status_code == 409
    async with database() as db, db.begin():
        other = User(
            username="other-searcher", display_name="Other", password_hash="no-login", role="member"
        )
        db.add(other)
        await db.flush()
        (await db.get(Operation, UUID(saved["id"]))).owner_id = other.id
    assert (await read(client, saved["id"])).status_code == 404


async def test_source_search_migration_guards_saved_profile_history(client, admin, database):
    from tests.integration.test_correction_migration import migrate

    async with database() as db:
        before = await db.scalar(text("SELECT version_num FROM alembic_version"))
    await client.post("/api/acquisition/profiles", json={"name": "Retain me"})
    downgraded = await migrate("downgrade", "0023_source_results")
    assert downgraded.returncode != 0 and "pre-upgrade backup" in downgraded.stderr
    async with database() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == before


@pytest.mark.parametrize("source", ["mam", "prowlarr"])
@pytest.mark.parametrize(
    "scenario", ["broaden", "precise", "custom", "page", "retry", "retry-page", "empty"]
)
async def test_empty_default_search_broadens_and_preserves_matching_and_pagination(
    client, admin, database, catalog, source_http, prowlarr_http, monkeypatch, source, scenario
):
    async with database() as db, db.begin():
        work = await db.get(Work, catalog["work"])
        work.title, work.authors = "Atmosphere: A Love Story", ["Taylor Jenkins Reid"]
    await (configure_mam(client) if source == "mam" else configure_prowlarr(client))
    original = book_sources.source_call if source == "mam" else book_sources.prowlarr_call
    calls = []
    primary = "Atmosphere: A Love Story Reid"
    chosen = primary if scenario == "precise" else "Atmosphere"

    async def provider(owner, action, query=None, **kwargs):
        if action == "search":
            calls.append((query.q, query.offset))
            if (scenario == "retry" and calls == [(primary, 0), ("Atmosphere Reid", 0)]) or (
                scenario == "retry-page"
                and calls[-1] == ("Atmosphere", 50)
                and calls.count(("Atmosphere", 50)) == 1
            ):
                from app.adapters.contracts import AdapterError, FailureKind

                raise AdapterError(FailureKind.RATE_LIMIT, "Retry fixture", retry_after=1)
            # A full first page establishes pagination. A later empty page must
            # stay on that query, rather than broadening to a different result set.
            found = scenario != "empty" and query.q == chosen and query.offset == 0
            count = 50 if scenario in {"page", "retry-page"} and found else int(found)
            if source == "mam":
                source_http["body"] = search_response(
                    data=[
                        release_row(
                            id=500 + i,
                            title="Atmosphere",
                            author_info='{"1":"Taylor Jenkins Reid"}',
                            series_info="{}",
                        )
                        for i in range(count)
                    ],
                    found=count,
                    total=count,
                )
            else:
                prowlarr_http["releases"] = [
                    release(
                        guid=f"https://tracker.test/item/{i}",
                        title="Taylor Jenkins Reid - Atmosphere [M4B]",
                    )
                    for i in range(count)
                ]
        return await original(owner, action, *(() if query is None else (query,)), **kwargs)

    monkeypatch.setattr(
        book_sources, "source_call" if source == "mam" else "prowlarr_call", provider
    )
    saved = await begin(
        client,
        catalog,
        q="custom m4b" if scenario == "custom" else primary,
        medium="audio",
        offset=50 if scenario in {"page", "retry-page"} else 0,
    )
    if scenario in {"retry", "retry-page"}:
        with pytest.raises(SourceSearchRetry):
            await book_sources.run(UUID(saved["id"]), source)
    await book_sources.run(UUID(saved["id"]), source)
    observed = (await read(client, saved["id"])).json()
    assert observed["status"] == "completed"
    expected = [(primary, 0), ("Atmosphere Reid", 0), ("Atmosphere", 0)]
    if scenario in {"page", "retry-page"}:
        expected.append(("Atmosphere", 50))
        if scenario == "retry-page":
            expected.append(("Atmosphere", 50))
    elif scenario == "retry":
        expected.insert(2, ("Atmosphere Reid", 0))
    elif scenario == "precise":
        expected = [(primary, 0)]
    elif scenario == "custom":
        expected = [("custom m4b", 0)]
    assert calls == expected
    if scenario in {"broaden", "precise", "retry"}:
        assert len(observed["items"]) == 1
        assert observed["items"][0]["assessment"]["identity"] == "corroborated"
    else:
        assert not observed["items"]
    unit = next(
        s for s in observed["sources"] if s["key"] == ("mam" if source == "mam" else "prowlarr:7")
    )
    assert unit["query"] == ("custom m4b" if scenario == "custom" else chosen)
