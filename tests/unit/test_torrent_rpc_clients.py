import json

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.deluge import DelugeClient
from app.adapters.deluge import parse_state as deluge_state
from app.adapters.qbittorrent import verify_association
from app.adapters.torrent_rpc import verify_untagged
from app.adapters.transmission import TransmissionClient
from app.adapters.transmission import parse_state as transmission_state

HASH = "a" * 40
TAG = "book-search:test"
PATH = "/downloads/dewarr-" + "b" * 32


def transmission_row(**overrides):
    return {
        "hashString": HASH,
        "downloadDir": "/downloads",
        "labels": [TAG, "books"],
        "status": 6,
        "percentDone": 1,
        "totalSize": 10,
        "leftUntilDone": 0,
        "error": 0,
        "files": [{"name": "Book/book.m4b", "length": 10, "bytesCompleted": 10}],
        "fileStats": [{"wanted": True}],
        **overrides,
    }


def deluge_row(**overrides):
    return {
        "hash": HASH,
        "save_path": PATH,
        "label": "books",
        "state": "Seeding",
        "progress": 100,
        "total_size": 10,
        "is_finished": True,
        "is_auto_managed": False,
        "move_completed": False,
        "files": [{"path": "Book/book.m4b", "size": 10}],
        "file_progress": [1],
        "file_priorities": [1],
        **overrides,
    }


async def test_transmission_handshake_submit_and_observation():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if request.headers.get("X-Transmission-Session-Id") != "session":
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "session"})
        values = {
            "session-get": {"rpc-version": 17, "version": "4.0.6", "download-dir": "/downloads"},
            "torrent-add": {"torrent-added": {"hashString": HASH}},
            "torrent-get": {"torrents": [transmission_row()]},
        }
        return httpx.Response(
            200, json={"result": "success", "arguments": values.get(payload["method"], {})}
        )

    async with TransmissionClient(
        "http://transmission", transport=httpx.MockTransport(handler)
    ) as client:
        assert "attempt-tagging" in (await client.capabilities()).operations
        assert await client.download_location("books") == "/downloads"
        await client.submit(b"torrent", attempt_tag=TAG, save_path="/downloads", category="books")
        states = await client.find(attempt_tag=TAG, torrent_hash=HASH)
        assert verify_association(
            states, tag=TAG, hashes={HASH}, save_path="/downloads", category="books"
        ).completed
        await client.pause(HASH)
        await client.resume(HASH)
    adds = [call for call in calls if call["method"] == "torrent-add"]
    assert len(adds) == 1
    assert adds[0]["arguments"]["labels"] == [TAG, "books"]


@pytest.mark.parametrize("client_class", [TransmissionClient, DelugeClient])
async def test_mutation_timeout_never_retries_add(client_class):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload["method"])
        if "add_torrent" in payload["method"] or payload["method"] == "torrent-add":
            raise httpx.ReadTimeout("fixture")
        if client_class is TransmissionClient:
            return httpx.Response(
                200, json={"result": "success", "arguments": {"rpc-version": 17, "version": "4"}}
            )
        result = {
            "auth.login": True,
            "web.connected": True,
            "daemon.info": "2.2",
            "core.get_enabled_plugins": [],
        }.get(payload["method"])
        return httpx.Response(200, json={"id": payload["id"], "result": result, "error": None})

    async with client_class("http://client", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError) as exc:
            await client.submit(b"torrent", attempt_tag=TAG, save_path=PATH, category="")
        assert exc.value.kind == FailureKind.UNCERTAIN
    assert sum("add_torrent" in method or method == "torrent-add" for method in calls) == 1


async def test_deluge_label_and_completed_folder_without_global_mutations():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        result = {
            "auth.login": True,
            "web.connected": True,
            "daemon.info": "2.2",
            "core.get_enabled_plugins": ["Label"],
            "label.get_labels": ["books"],
            "label.get_options": {
                "apply_move_completed": True,
                "move_completed": True,
                "move_completed_path": "/complete",
            },
            "core.get_config_values": {"download_location": "/incomplete", "move_completed": False},
            "core.add_torrent_file": HASH,
            "core.get_torrents_status": {HASH: deluge_row()},
        }.get(payload["method"])
        return httpx.Response(200, json={"id": payload["id"], "result": result, "error": None})

    async with DelugeClient(
        "http://deluge", password="secret", transport=httpx.MockTransport(handler)
    ) as client:
        assert await client.download_location("books") == "/complete"
        await client.submit(b"torrent", attempt_tag=TAG, save_path=PATH, category="books")
        states = await client.find(attempt_tag=TAG, torrent_hash=HASH)
        assert verify_untagged(states, hashes={HASH}, save_path=PATH, category="books").completed
    methods = [call["method"] for call in calls]
    assert methods.count("core.add_torrent_file") == 1
    assert "label.set_torrent" in methods
    assert not any("remove" in m or "rename" in m or "move_storage" in m for m in methods)
    add = next(c for c in calls if c["method"] == "core.add_torrent_file")
    assert add["params"][2]["move_completed"] is False


def test_untagged_requires_unique_path_and_full_identity():
    state = deluge_state(HASH, deluge_row())
    for path, hashes, states in [
        ("/downloads", {HASH}, [state]),
        (PATH, {"c" * 40}, [state]),
        (PATH, {HASH}, [state, state]),
    ]:
        with pytest.raises(AdapterError):
            verify_untagged(states, hashes=hashes, save_path=path, category="books")


@pytest.mark.parametrize(
    "parser,row",
    [(lambda r: deluge_state(HASH, r), deluge_row), (transmission_state, transmission_row)],
)
async def test_bad_paths_and_incomplete_files_never_import(parser, row):
    if row is deluge_row:
        bad = row(files=[{"path": "../escape.m4b", "size": 10}])
        incomplete = row(file_progress=[0.5])
    else:
        bad = row(files=[{"name": "../escape.m4b", "length": 10, "bytesCompleted": 10}])
        incomplete = row(files=[{"name": "Book/book.m4b", "length": 10, "bytesCompleted": 5}])
    with pytest.raises(AdapterError):
        parser(bad)
    assert not parser(incomplete).completed


async def test_deluge_duplicate_race_does_not_label_or_resume_an_existing_transfer():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload["method"])
        values = {
            "auth.login": True,
            "web.connected": True,
            "daemon.info": "2.2",
            "core.get_enabled_plugins": ["Label"],
            "label.get_labels": ["books"],
            "label.get_options": {},
            "core.add_torrent_file": HASH,
            "core.get_torrents_status": {HASH: deluge_row(save_path="/someone-else")},
        }
        return httpx.Response(
            200, json={"id": payload["id"], "result": values[payload["method"]], "error": None}
        )

    async with DelugeClient("http://deluge", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(b"torrent", attempt_tag=TAG, save_path=PATH, category="books")
        assert error.value.kind == FailureKind.UNCERTAIN
    assert "label.set_torrent" not in calls
    assert "core.resume_torrent" not in calls
    assert "core.set_torrent_options" not in calls
