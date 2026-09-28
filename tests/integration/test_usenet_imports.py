# ruff: noqa: F811
"""Completed NZB output reaches the shared importer and confirmed library."""

import json
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.contracts import SubmissionReceipt
from app.adapters.nzbget import NzbClient
from app.adapters.sabnzbd import SabClient
from app.config import get_settings
from app.db.models import AutomaticImport, DownloadFulfillment, ImportEntry, Integration
from app.domain import automatic_selection
from app.domain import download_attempts as downloads
from app.importing import automatic
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.integration.test_acquisition import body, request
from tests.integration.test_acquisition_selections import prepare
from tests.integration.test_download_attempts import start
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_inspection_matching import edition
from tests.integration.test_prowlarr_sources import (
    configure,
    prowlarr_http,  # noqa: F401
    resolve,
    search,
)
from tests.media_fixtures import epub
from tests.nzb_fixture import nzb_bytes
from tests.prowlarr_fixture import release

pytestmark = pytest.mark.integration


class CompletedClient:
    """Real history parsing/association, with a synthetic completed transfer."""

    def __init__(self, kind, output):
        self.kind, self.output = kind, output
        self.tag = None
        self.submissions = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def capabilities(self):
        pass

    async def submit(self, content, *, attempt_tag, save_path, category):
        assert b"<nzb" in content and save_path == "/downloads"
        self.tag, self.category = attempt_tag, category
        self.submissions += 1
        return SubmissionReceipt(external_ids=["42"])

    async def find(self, **kwargs):
        def handler(req):
            if self.kind == "sabnzbd":
                mode = parse_qs(req.content.decode())["mode"][0]
                if mode == "version":
                    return httpx.Response(200, json={"version": "5.1.3"})
                rows = []
                if self.tag and mode == "history":
                    rows = [
                        {
                            "nzo_id": "42",
                            "name": self.tag.replace(":", "_"),
                            "nzb_name": "book.nzb",
                            "category": self.category,
                            "status": "Completed",
                            "storage": self.output,
                        }
                    ]
                return httpx.Response(200, json={mode: {"slots": rows}})
            method = json.loads(req.content)["method"]
            result = "26.3" if method == "version" else []
            if self.tag and method == "history":
                result = [
                    {
                        "NZBID": 42,
                        "Kind": "NZB",
                        "Name": "Renamed download",
                        "Category": self.category,
                        "DupeKey": self.tag,
                        "Status": "SUCCESS/ALL",
                        "FinalDir": self.output,
                    }
                ]
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})

        adapter = SabClient if self.kind == "sabnzbd" else NzbClient
        credentials = ("fixture-key",) if self.kind == "sabnzbd" else ("", "")
        async with adapter(
            "http://client.test", *credentials, transport=httpx.MockTransport(handler)
        ) as client:
            return await client.find(**kwargs)


