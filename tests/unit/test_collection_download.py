from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.contracts import AdapterError
from app.adapters.qbittorrent import parse_state, verify_association
from app.domain.download_attempts import transfer_stage
from tests.unit.test_qbittorrent_adapter import HASH, MAGNET, TAG, Server, properties, row


def inventory():
    return [
        {"index": 3, "name": "Pack/One.m4b", "size": 12, "progress": 1, "priority": 1},
        {"index": 9, "name": "Pack/Two.m4b", "size": 24, "progress": 0, "priority": 0},
    ]


def test_selected_completion_uses_frozen_files_not_total_pack_or_skipped_leftovers():
    state = parse_state(row(), properties(), inventory())
    selection = SimpleNamespace(
        frozen={
            "selected_paths": ["Pack/One.m4b"],
            "descriptor": {
                "files": [{"path": f["name"], "size_bytes": f["size"]} for f in inventory()]
            },
        }
    )
    assert not state.completed
    assert transfer_stage(selection, state)[0] == "complete"
    state.files[1].priority = 1
    assert transfer_stage(selection, state)[0] == "held"
    state.files[1].priority = 0
    state.files[0].size_bytes += 1
    assert transfer_stage(selection, state)[0] == "held"


@pytest.mark.parametrize("refuse", [False, True])
async def test_stopped_priority_handshake_uses_actual_sparse_indexes_and_readback(refuse):
    class SelectedServer(Server):
        async def __call__(self, request):
            path = request.url.path.rsplit("/", 1)[-1]
            if path == "filePrio":
                self.requests.append(request)
                body = parse_qs(request.content.decode())
                for f in self.files:
                    if str(f["index"]) in body["id"][0].split("|") and not refuse:
                        f["priority"] = int(body["priority"][0])
                return httpx.Response(200)
            if path == "start":
                self.requests.append(request)
                assert self.files[0]["priority"] == 1 and self.files[1]["priority"] == 0
                return httpx.Response(200)
            response = await super().__call__(request)
            if path == "add":
                assert parse_qs(request.content.decode())["stopped"] == ["true"]
                self.torrents[0]["state"] = "stoppedDL"
            return response

    server = SelectedServer()
    server.files = inventory()
    server.files[1]["priority"] = 1
    async with server.client() as client:
        await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books", stopped=True)
        state = verify_association(
            await client.find(attempt_tag=TAG, torrent_hash=HASH),
            tag=TAG,
            hashes={HASH},
            save_path="/downloads/books",
            category="book-search",
        )
        expected = {f["name"]: f["size"] for f in inventory()}
        if refuse:
            with pytest.raises(AdapterError):
                await client.select_files(state, expected, ["Pack/One.m4b"])
        else:
            await client.select_files(state, expected, ["Pack/One.m4b"])
            await client.start_transfer(HASH)
    assert server.adds == 1
    assert sum(r.url.path.endswith("/start") for r in server.requests) == (0 if refuse else 1)
