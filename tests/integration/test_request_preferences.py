# ruff: noqa: F401, F811
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.models import AcquisitionIntent, AcquisitionReason, AcquisitionTarget, Operation
from app.domain import automatic_selection, book_sources, list_requests
from tests.integration.test_acquisition_defaults import save as defaults
from tests.integration.test_automatic_selection import detail, start
from tests.integration.test_list_policies import (
    activate,
    add,
    authorized,
    catalog,
    policy_fixture,
    preview,
    selection_route,
    source,
    tick,
)

pytestmark = pytest.mark.integration


async def request(client, f, *, overrides=None, reason=True, constraints=None, key=None):
    body = {
        "work_id": f["work"],
        "specification": {"mode": "audio"},
        "release_preferences": {"overrides": overrides or {}},
    }
    if constraints:
        body["specification"]["download_constraints"] = constraints
    if reason:
        body["reason"] = {"list_id": f["list"]}
    plan = await client.post("/api/requests/preview", json=body)
    assert plan.status_code == 200, plan.text
    body["expected_preference_revision"] = plan.json()["release_policy"]["effective_revision"]
    result = await client.post(
        "/api/requests", json=body, headers={"Idempotency-Key": key or str(uuid4())}
    )
    assert result.status_code == 202, result.text
    return result.json()["request"]


async def test_all_five_layers_and_explicit_clearing_preserve_independent_limits(
    client, admin, database, policy_fixture
):
    f = policy_fixture
    await defaults(client, {"source_order": ["prowlarr", "mam"]}, "installation")
    await defaults(client, {"criteria": ["seeders", "source", "format"]})
    profile = (
        await client.post(
            "/api/acquisition/profiles",
            json={
                "name": "Recordings",
                "preferences": {"audio_formats": ["mp3", "m4b"]},
            },
        )
    ).json()
    await add(client, f)
    await activate(
        client,
        f,
        await preview(
            client,
            f,
            mode="manual",
            profile_id=profile["id"],
            profile_generation=profile["generation"],
            preference_overrides={
                "audio_formats": ["m4b", "mp3"],
                "blocked_formats": ["flac"],
                "maximum_bytes": 20000,
            },
        ),
    )
    item = await request(
        client,
        f,
        overrides={"audio_formats": ["mp3", "m4b"], "blocked_formats": [], "maximum_bytes": None},
        constraints={"maximum_bytes": 10000, "blocked_formats": ["ogg"]},
    )
    effective = item["release_policy"]
    assert effective["origins"]["source_order"] == "Installation default"
    assert effective["origins"]["criteria"] == "Personal default"
    assert effective["origins"]["audio_formats"] == "Request override"
    assert effective["preferences"]["maximum_bytes"] is None
    assert effective["preferences"]["blocked_formats"] == []
    assert item["specification"]["download_constraints"] == {
        "maximum_bytes": 10000,
        "blocked_formats": ["ogg"],
    }
    async with database() as db:
        intent = await db.get(AcquisitionIntent, UUID(item["id"]))
        reason = await db.scalar(
            select(AcquisitionReason).where(AcquisitionReason.intent_id == intent.id)
        )
        assert intent.release_policy == reason.release_policy == effective


async def test_distinct_list_preferences_keep_snapshots_and_share_compatible_reservation(
    client, database, policy_fixture
):
    f = policy_fixture
    await add(client, f)
    await activate(
        client,
        f,
        await preview(
            client,
            f,
            mode="manual",
            preference_overrides={"criteria": ["seeders", "source", "format"]},
        ),
    )
    first = await request(client, f)
    other = {
        **f,
        "list": (await client.post("/api/lists", json={"name": "Other rankings"})).json()["id"],
    }
    await add(client, other)
    await activate(
        client,
        other,
        await preview(
            client,
            other,
            mode="manual",
            preference_overrides={"criteria": ["source", "format", "seeders"]},
        ),
    )
    second = await request(client, other)
    assert first["id"] != second["id"]
    async with database() as db:
        targets = list(
            await db.scalars(
                select(AcquisitionTarget).where(
                    AcquisitionTarget.intent_id.in_([UUID(first["id"]), UUID(second["id"])])
                )
            )
        )
        assert len(targets) == 2 and targets[0].reservation_id == targets[1].reservation_id
    assert (
        first["release_policy"]["preferences"]["criteria"]
        != second["release_policy"]["preferences"]["criteria"]
    )


