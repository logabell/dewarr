# ruff: noqa: F811
import asyncio
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    DownloadAttempt,
)
from app.domain import automatic_selection, list_requests
from app.domain.release_profiles import ProfileSnapshot, enforce_inspected_profile
from app.domain.request_constraints import PRIMARY_FORMATS
from tests.integration.test_acquisition import body, catalog, request  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_automatic_selection import source, start  # noqa: F401
from tests.integration.test_list_requests import preview, shelf, submit  # noqa: F401

pytestmark = pytest.mark.integration


def wanted(catalog, **constraints):
    return body(
        catalog, "audio", audio_library_id=str(catalog["library"]), download_constraints=constraints
    )


async def reservation(database, saved):
    async with database() as db:
        target = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == UUID(saved["request"]["id"]),
                AcquisitionTarget.slot == "audio",
            )
        )
        return await db.get(AcquisitionReservation, target.reservation_id)


async def test_concurrent_limits_keep_independent_intents_and_tighten_one_pending_transfer(
    client, database, catalog
):
    a, b = await asyncio.gather(
        request(client, wanted(catalog, maximum_bytes=100, blocked_formats=["mp3"])),
        request(client, wanted(catalog, maximum_bytes=50, blocked_formats=["flac"])),
    )
    assert a["request"]["id"] != b["request"]["id"]
    first, second = await reservation(database, a), await reservation(database, b)
    assert first.id == second.id
    assert second.requirements["download_constraints"] == {
        "blocked_formats": ["flac", "mp3"],
        "maximum_bytes": 50,
    }
    cancelled = await client.delete(
        f"/api/requests/{b['request']['id']}/reasons/{b['request']['reasons'][0]['id']}"
    )
    assert cancelled.status_code == 200
    remaining = await reservation(database, a)
    assert remaining.requirements["download_constraints"] == {
        "blocked_formats": ["mp3"],
        "maximum_bytes": 100,
    }


async def test_format_incompatibility_keeps_requests_separate(client, database, catalog):
    a = await request(
        client, wanted(catalog, blocked_formats=list(PRIMARY_FORMATS["audio"] - {"m4b"}))
    )
    b = await request(
        client, wanted(catalog, blocked_formats=list(PRIMARY_FORMATS["audio"] - {"mp3"}))
    )
    assert (await reservation(database, a)).id != (await reservation(database, b)).id


async def test_download_limits_do_not_trigger_replacement_of_owned_media(client, database, catalog):
    result = await request(
        client,
        body(
            catalog, "ebook", download_constraints={"maximum_bytes": 1, "blocked_formats": ["epub"]}
        ),
    )
    assert result["request"]["targets"][0]["state"] == "satisfied"
    assert (await client.get(f"/api/catalog/works/{catalog['work']}")).json()["availability"][
        "owned"
    ]
    async with database() as db:
        assert not await db.scalar(select(AcquisitionReservation.id))


@pytest.mark.parametrize("constraints", [{"blocked_formats": ["m4b"]}])
async def test_manual_selection_cannot_bypass_another_request_limit(
    client, database, catalog, selection_route, constraints
):
    await request(client, wanted(catalog, **constraints))
    response = await prepare(client, selection_route)
    assert response.status_code == 422, response.text
    async with database() as db:
        assert not await db.scalar(select(AcquisitionSelection.id))
        assert (await db.scalar(select(AcquisitionReservation))).state == "planned"


async def test_limits_freeze_into_import_profile_and_survive_request_withdrawal(
    client, database, catalog, selection_route
):
    strict = await request(client, wanted(catalog, blocked_formats=["flac"], maximum_bytes=24))
    response = await prepare(client, selection_route)
    assert response.status_code == 201, response.text
    async with database() as db:
        selected = await db.get(AcquisitionSelection, UUID(response.json()["id"]))
        frozen = deepcopy(selected.frozen)
        profile = ProfileSnapshot.model_validate(frozen["profile"])
        assert profile.preferences.maximum_bytes == 24 and profile.preferences.blocked_formats == [
            "flac"
        ]
    await client.delete(
        f"/api/requests/{strict['request']['id']}/reasons/{strict['request']['reasons'][0]['id']}"
    )
    async with database() as db:
        assert (await db.get(AcquisitionSelection, selected.id)).frozen == frozen
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="blocked format"):
        enforce_inspected_profile([{"extension": "flac", "identity": {"size": 12}}], profile)
    enforce_inspected_profile([{"extension": "m4b", "identity": {"size": 25}}], profile)