@pytest.mark.parametrize("kind", ["sabnzbd", "nzbget"])
@pytest.mark.parametrize(
    "scenario",
    ["single-file", "extracted-folder", "retry-held", "untagged", "wrong-book", "invalid-file"],
)
async def test_completed_usenet_output_imports_once(
    client,
    admin,
    database,
    ready_route,
    prowlarr_http,
    monkeypatch,
    kind,
    scenario,
    automatically=False,
):
    route = ready_route
    work_id = route["plan"]["document"]["groups"][0]["work_id"]
    source = route["source"] / "finished/renamed.epub"
    epub(
        source,
        title="Other Book"
        if scenario == "wrong-book"
        else ""
        if scenario == "untagged"
        else "First Harbor",
        author="" if scenario == "untagged" else "Alex Morgan",
        isbn="9781234567897",
    )
    if scenario == "invalid-file":
        source.write_bytes(b"not an EPUB")
    original = source.read_bytes()
    epub(route["source"] / "unrelated.epub", title="Unrelated Book")
    await edition(database, work_id=UUID(work_id))
    # Encoded article sizes and archive subjects are not the output manifest.
    await configure(client)
    prowlarr_http.update(
        releases=[
            release(
                title="Alex Morgan - First Harbor [EPUB]",
                protocol="usenet",
                categories=[{"id": 7020}],
            )
        ],
        bytes=nzb_bytes(name="Article collection", filename="encoded.rar", size=99999),
    )
    found = (await search(client)).json()["items"][0]
    artifact = await resolve(client, found["id"])
    assert artifact.status_code == 200, artifact.text
    async with database() as db, db.begin():
        downloader = Integration(
            kind=kind,
            name="Fixture Usenet",
            base_url="http://client.test",
            encrypted_secrets=encrypt_secrets({}),
            credential_generation=1,
            status="connected",
            config={
                "save_path": "/downloads",
                "category": "books",
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
    wanted = await request(
        client,
        body(
            {"work": work_id},
            "ebook",
            ebook_library_id=route["library_id"],
            download_constraints={"maximum_bytes": 1} if automatically else None,
        ),
    )
    prepared = (
        None
        if automatically
        else await prepare(
            client,
            {
                "intent_id": wanted["request"]["id"],
                "slot": "ebook",
                "artifact_id": artifact.json()["id"],
                "downloader_id": downloader_id,
                "downloader_generation": 1,
                "destination_id": route["destination"]["id"],
                "destination_revision": route["destination"]["revision"],
                "confirmed_work_id": work_id,
            },
        )
    )
    if prepared is not None:
        assert prepared.status_code == 201, prepared.text
    approved = await client.put(
        f"/api/organization/destinations/{route['destination']['id']}/automatic-import",
        json={
            "enabled": True,
            "expected_generation": 0,
            "destination_revision": route["destination"]["revision"],
        },
    )
    assert approved.status_code == 200 and approved.json()["ready"], approved.text
    output = (
        "/downloads/finished/renamed.epub" if scenario == "single-file" else "/downloads/finished"
    )
    grab = CompletedClient(kind, output)
    monkeypatch.setattr(
        downloads, "SabClient" if kind == "sabnzbd" else "NzbClient", lambda *args: grab
    )
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    manifest_check = automatic.manifest_matches
    if scenario == "retry-held":

        def legacy_check(*args):
            raise HTTPException(
                409, "Inspected files differ from the completed release; review its contents"
            )

        monkeypatch.setattr(automatic, "manifest_matches", legacy_check)
    if automatically:
        searched = await client.post(
            f"/api/catalog/works/{work_id}/source-searches",
            json={"medium": "ebook"},
            headers={"Idempotency-Key": "automatic-usenet-search"},
        )
        assert searched.status_code == 202, searched.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
        selected = await client.post(
            "/api/acquisition/automatic-selections",
            json={
                "intent_id": wanted["request"]["id"],
                "slot": "ebook",
                "search_id": searched.json()["id"],
                "downloader_id": downloader_id,
                "downloader_generation": 1,
                "destination_id": route["destination"]["id"],
                "destination_revision": route["destination"]["revision"],
                "download_when_ready": True,
            },
            headers={"Idempotency-Key": "automatic-usenet-download"},
        )
        assert selected.status_code == 202, selected.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
        selection = (
            await client.get(f"/api/acquisition/automatic-selections/{selected.json()['id']}")
        ).json()
        assert selection["status"] == "completed", selection
        download_id = selection["download_id"]
        await automatic_selection.run(UUID(selected.json()["id"]))
    else:
        started = await start(client, prepared.json())
        assert started.status_code == 202, started.text
        download_id = started.json()["id"]
    await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        auto = await db.scalar(select(AutomaticImport))
        assert auto is not None
        inspection_id = auto.inspection_id
    endpoint = f"/api/organization/inspections/{inspection_id}"
    if scenario == "retry-held":
        held = (await client.get(endpoint)).json()
        assert held["download"]["state"] == "held" and held["download"]["can_retry"]
        assert "Inspected files differ" in held["download"]["message"]
        monkeypatch.setattr(automatic, "manifest_matches", manifest_check)
        retried = await client.post(endpoint + "/retry")
        assert retried.status_code == 202, retried.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        auto = await db.get(AutomaticImport, auto.id)
        entries = list(await db.scalars(select(ImportEntry)))
        fulfilled = await db.scalar(select(DownloadFulfillment))
        if scenario in {"wrong-book", "invalid-file"}:
            assert auto.state == "held", auto.message
            assert not entries and not fulfilled
            assert not list(route["target"].rglob("*.epub"))
            return
        assert auto.state == "importing", (auto.message, auto.evidence)
        assert len(entries) == 1 and entries[0].state == "confirmed"
        assert fulfilled and fulfilled.import_entry_id == entries[0].id
    imported = list(route["target"].rglob("*.epub"))
    assert len(imported) == 1
    assert imported[0].read_bytes() == original == source.read_bytes()
    assert imported[0].stat().st_ino == source.stat().st_ino
    assert (route["source"] / "unrelated.epub").exists()
    assert (await client.get(endpoint)).json()["download"]["state"] == "complete"
    ledger = (await client.get(f"/api/requests/{wanted['request']['id']}")).json()
    assert ledger["targets"][0]["state"] == "satisfied"
    await automatic.run(auto.id)
    await downloads.run(UUID(download_id))
    assert grab.submissions == 1


@pytest.mark.parametrize("kind", ["sabnzbd", "nzbget"])
@pytest.mark.parametrize("scenario", ["extracted-folder", "untagged", "wrong-book"])
async def test_automatic_usenet_search_download_and_import(
    client, admin, database, ready_route, prowlarr_http, monkeypatch, kind, scenario
):
    await test_completed_usenet_output_imports_once(
        client,
        admin,
        database,
        ready_route,
        prowlarr_http,
        monkeypatch,
        kind,
        scenario,
        automatically=True,
    )
