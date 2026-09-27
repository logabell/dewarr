# ruff: noqa: F811
"""Native HTML + qBit metadata transport to real audio hardlinks and fixture ABS."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import libtorrent as lt
import pytest
from sqlalchemy import func, select

from app.adapters.torrent_descriptor import inspect_torrent
from app.config import get_settings
from app.db.models import (
    AutomaticImport,
    CatalogSeries,
    DownloadAttempt,
    DownloadFulfillment,
    ImportEntry,
    Integration,
    SeriesMembership,
)
from app.domain import automatic_selection, download_attempts
from app.importing import execution
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.integration.test_acquisition import body, request
from tests.integration.test_audiobookbay_sources import abb_http, configure  # noqa: F401
from tests.integration.test_download_attempts import Client
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_single_file_acquisition import prepare_audio_route
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("delayed", "naming"),
    [
        (False, "structured"),
        (True, "structured"),
        (False, "credits"),
        (False, "series"),
        (False, "structured-series"),
        (False, "study-guide"),
        (False, "wrong-series-volume"),
    ],
)
async def test_abb_automatic_acquisition_preserves_audio_and_confirms_inventory(
    client, admin, database, ready_route, abb_http, monkeypatch, delayed, naming
):
    route = ready_route
    work_id = route["plan"]["document"]["groups"][0]["work_id"]
    source = route["source"] / "selected.mp3"
    conflict = naming in {"study-guide", "wrong-series-volume"}
    file_title = {
        "study-guide": "First Harbor: A Study Guide",
        "wrong-series-volume": "First Harbor: Harbor Stories, Book 4",
    }.get(naming, "First Harbor: Harbor Stories, Book 5" if "series" in naming else "First Harbor")
    audio(
        source,
        title=file_title,
        author="Alex Morgan; Co Writer" if "series" in naming else "Alex Morgan",
        tags={"language": "en"},
    )
    await prepare_audio_route(client, database, route, work_id, source)
    epub(source.parent / "private-neighbor.epub", title="Unrelated")
    original = source.read_bytes()
    raw = lt.bencode(
        {
            b"info": {
                b"name": source.name.encode(),
                b"length": len(original),
                b"piece length": 16384,
                b"pieces": b"".join(
                    hashlib.sha1(original[pos : pos + 16384]).digest()
                    for pos in range(0, len(original), 16384)
                ),
            }
        }
    )
    descriptor = await inspect_torrent(raw)
    abb_http.update(
        raw=raw,
        descriptor=descriptor,
        replacements={
            "Harbor - Writer": "First Harbor - Alex Morgan",
            "Written by: Writer": "Written by: Alex Morgan",
            "Casey Reader": "Jordan Lee",
            "M4B": "MP3",
        },
    )
    if naming != "structured":
        abb_http["redirect_capitals"] = True
        abb_http["replacements"].update(
            {
                "Harbor - Writer": "Harbor Stories 5 - Alex Morgan"
                if "series" in naming
                else "First Harbor - Alex Morgan, Jordan Lee",
                "Written by: Writer": "Written by: Alex Morgan"
                if naming == "structured-series"
                else "",
            }
        )
    if "series" in naming:
        async with database() as db, db.begin():
            series = CatalogSeries(
                owner_id=UUID(admin["id"]),
                provider="fixture",
                external_id="harbor",
                name="Harbor Stories",
                fetched_at=datetime.now(UTC),
            )
            db.add(series)
            await db.flush()
            db.add(
                SeriesMembership(
                    series_id=series.id,
                    external_id="5",
                    work_id=UUID(work_id),
                    snapshot={"position": "5", "compilation": False},
                )
            )
    async with database() as db, db.begin():
        downloader = Integration(
            kind="qbittorrent",
            name="ABB fixture",
            base_url="http://qbit.test",
            encrypted_secrets=encrypt_secrets(
                {"username": "private-user", "password": "private-password"}
            ),
            credential_generation=1,
            status="connected",
            config={
                "save_path": "/downloads",
                "category": "book-search",
                "mappings": [
                    {
                        "download_root": "/downloads",
                        "source_key": "fixture",
                        "source_path": str(route["source"]),
                    }
                ],
            },
        )
        db.add(downloader)
        await db.flush()
        downloader_id = str(downloader.id)
    assert (await configure(client, metadata_downloader_id=downloader_id)).status_code == 200
    profile = await client.post(
        "/api/acquisition/profiles",
        json={"name": "ABB availability", "preferences": {"allow_unknown_seeders": True}},
    )
    assert profile.status_code == 201, profile.text
    approved = await client.put(
        f"/api/organization/destinations/{route['destination']['id']}/automatic-import",
        json={
            "enabled": True,
            "expected_generation": 0,
            "destination_revision": route["destination"]["revision"],
        },
    )
    assert approved.status_code == 200 and approved.json()["ready"], approved.text
    wanted = await request(
        client, body({"work": UUID(work_id)}, "audio", audio_library_id=route["library_id"])
    )
    search = await client.post(
        f"/api/catalog/works/{work_id}/source-searches",
        json={"medium": "audio", "profile_id": profile.json()["id"], "profile_generation": 1},
        headers={"Idempotency-Key": "abb-file-search"},
    )
    assert search.status_code == 202, search.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    qbit = Client(database, descriptor.model_dump(mode="json"))
    qbit.complete = True
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: qbit)
    route["scan_backend"].detect = not delayed
    command = {
        "intent_id": wanted["request"]["id"],
        "slot": "audio",
        "search_id": search.json()["id"],
        "downloader_id": downloader_id,
        "downloader_generation": 1,
        "destination_id": route["destination"]["id"],
        "destination_revision": route["destination"]["revision"],
        "download_when_ready": True,
    }
    response = await client.post(
        "/api/acquisition/automatic-selections",
        json=command,
        headers={"Idempotency-Key": "abb-file-auto"},
    )
    assert response.status_code == 202, response.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    selected = (
        await client.get(f"/api/acquisition/automatic-selections/{response.json()['id']}")
    ).json()
    assert selected["status"] == "completed" and selected["download_id"], selected
    if conflict:
        async with database() as db:
            receipt = await db.scalar(select(AutomaticImport))
            assert receipt and receipt.state == "held", receipt.message if receipt else "No receipt"
            assert not await db.scalar(select(ImportEntry.id))
            assert not await db.scalar(select(DownloadFulfillment.id))
        assert not list(route["target"].rglob("*.mp3"))
        return
    async with database() as db:
        entries = list(await db.scalars(select(ImportEntry)))
        assert len(entries) == 1
        entry = entries[0]
        assert entry.state == ("awaiting-library" if delayed else "confirmed"), entry.message
    if delayed:
        assert not (await client.get(f"/api/catalog/works/{work_id}")).json()["availability"][
            "owned"
        ]
        route["scan_backend"].detect = True
        route["scan_backend"].scan()
        await execution.execute(entry.operation_id)
        await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        assert (await db.get(ImportEntry, entry.id)).state == "confirmed"
        fulfilled = await db.scalar(select(DownloadFulfillment))
        assert fulfilled and fulfilled.import_entry_id == entry.id
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
    output = list(route["target"].rglob("*.mp3"))
    assert len(output) == 1 and output[0].stat().st_ino == source.stat().st_ino
    assert output[0].read_bytes() == original == source.read_bytes()
    assert not list(route["target"].rglob("private-neighbor.epub"))
    book = (await client.get(f"/api/catalog/works/{work_id}")).json()
    assert book["availability"]["owned"] and book["availability"]["audio"]
    repeated = await client.post(
        "/api/acquisition/automatic-selections",
        json=command,
        headers={"Idempotency-Key": "abb-file-auto"},
    )
    assert repeated.json()["download_id"] == selected["download_id"]
    await automatic_selection.run(UUID(selected["id"]))
    await download_attempts.run(UUID(selected["download_id"]))
    assert qbit.calls.count("submit") == 1
    assert "fetchMetadata" in abb_http["qbit_calls"]
    assert "add" not in abb_http["qbit_calls"]