@pytest.mark.parametrize(
    "constraints, shared",
    [
        ({"blocked_formats": ["flac"], "maximum_bytes": 24}, True),
        ({"blocked_formats": ["m4b"]}, False),
        ({"maximum_bytes": 23}, True),
    ],
)
@pytest.mark.parametrize("committed", [False, True])
async def test_selected_payload_can_be_reused_only_when_it_proves_the_new_limits(
    client, database, catalog, selection_route, constraints, shared, committed, monkeypatch
):
    response = await prepare(client, selection_route)
    assert response.status_code == 201
    if committed:
        from app.config import get_settings
        from tests.integration.test_download_attempts import start as dispatch

        monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
        dispatched = await dispatch(client, response.json())
        assert dispatched.status_code == 202, dispatched.text
    async with database() as db:
        selected = await db.get(AcquisitionSelection, UUID(response.json()["id"]))
        frozen = deepcopy(selected.frozen)
    newer = await request(client, wanted(catalog, **constraints))
    assert ((await reservation(database, newer)).id == selected.reservation_id) is shared
    async with database() as db:
        assert (await db.get(AcquisitionSelection, selected.id)).frozen == frozen


async def test_legacy_requests_replay_across_migration_and_new_constraints_block_lossy_rollback(
    client, database, catalog
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import legacy_request_policy_fixture, migrate

    payload = body(catalog, "audio")
    legacy = await request(client, payload, "legacy-constraints-replay")
    assert "download_constraints" not in legacy["request"]["specification"]
    await get_engine().dispose()
    try:
        await legacy_request_policy_fixture(database)
        legacy = await request(client, payload, "legacy-constraints-replay")
        prior = await migrate("downgrade", "0028_capacity")
        assert prior.returncode == 0, prior.stderr
        upgraded = await migrate("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        repeated = await request(client, payload, "legacy-constraints-replay")
        assert repeated == legacy
        await request(client, wanted(catalog, maximum_bytes=100))
        await get_engine().dispose()
        async with database() as db:
            current_revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
        await legacy_request_policy_fixture(database)
        rejected = await migrate("downgrade", "0028_capacity")
        assert rejected.returncode != 0 and "pre-upgrade backup" in rejected.stderr
        async with database() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == current_revision
            )
            assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 2
    finally:
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stderr
        await get_engine().dispose()


@pytest.mark.parametrize("constraint", [{"blocked_formats": ["m4b"]}, {"maximum_bytes": 11}])
async def test_automatic_selection_inherits_shared_limits_before_fetching_or_preparing(
    client, database, catalog, source, constraint
):
    await request(client, wanted(catalog, **constraint))
    saved = await start(client, source)
    await automatic_selection.run(UUID(saved["id"]))
    result = (await client.get(f"/api/acquisition/automatic-selections/{saved['id']}")).json()
    assert result["status"] == ("held" if "blocked_formats" in constraint else "completed"), result
    async with database() as db:
        assert bool(await db.scalar(select(AcquisitionSelection.id))) == (
            "maximum_bytes" in constraint
        )
        assert not await db.scalar(select(DownloadAttempt.id))
    if "blocked_formats" in constraint:
        assert not source["resolver"].calls
    else:
        assert "maximum_bytes" not in result


async def test_list_preview_and_requests_preserve_limits_with_independent_manual_reason(
    client, database, catalog, shelf
):
    constraints = {"blocked_formats": ["mp3"], "maximum_bytes": 100}
    manual = await request(client, wanted(catalog, **constraints))
    plan = await preview(
        client,
        shelf,
        [catalog["work"]],
        mode="audio",
        audio_library_id=str(catalog["library"]),
        download_constraints=constraints,
    )
    assert plan["specification"]["download_constraints"] == constraints
    assert plan["counts"]["pending"] == 1
    await submit(client, shelf, plan)
    await list_requests.run(UUID(plan["id"]))
    await client.delete(f"/api/lists/{shelf}/entries/{catalog['work']}")
    detail = (await client.get(f"/api/requests/{manual['request']['id']}")).json()
    assert {r["kind"]: r["active"] for r in detail["reasons"]} == {"manual": True, "list": False}
    assert detail["specification"]["download_constraints"] == constraints
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 1