async def test_request_preview_rejects_changed_defaults_and_replay_retains_accepted_policy(
    client, admin, policy_fixture
):
    f = policy_fixture
    body = {"work_id": f["work"], "specification": {"mode": "audio"}}
    plan = (await client.post("/api/requests/preview", json=body)).json()
    body["expected_preference_revision"] = plan["release_policy"]["effective_revision"]
    await defaults(client, {"audio_formats": ["mp3", "m4b"]})
    rejected = await client.post(
        "/api/requests", json=body, headers={"Idempotency-Key": "stale-policy-request"}
    )
    assert rejected.status_code == 409
    saved = await request(client, f, reason=False, key="frozen-policy-request")
    await defaults(client, {"audio_formats": ["m4b", "mp3"]})
    historical = (await client.get(f"/api/requests/{saved['id']}")).json()
    assert historical["release_policy"] == saved["release_policy"]
    replay = await client.post(
        "/api/requests",
        json={
            "work_id": f["work"],
            "specification": {"mode": "audio"},
            "release_preferences": {"overrides": {}},
            "expected_preference_revision": saved["release_policy"]["effective_revision"],
        },
        headers={"Idempotency-Key": "frozen-policy-request"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["request"]["id"] == saved["id"]
    assert replay.json()["request"]["release_policy"] == saved["release_policy"]


async def test_bound_source_search_uses_list_and_request_overrides_and_checks_work(
    client, database, policy_fixture
):
    f = policy_fixture
    await add(client, f)
    await activate(
        client,
        f,
        await preview(
            client, f, mode="manual", preference_overrides={"source_order": ["prowlarr", "mam"]}
        ),
    )
    wanted = await request(client, f, overrides={"criteria": ["seeders", "format", "source"]})
    result = await client.post(
        f"/api/catalog/works/{f['work']}/source-searches",
        json={"request_id": wanted["id"]},
        headers={"Idempotency-Key": "bound-policy-source"},
    )
    assert result.status_code == 202, result.text
    assert result.json()["request_id"] == wanted["id"]
    profile = result.json()["profile"]
    assert profile["preferences"]["source_order"] == ["prowlarr", "mam"]
    assert profile["preferences"]["criteria"] == ["seeders", "format", "source"]
    other = (
        await client.post("/api/catalog/works", json={"title": "Unrelated", "authors": ["Writer"]})
    ).json()["id"]
    wrong = await client.post(
        f"/api/catalog/works/{other}/source-searches",
        json={"request_id": wanted["id"]},
        headers={"Idempotency-Key": "wrong-book-policy"},
    )
    assert wrong.status_code == 409
    unavailable = await client.post(
        f"/api/catalog/works/{f['work']}/source-searches",
        json={"request_id": str(uuid4())},
        headers={"Idempotency-Key": "missing-policy"},
    )
    assert unavailable.status_code == 404


async def test_manual_batch_freezes_list_overrides_and_rejects_policy_edits_after_preview(
    client, database, policy_fixture
):
    f = policy_fixture
    await add(client, f)
    active = await activate(
        client,
        f,
        await preview(
            client, f, mode="manual", preference_overrides={"audio_formats": ["mp3", "m4b"]}
        ),
    )
    body = {
        "work_ids": [f["work"]],
        "specification": {"mode": "audio"},
        "release_preferences": {"overrides": {"criteria": ["source", "format", "seeders"]}},
    }
    plan = await client.post(
        f"/api/lists/{f['list']}/requests/preview",
        json=body,
        headers={"Idempotency-Key": "batch-profile-plan"},
    )
    assert plan.status_code == 200, plan.text
    saved = plan.json()
    assert saved["release_policy"]["origins"]["audio_formats"] == "List override"
    await activate(
        client,
        f,
        await preview(
            client,
            f,
            mode="manual",
            expected_revision=active["revision"],
            preference_overrides={"audio_formats": ["m4b", "mp3"]},
        ),
    )
    rejected = await client.post(f"/api/lists/{f['list']}/requests/{saved['id']}/submit")
    assert rejected.status_code == 409
    plan = (
        await client.post(
            f"/api/lists/{f['list']}/requests/preview",
            json=body,
            headers={"Idempotency-Key": "batch-profile-new-plan"},
        )
    ).json()
    submitted = await client.post(f"/api/lists/{f['list']}/requests/{plan['id']}/submit")
    assert submitted.status_code == 202
    await list_requests.run(UUID(plan["id"]))
    result = (await client.get(f"/api/lists/{f['list']}/requests/{plan['id']}")).json()
    assert result["status"] == "completed"
    intent = (await client.get(f"/api/requests/{result['receipt'][0]['request_id']}")).json()
    assert intent["release_policy"] == result["release_policy"]


async def test_list_overrides_reach_automatic_selection_and_download_once(
    client, database, policy_fixture
):
    f = policy_fixture
    await add(client, f)
    active = await activate(
        client,
        f,
        await preview(
            client,
            f,
            include_work_ids=[f["work"]],
            preference_overrides={
                "criteria": ["seeders", "source", "format"],
                "audio_formats": ["mp3", "m4b"],
            },
        ),
    )
    for _ in range(4):
        await tick(database, active, force_books=True)
    assert f["source"]["qbit"].calls.count("submit") == 1
    async with database() as db:
        from app.db.models import AcquisitionSelection

        selections = list(await db.scalars(select(AcquisitionSelection)))
        assert len(selections) == 1
        snapshot = selections[0].frozen["profile"]
        assert snapshot["preferences"]["criteria"] == ["seeders", "source", "format"]
        assert snapshot["origins"]["criteria"] == "List override"


async def test_all_blocked_formats_are_rejected_before_creating_request(client, policy_fixture):
    f = policy_fixture
    response = await client.post(
        "/api/requests/preview",
        json={
            "work_id": f["work"],
            "specification": {"mode": "audio"},
            "release_preferences": {
                "overrides": {"blocked_formats": ["m4b", "mp3", "flac", "aac", "ogg", "opus"]}
            },
        },
    )
    assert response.status_code == 422


async def bound_search(client, f, wanted):
    result = await client.post(
        f"/api/catalog/works/{f['work']}/source-searches",
        json={"request_id": wanted["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert result.status_code == 202, result.text
    return result.json()


async def test_request_search_polling_does_not_switch_to_a_different_request(
    client, policy_fixture
):
    f = policy_fixture
    first = await request(
        client, f, reason=False, overrides={"criteria": ["seeders", "format", "source"]}
    )
    second = await request(
        client, f, reason=False, overrides={"criteria": ["source", "seeders", "format"]}
    )
    searches = [await bound_search(client, f, item) for item in (first, second)]
    for wanted, search in zip((first, second), searches, strict=True):
        response = await client.get(
            f"/api/catalog/works/{f['work']}/source-searches/latest",
            params={"request_id": wanted["id"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == search["id"]
        assert response.json()["profile"] == wanted["release_policy"]
    absent = await client.get(
        f"/api/catalog/works/{f['work']}/source-searches/latest",
        params={"request_id": str(uuid4())},
    )
    assert absent.status_code == 404


async def test_manual_selection_rejects_other_request_and_freezes_bound_policy(
    client, database, policy_fixture
):
    from app.db.models import AcquisitionSelection

    f = policy_fixture
    wanted = await request(
        client, f, reason=False, overrides={"criteria": ["seeders", "format", "source"]}
    )
    other = await request(
        client, f, reason=False, overrides={"criteria": ["source", "seeders", "format"]}
    )
    search = await bound_search(client, f, wanted)
    route = {k: v for k, v in f["source"]["body"].items() if k != "download_when_ready"}
    body = {
        **route,
        "intent_id": other["id"],
        "search_id": search["id"],
        "artifact_id": str(f["source"]["artifact"]),
        "confirmed_work_id": f["work"],
    }
    response = await client.post(
        "/api/acquisition/selections",
        json=body,
        headers={"Idempotency-Key": "wrong-request-snapshot"},
    )
    assert response.status_code == 409, response.text
    body["intent_id"] = wanted["id"]
    response = await client.post(
        "/api/acquisition/selections",
        json=body,
        headers={"Idempotency-Key": "correct-request-snapshot"},
    )
    assert response.status_code == 201, response.text
    async with database() as db:
        selected = await db.get(AcquisitionSelection, UUID(response.json()["id"]))
        assert selected.frozen["profile"] == search["profile"]


async def test_bound_search_ignores_legacy_request_size_limit(client, database, policy_fixture):
    from app.jobs.queue import get_queue

    f = policy_fixture
    wanted = await request(
        client, f, reason=False, constraints={"maximum_bytes": 1}, overrides={"maximum_bytes": None}
    )
    search = await bound_search(client, f, wanted)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    result = (await client.get(f"/api/source-searches/{search['id']}")).json()
    assert result["items"] and not result["items"][0]["assessment"]["blocked"]
    body = {
        **{k: v for k, v in f["source"]["body"].items() if k != "download_when_ready"},
        "intent_id": wanted["id"],
        "search_id": search["id"],
        "artifact_id": str(f["source"]["artifact"]),
        "confirmed_work_id": f["work"],
    }
    response = await client.post(
        "/api/acquisition/selections",
        json=body,
        headers={"Idempotency-Key": "independent-limit-selection"},
    )
    assert response.status_code == 201, response.text


async def test_saved_request_policy_blocks_lossy_migration_downgrade(
    client, database, policy_fixture
):
    from tests.integration.test_correction_migration import migrate

    await request(client, policy_fixture, reason=False)
    result = await migrate("downgrade", "0031_acquisition_defaults")
    assert result.returncode != 0
    assert "discarding request decisions" in result.stderr


async def test_list_series_search_override_reaches_request_bound_source_queries(
    client, database, policy_fixture
):
    from app.db.models import WorkMetadataSource

    f = policy_fixture
    async with database() as db, db.begin():
        metadata = await db.scalar(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id == UUID(f["work"]))
        )
        metadata.snapshot = {
            **metadata.snapshot,
            "series": [
                {"external_id": "series-search", "name": "Harbor Cycle", "compilation": False}
            ],
        }
    await defaults(client, {"search_series": False})
    await add(client, f)
    await activate(
        client,
        f,
        await preview(client, f, mode="manual", preference_overrides={"search_series": True}),
    )
    item = await request(client, f)
    assert item["release_policy"]["origins"]["search_series"] == "List override"
    searched = await client.post(
        f"/api/catalog/works/{f['work']}/source-searches",
        json={"request_id": item["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert searched.status_code == 202, searched.text
    assert len(searched.json()["query_plan"]["queries"]) == 2
    title_only = await request(client, f, overrides={"search_series": False})
    searched = await client.post(
        f"/api/catalog/works/{f['work']}/source-searches",
        json={"request_id": title_only["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert searched.status_code == 202, searched.text
    assert len(searched.json()["query_plan"]["queries"]) == 1
    assert searched.json()["profile"]["origins"]["search_series"] == "Request override"
