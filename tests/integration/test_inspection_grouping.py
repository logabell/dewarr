import asyncio
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import DownloadInspection, InspectionGrouping, Version, Work
from app.jobs.queue import get_queue
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route, start  # noqa: F401
from tests.integration.test_import_inspections import submit
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


@pytest.fixture
async def pack(client, admin, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    audio(root / "pack/one/01.mp3", track=1)
    audio(root / "pack/two/02.mp3", track=2)
    epub(root / "pack/book.epub")
    (root / "pack/notes.txt").write_text("Synthetic collection notes")
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": root})
    response = await submit(client)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    inspection = (await client.get(f"/api/organization/inspections/{response.json()['id']}")).json()
    grouping = (
        await client.get(f"/api/organization/inspections/{inspection['id']}/grouping")
    ).json()
    return root, inspection, grouping


def request(inspection, grouping):
    return {
        "inspection_revision": inspection["snapshot"]["revision"],
        "expected_revision": grouping["revision"],
        "groups": [
            {
                "files": [
                    {key: file.get(key) for key in ("path", "disc", "track")}
                    for file in group["files"]
                ]
            }
            for group in grouping["content"]["groups"]
        ],
        "excluded": deepcopy(grouping["content"]["excluded"]),
    }


def merge_audio(body):
    audio_groups = [group for group in body["groups"] if group["files"][0]["path"].endswith(".mp3")]
    body["groups"] = [group for group in body["groups"] if group not in audio_groups] + [
        {"files": [file for group in audio_groups for file in group["files"]]}
    ]
    return body


async def save(client, inspection, body):
    return await client.put(f"/api/organization/inspections/{inspection['id']}/grouping", json=body)


async def test_legacy_numbered_audio_uses_current_revision_for_review_and_matching(
    client, admin, database, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    for number in (1, 2):
        audio(root / f"pack/{number:02}.mp3", track="")
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": root})
    submitted = await submit(client)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    inspection_id = submitted.json()["id"]
    endpoint = f"/api/organization/inspections/{inspection_id}"
    async with database() as db, db.begin():
        row = await db.get(DownloadInspection, UUID(inspection_id))
        snapshot = deepcopy(row.snapshot)
        for group in snapshot["groups"]:
            for file in group["files"]:
                file["track"] = None  # An inspection saved before filename ordering.
        row.snapshot = snapshot

    inspection = (await client.get(endpoint)).json()
    response = await client.get(f"{endpoint}/grouping")
    assert response.status_code == 200, response.text
    grouping = response.json()
    assert grouping["revision"] != snapshot["revision"]
    assert [file["track"] for file in grouping["content"]["groups"][0]["files"]] == [1, 2]
    stale = await client.get(
        f"{endpoint}/matches", params={"grouping_revision": snapshot["revision"]}
    )
    assert stale.status_code == 409
    matched = await client.get(
        f"{endpoint}/matches", params={"grouping_revision": grouping["revision"]}
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["grouping_revision"] == grouping["revision"]

    # Saving/resetting the proposal must use the same revision as matching.
    reset = await save(
        client,
        inspection,
        {
            "inspection_revision": snapshot["revision"],
            "expected_revision": grouping["revision"],
            "action": "reset",
        },
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == grouping
    changed = request(inspection, grouping)
    for file in changed["groups"][0]["files"]:
        file["track"] = 3 - file["track"]
    reviewed = await save(client, inspection, changed)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["revision"] != grouping["revision"]
    fetched = (await client.get(f"{endpoint}/grouping")).json()
    assert fetched == reviewed.json()
    matched = await client.get(
        f"{endpoint}/matches", params={"grouping_revision": fetched["revision"]}
    )
    assert matched.status_code == 200, matched.text
    assert (await client.get(endpoint)).json()["snapshot"] == inspection["snapshot"]
    async with database() as db:
        assert (await db.get(DownloadInspection, UUID(inspection_id))).snapshot == snapshot


async def test_regroup_merge_split_and_restore_preserve_original_evidence(
    client, admin, database, pack
):
    root, inspection, grouping = pack
    original = {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    merged = await save(client, inspection, merge_audio(request(inspection, grouping)))
    assert merged.status_code == 200, merged.text
    assert len(merged.json()["content"]["groups"]) == 2
    audio_group = next(
        group for group in merged.json()["content"]["groups"] if group["medium"] == "audio"
    )
    assert [file["track"] for file in audio_group["files"]] == [1, 2]
    assert audio_group["identity"] == "unresolved" and audio_group["full_content"] == "unverified"
    split = request(inspection, grouping)
    split["expected_revision"] = merged.json()["revision"]
    split_result = await save(client, inspection, split)
    assert split_result.status_code == 200 and len(split_result.json()["content"]["groups"]) == 3
    reset = await save(
        client,
        inspection,
        {
            "inspection_revision": inspection["snapshot"]["revision"],
            "expected_revision": split_result.json()["revision"],
            "action": "reset",
        },
    )
    assert reset.status_code == 200, reset.text
    fetched = (await client.get(f"/api/organization/inspections/{inspection['id']}")).json()
    assert fetched["snapshot"] == inspection["snapshot"]
    assert reset.json()["revision"] != grouping["revision"]  # No ABA reuse of an old review token.
    assert original == {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize(
    "invalid", ["unknown", "duplicate", "omitted", "held", "mixed", "order", "forged"]
)
async def test_invalid_groupings_cannot_override_file_evidence(client, admin, pack, invalid):
    _, inspection, grouping = pack
    body = merge_audio(request(inspection, grouping))
    audio_group = next(
        group for group in body["groups"] if group["files"][0]["path"].endswith(".mp3")
    )
    ebook_group = next(
        group for group in body["groups"] if group["files"][0]["path"].endswith(".epub")
    )
    if invalid == "unknown":
        audio_group["files"][0]["path"] = "../outside.mp3"
    elif invalid == "duplicate":
        audio_group["files"].append(deepcopy(audio_group["files"][0]))
    elif invalid == "omitted":
        body["excluded"] = []
    elif invalid == "held":
        audio_group["files"].append({"path": "notes.txt"})
        body["excluded"] = []
    elif invalid == "mixed":
        audio_group["files"] += ebook_group["files"]
        body["groups"].remove(ebook_group)
    elif invalid == "order":
        audio_group["files"][1]["track"] = audio_group["files"][0]["track"]
    else:
        audio_group["files"][0]["complete"] = True
    result = await save(client, inspection, body)
    assert result.status_code == 422, result.text


async def test_concurrent_group_reviews_and_response_replay(client, admin, database, pack):
    _, inspection, grouping = pack
    merged = merge_audio(request(inspection, grouping))
    other = request(inspection, grouping)
    file = other["groups"].pop()["files"][0]
    other["excluded"].append({"path": file["path"], "reason": "Not requested"})
    responses = await asyncio.gather(
        save(client, inspection, merged), save(client, inspection, other)
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = merged if responses[0].status_code == 200 else other
    replay = await save(client, inspection, winner)
    assert replay.status_code == 200
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(InspectionGrouping)) == 1


async def test_grouping_revision_freezes_membership_and_invalidates_old_plans(
    client, admin, database, pack
):
    _, inspection, grouping = pack
    merged = (await save(client, inspection, merge_audio(request(inspection, grouping)))).json()
    async with database() as db, db.begin():
        work = Work(title="First Harbor", authors=["Alex Morgan"])
        db.add(work)
        await db.flush()
        version = Version(work_id=work.id, medium="audio", narrators=["Jordan Lee"])
        db.add(version)
        await db.flush()
        work_id, version_id = str(work.id), str(version.id)
    settings = (await client.get("/api/organization/settings")).json()
    group = next(group for group in merged["content"]["groups"] if group["medium"] == "audio")
    body = {
        "inspection_revision": inspection["snapshot"]["revision"],
        "profile_revision": settings["revision"],
        "grouping_revision": merged["revision"],
        "selections": [
            {
                "group_key": group["key"],
                "work_id": work_id,
                "version_id": version_id,
                "full_content": True,
            }
        ],
    }
    result = await client.post(f"/api/organization/inspections/{inspection['id']}/plans", json=body)
    assert result.status_code == 201, result.text
    document = result.json()["document"]
    assert len(document["files"]) == 2 and document["plan"]["expected_items"] == 1
    assert document["excluded_files"][0]["path"] == "notes.txt"
    reset = await save(
        client,
        inspection,
        {
            "inspection_revision": inspection["snapshot"]["revision"],
            "expected_revision": merged["revision"],
            "action": "reset",
        },
    )
    assert reset.status_code == 200
    stale = await client.post(f"/api/organization/inspections/{inspection['id']}/plans", json=body)
    assert stale.status_code == 409
    old = await client.get(f"/api/organization/plans/{result.json()['id']}")
    assert old.json()["document"] == document
    publish = await client.post(
        f"/api/organization/plans/{result.json()['id']}/imports",
        headers={"Idempotency-Key": "outdated-plan"},
        json={"plan_revision": result.json()["revision"], "destinations": {}},
    )
    assert publish.status_code == 409 and "groups changed" in publish.text


async def test_reserved_import_blocks_regrouping(client, admin, database, ready_route):  # noqa: F811
    route = ready_route
    result = await start(client, route)
    assert result.status_code == 202
    inspection_id = route["plan"]["inspection_id"]
    inspection = (await client.get(f"/api/organization/inspections/{inspection_id}")).json()
    grouping = (await client.get(f"/api/organization/inspections/{inspection_id}/grouping")).json()
    body = request(inspection, grouping)
    file = body["groups"].pop()["files"][0]
    body["excluded"].append({"path": file["path"], "reason": "Try changing active import"})
    changed = await save(client, inspection, body)
    assert changed.status_code == 409 and "reserved" in changed.text


async def test_grouping_is_owner_scoped_and_populated_history_blocks_downgrade(
    client, admin, database, pack
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import migrate

    _, inspection, grouping = pack
    await save(client, inspection, merge_audio(request(inspection, grouping)))
    await get_engine().dispose()
    try:
        result = await migrate("downgrade", "0011_import_runs")
        assert result.returncode != 0 and "File-group review history" in result.stderr
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0
        await get_engine().dispose()
    async with database() as db, db.begin():
        from app.db.models import User
        from app.security import hash_password

        other = User(
            username="other-curator",
            display_name="Other curator",
            password_hash=hash_password("another-fixture-password"),
            role="admin",
        )
        db.add(other)
        await db.flush()
        row = await db.get(DownloadInspection, UUID(inspection["id"]))
        row.owner_id = other.id
    hidden = await client.get(f"/api/organization/inspections/{inspection['id']}/grouping")
    assert hidden.status_code == 404
