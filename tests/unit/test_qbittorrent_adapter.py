import asyncio
import base64
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.qbittorrent import (
    QbitClient,
    absolute_path,
    magnet_hashes,
    parse_state,
    relative_torrent_path,
    verify_association,
)

HASH = "a" * 40
V2 = "b" * 64
TAG = "book-search:fixture-attempt"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&tr=https%3A%2F%2Ftracker.test%2Fprivate-key"


def row(**changes):
    return {
        "hash": HASH,
        "save_path": "/downloads/books/",
        "state": "stalledUP",
        "tags": TAG,
        "category": "book-search",
        "auto_tmm": False,
        "amount_left": 0,
        "total_size": 12,
        "progress": 1,
        **changes,
    }


def properties(**changes):
    return {
        "infohash_v1": HASH,
        "infohash_v2": "",
        "save_path": "/downloads/books",
        **changes,
    }


def files(**changes):
    return [
        {"index": 0, "name": "Book/book.m4b", "size": 12, "progress": 1, "priority": 1, **changes}
    ]


class Server:
    def __init__(self):
        self.requests = []
        self.torrents = []
        self.adds = 0
        self.lost_response = False
        self.version = "v5.2.3"
        self.files = files()
        self.save_path = "/downloads/books"
        self.refuse_rename = False

    async def __call__(self, request):
        self.requests.append(request)
        assert request.headers["origin"] == "http://qbit.test"
        assert request.headers["referer"] == "http://qbit.test/qbit/"
        path = request.url.path.removeprefix("/qbit/api/v2/")
        if path == "auth/login":
            assert parse_qs(request.content.decode()) == {
                "username": ["fixture-admin"],
                "password": ["fixture-password"],
            }
            return httpx.Response(204, headers={"set-cookie": "SID=fixture; Path=/"})
        assert request.headers["cookie"] == "SID=fixture"
        if path == "app/version":
            return httpx.Response(200, text=self.version)
        if path == "app/webapiVersion":
            return httpx.Response(200, text="2.15.1")
        if path == "torrents/add":
            self.adds += 1
            self.torrents.append(row())
            if self.lost_response:
                raise httpx.ReadTimeout("private response lost", request=request)
            return httpx.Response(
                200,
                json={
                    "success_count": 1,
                    "failure_count": 0,
                    "pending_count": 0,
                    "added_torrent_ids": [HASH],
                },
            )
        if path == "torrents/info":
            tag, key = request.url.params.get("tag"), request.url.params.get("hashes")
            return httpx.Response(
                200,
                json=[
                    t
                    for t in self.torrents
                    if (not tag or tag in t["tags"].split(",")) and (not key or t["hash"] == key)
                ],
            )
        if path == "torrents/properties":
            return httpx.Response(200, json=properties(save_path=self.save_path))
        if path == "torrents/files":
            return httpx.Response(200, json=self.files)
        if path == "torrents/renameFile":
            if self.refuse_rename:
                return httpx.Response(409, text="fails")
            body = parse_qs(request.content.decode())
            old, new = body["oldPath"][0], body["newPath"][0]
            renamed = False
            for item in self.files:
                if item["name"] == old:
                    item["name"] = new
                    renamed = True
            return httpx.Response(200 if renamed else 409, text="Ok." if renamed else "fails")
        if path == "torrents/setLocation":
            body = parse_qs(request.content.decode())
            self.save_path = body["location"][0]
            for torrent in self.torrents:
                torrent["save_path"] = self.save_path
            return httpx.Response(200, text="Ok.")
        if path == "app/getDirectoryContent":
            body = parse_qs(request.content.decode())
            self.listed = body["dirPath"][0]
            return httpx.Response(200, json=[".dewarr-route-test"])
        raise AssertionError(path)

    def client(self):
        return QbitClient(
            "http://qbit.test/qbit",
            "fixture-admin",
            "fixture-password",
            transport=httpx.MockTransport(self),
        )


async def test_submission_and_independent_confirmation_use_one_session_and_exact_parameters():
    server = Server()
    async with server.client() as client:
        receipt = await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert receipt.external_ids == [HASH] and not receipt.pending
        states = await client.find(attempt_tag=TAG, torrent_hash=HASH)
        assert len(states) == 1 and not states[0].association_verified
        confirmed = verify_association(
            states, tag=TAG, hashes={HASH}, save_path="/downloads/books", category="book-search"
        )
        assert confirmed.association_verified and confirmed.completed
        assert confirmed.files[0].relative_path == "Book/book.m4b"
        status = await client.status(HASH)
        assert status.completed and not status.association_verified
    assert server.adds == 1
    adds = [r for r in server.requests if r.url.path.endswith("torrents/add")]
    assert parse_qs(adds[0].content.decode(), keep_blank_values=True) == {
        "urls": [MAGNET],
        "tags": [TAG],
        "category": [""],
    }
    assert len([r for r in server.requests if r.url.path.endswith("auth/login")]) == 1


async def test_lost_add_response_can_be_observed_without_automatic_resubmission():
    server = Server()
    server.lost_response = True
    async with server.client() as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert error.value.kind == FailureKind.UNCERTAIN
        assert "private" not in str(error.value)
        states = await client.find(attempt_tag=TAG, torrent_hash=HASH)
        assert verify_association(
            states, tag=TAG, hashes={HASH}, save_path="/downloads/books", category="book-search"
        )
    assert server.adds == 1


async def test_matching_hash_without_attempt_tag_is_returned_but_never_adopted_or_retagged():
    server = Server()
    server.torrents = [row(tags="someone-elses-torrent")]
    async with server.client() as client:
        states = await client.find(attempt_tag=TAG, torrent_hash=HASH)
        with pytest.raises(AdapterError) as error:
            verify_association(
                states, tag=TAG, hashes={HASH}, save_path="/downloads/books", category="book-search"
            )
        assert error.value.kind == FailureKind.UNCERTAIN
    assert server.adds == 0
    assert all(r.method == "GET" or r.url.path.endswith("auth/login") for r in server.requests)


@pytest.mark.parametrize(
    "changes",
    [
        {"tags": {"different"}},
        {"category": "other"},
        {"save_path": "/other/path"},
        {"infohash_v1": "c" * 40},
    ],
)
def test_association_checks_all_frozen_evidence(changes):
    state = parse_state(row(), properties(), files()).model_copy(update=changes)
    with pytest.raises(AdapterError):
        verify_association(
            [state], tag=TAG, hashes={HASH}, save_path="/downloads/books", category="book-search"
        )


def test_ambiguous_tag_and_absent_transfer_are_not_success():
    state = parse_state(row(), properties(), files())
    kwargs = dict(tag=TAG, hashes={HASH}, save_path="/downloads/books", category="book-search")
    assert verify_association([], **kwargs) is None
    with pytest.raises(AdapterError):
        verify_association([state, state], **kwargs)


def test_v2_key_is_not_misrepresented_as_v1_hash_and_hybrid_checks_both_identities():
    state = parse_state(row(hash=V2[:40]), properties(infohash_v1="", infohash_v2=V2), files())
    assert state.identities == {V2} and state.external_id == V2[:40]
    assert verify_association(
        [state], tag=TAG, hashes={V2}, save_path="/downloads/books", category="book-search"
    )
    with pytest.raises(AdapterError):
        verify_association(
            [state], tag=TAG, hashes={V2[:40]}, save_path="/downloads/books", category="book-search"
        )
    hybrid = parse_state(row(), properties(infohash_v2=V2), files())
    assert verify_association(
        [hybrid], tag=TAG, hashes={HASH, V2}, save_path="/downloads/books", category="book-search"
    )


@pytest.mark.parametrize(
    "state", ["checkingUP", "checkingDL", "moving", "missingFiles", "error", "metaDL", "unknown"]
)
def test_progress_one_in_nonready_state_never_means_completed(state):
    assert not parse_state(row(state=state), properties(), files()).completed


@pytest.mark.parametrize(
    "torrent,contents",
    [
        (row(), []),
        (row(), files(progress=0.9)),
        (row(), files(priority=0)),
        (row(total_size=24), files()),
        (row(amount_left=1), files()),
        (row(progress=0.999), files()),
    ],
)
def test_completion_requires_whole_pack_file_evidence(torrent, contents):
    assert not parse_state(torrent, properties(), contents).completed


@pytest.mark.parametrize(
    "path",
    ["../book", "/book", "a/../../book", "a\\book", "C:/book", "C:book.m4b", "a//b", "a\x00b"],
)
def test_file_evidence_rejects_unsafe_paths(path):
    with pytest.raises(AdapterError) as error:
        parse_state(row(), properties(), files(name=path))
    assert error.value.kind == FailureKind.PARSER


def test_a_colon_in_a_title_is_a_torrent_file_name():
    name = "Book/Title: Subtitle.m4b"
    state = parse_state(row(), properties(), files(name=name))
    assert state.files[0].relative_path == name
    assert relative_torrent_path(name) == name
    assert relative_torrent_path("A: Novel.m4b") == "A: Novel.m4b"


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1", 1.1])
def test_invalid_progress_never_becomes_ready(value):
    with pytest.raises(AdapterError):
        parse_state(row(progress=value), properties(), files())


def test_duplicate_files_inconsistent_hash_and_path_changes_fail():
    for torrent, props, contents in [
        (row(), properties(), files() + files()),
        (row(), properties(infohash_v1="c" * 40), files()),
        (row(), properties(save_path="/moved/books"), files()),
        (row(), {"infohash_v1": HASH}, files()),
        (row(), properties(), files(size=True)),
    ]:
        with pytest.raises(AdapterError):
            parse_state(torrent, props, contents)


@pytest.mark.parametrize("value", ["/", "relative", "/a/../b", "/a//b", "/a\\b", "/a\nb"])
def test_download_paths_are_explicit_posix_destinations(value):
    with pytest.raises(ValueError):
        absolute_path(value)


def test_magnets_preserve_protocol_identity():
    assert magnet_hashes(MAGNET) == {HASH}
    b32 = base64.b32encode(bytes.fromhex(HASH)).decode()
    assert magnet_hashes(f"magnet:?xt=urn:btih:{b32}") == {HASH}
    assert magnet_hashes(f"magnet:?xt=urn:btih:{HASH}&xt=urn:btmh:1220{V2}") == {HASH, V2}


@pytest.mark.parametrize(
    "value",
    [
        "https://private.test/key.torrent",
        MAGNET + "\n" + MAGNET,
        "magnet:?dn=book",
        f"magnet:?xt=urn:btih:{V2}",
        "magnet:?xt=urn:btmh:bad",
        MAGNET + "&xt=urn:btih:" + "c" * 40,
    ],
)
def test_submission_artifact_rejects_multiple_or_unknown_identities(value):
    with pytest.raises(ValueError):
        magnet_hashes(value)


async def test_upload_sends_one_opaque_torrent_file_without_tracker_cookies_or_skip_checking():
    server = Server()
    async with server.client() as client:
        await client.submit(b"fixture torrent bytes", attempt_tag=TAG, save_path="/downloads/books")
    request = next(r for r in server.requests if r.url.path.endswith("torrents/add"))
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert request.content.count(b'filename="book.torrent"') == 1
    assert b"fixture torrent bytes" in request.content
    assert b"mam_id" not in request.content


@pytest.mark.parametrize(
    "status,body,kind",
    [
        (200, "Fails.", FailureKind.UNCERTAIN),
        (200, "<html>private</html>", FailureKind.UNCERTAIN),
        (302, "", FailureKind.UNCERTAIN),
        (500, "private detail", FailureKind.UNCERTAIN),
        (429, "private detail", FailureKind.UNCERTAIN),
        (403, "private detail", FailureKind.AUTHENTICATION),
        (415, "private detail", FailureKind.PARSER),
    ],
)
async def test_add_failures_never_retry_or_disclose_response(status, body, kind):
    server = Server()
    attempts = []

    async def handler(request):
        if request.url.path.endswith("torrents/add"):
            attempts.append(request)
            return httpx.Response(status, text=body, headers={"location": "http://other.test"})
        return await server(request)

    async with QbitClient(
        "http://qbit.test/qbit",
        "fixture-admin",
        "fixture-password",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert error.value.kind == kind and "private" not in str(error.value)
    assert len(attempts) == 1


async def test_unsupported_version_prevents_add():
    server = Server()
    server.version = "v4.6.7"
    async with server.client() as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert error.value.kind == FailureKind.UNSUPPORTED
    assert server.adds == 0


async def test_cancellation_propagates_without_a_second_add():
    server = Server()
    entered = asyncio.Event()
    adds = []

    async def handler(request):
        if request.url.path.endswith("torrents/add"):
            adds.append(request)
            entered.set()
            await asyncio.Event().wait()
        return await server(request)

    async with QbitClient(
        "http://qbit.test/qbit",
        "fixture-admin",
        "fixture-password",
        transport=httpx.MockTransport(handler),
    ) as client:
        task = asyncio.create_task(
            client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(adds) == 1


async def test_full_v2_hash_uses_truncated_lookup_but_verifies_full_identity():
    seen = []

    async def handler(request):
        seen.append(request)
        if request.url.path.endswith("auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("torrents/info"):
            if "hashes" in request.url.params:
                assert request.url.params["hashes"] == V2[:40]
            return httpx.Response(200, json=[row(hash=V2[:40])])
        if request.url.path.endswith("torrents/properties"):
            return httpx.Response(200, json=properties(infohash_v1="", infohash_v2=V2))
        return httpx.Response(200, json=files())

    async with QbitClient(
        "http://qbit.test", "user", "password", transport=httpx.MockTransport(handler)
    ) as client:
        states = await client.find(attempt_tag=TAG, torrent_hash=V2)
        assert len(states) == 1
        assert verify_association(
            states, tag=TAG, hashes={V2}, save_path="/downloads/books", category="book-search"
        ).association_verified
    assert len([r for r in seen if r.url.path.endswith("torrents/properties")]) == 1


@pytest.mark.parametrize("body", [b"Fails.", b"<form>secret</form>"])
async def test_login_rejection_does_not_attempt_submission(body):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=body)

    async with QbitClient(
        "http://qbit.test", "user", "password", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert error.value.kind == FailureKind.AUTHENTICATION
        assert "secret" not in str(error.value)
    assert len(requests) == 1 and requests[0].url.path.endswith("auth/login")


async def test_response_budget_covers_streamed_json_and_add_receipt(monkeypatch):
    monkeypatch.setattr("app.adapters.qbittorrent.MAX_RESPONSE", 64)
    server = Server()
    server.torrents = [row()]
    async with server.client() as client:
        with pytest.raises(AdapterError) as error:
            await client.find(attempt_tag=TAG, torrent_hash=HASH)
        assert error.value.kind == FailureKind.PARSER

    async def handler(request):
        if request.url.path.endswith("torrents/add"):
            return httpx.Response(200, content=b"x" * 65)
        return await server(request)

    async with QbitClient(
        "http://qbit.test/qbit",
        "fixture-admin",
        "fixture-password",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert error.value.kind == FailureKind.UNCERTAIN


async def test_missing_and_malformed_status_are_distinct():
    server = Server()
    async with server.client() as client:
        assert await client.find(attempt_tag=TAG, torrent_hash=HASH) == []
        with pytest.raises(AdapterError) as error:
            await client.status(HASH)
        assert error.value.kind == FailureKind.NOT_FOUND
        server.torrents = [row(), row(), row()]
        with pytest.raises(AdapterError) as error:
            await client.find(attempt_tag=TAG, torrent_hash=HASH)
        assert error.value.kind == FailureKind.PARSER


async def test_auth_expiry_during_add_is_not_automatically_retried():
    server = Server()
    adds = []

    async def handler(request):
        if request.url.path.endswith("torrents/add"):
            adds.append(request)
            return httpx.Response(403, text="expired")
        return await server(request)

    async with QbitClient(
        "http://qbit.test/qbit",
        "fixture-admin",
        "fixture-password",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AdapterError):
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
    assert len(adds) == 1
    assert len([r for r in server.requests if r.url.path.endswith("auth/login")]) == 1


@pytest.mark.parametrize(
    "status,receipt,expected_ids,pending",
    [
        (200, "Ok.", [], False),
        (
            202,
            {"success_count": 0, "failure_count": 0, "pending_count": 1, "added_torrent_ids": []},
            [],
            True,
        ),
    ],
)
async def test_legacy_and_async_receipts_are_acknowledgements_only(
    status, receipt, expected_ids, pending
):
    server = Server()

    async def handler(request):
        if request.url.path.endswith("torrents/add"):
            return httpx.Response(
                status, **({"json": receipt} if isinstance(receipt, dict) else {"text": receipt})
            )
        return await server(request)

    async with QbitClient(
        "http://qbit.test/qbit",
        "fixture-admin",
        "fixture-password",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert result.external_ids == expected_ids and result.pending == pending
        assert await client.find(attempt_tag=TAG, torrent_hash=HASH) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"success_count": True},
        {"success_count": 2, "added_torrent_ids": [HASH, HASH]},
        {"failure_count": 1},
        {"pending_count": 1},
        {"added_torrent_ids": ["all"]},
        {"added_torrent_ids": [V2]},
        {"added_torrent_ids": []},
    ],
)
async def test_malformed_or_multi_artifact_receipt_stays_uncertain(changes):
    server = Server()
    receipt = {
        "success_count": 1,
        "failure_count": 0,
        "pending_count": 0,
        "added_torrent_ids": [HASH],
        **changes,
    }

    async def handler(request):
        if request.url.path.endswith("torrents/add"):
            return httpx.Response(200, json=receipt)
        return await server(request)

    async with QbitClient(
        "http://qbit.test/qbit",
        "fixture-admin",
        "fixture-password",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.submit(MAGNET, attempt_tag=TAG, save_path="/downloads/books")
        assert error.value.kind == FailureKind.UNCERTAIN


def test_padding_requires_manifest_evidence_before_confirming_complete():
    state = parse_state(row(total_size=24), properties(), files())
    assert state.reported_complete
    assert not state.completed  # The caller must reconcile the known padding/file manifest.


@pytest.mark.parametrize("category_path", ["books", "", "/downloads/books"])
async def test_credential_free_connection_uses_server_defaults_without_logging_in(category_path):
    calls = []

    async def handler(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("app/version"):
            return httpx.Response(200, text="v5.2.3")
        if path.endswith("app/webapiVersion"):
            return httpx.Response(200, text="2.15.1")
        if path.endswith("app/preferences"):
            return httpx.Response(200, json={"save_path": "/downloads", "auto_tmm_enabled": True})
        if path.endswith("torrents/categories"):
            return httpx.Response(200, json={"books": {"savePath": category_path}})
        raise AssertionError(path)

    async with QbitClient(
        "http://qbit.test", "", "", transport=httpx.MockTransport(handler)
    ) as client:
        await client.capabilities()
        assert await client.download_location("books") == "/downloads/books"
    assert all(request.method == "GET" for request in calls)


def test_auto_managed_torrents_can_be_verified_without_changing_server_preferences():
    state = parse_state(row(auto_tmm=True), properties(), files())
    assert verify_association(
        [state], tag=TAG, hashes={HASH}, save_path="/downloads/books", category="book-search"
    ).association_verified


async def test_rename_and_move_are_explicit_and_a_refusal_is_definite():
    server = Server()
    server.torrents.append(row())
    async with server.client() as client:
        moved = await client.set_location(HASH, "/library")
        assert moved
        state = await client.status(HASH)
        assert state.save_path == "/library"
        renamed = await client.rename_file(HASH, "Book/book.m4b", "Author/Title.m4b")
        assert renamed
        assert (await client.status(HASH)).files[0].relative_path == "Author/Title.m4b"
        server.refuse_rename = True
        assert not await client.rename_file(HASH, "Author/Title.m4b", "Other.m4b")
    bodies = [
        parse_qs(request.content.decode())
        for request in server.requests
        if request.url.path.endswith(("torrents/setLocation", "torrents/renameFile"))
    ]
    assert bodies[0] == {"hashes": [HASH], "location": ["/library"]}
    assert bodies[1]["oldPath"] == ["Book/book.m4b"]
    assert bodies[1]["newPath"] == ["Author/Title.m4b"]


async def test_directory_listing_reads_the_path_qbittorrent_sees():
    server = Server()
    async with server.client() as client:
        assert await client.directory_entries("/library/books") == [".dewarr-route-test"]
    assert server.listed == "/library/books"
    listed = [
        request
        for request in server.requests
        if request.url.path.endswith("app/getDirectoryContent")
    ]
    assert len(listed) == 1 and listed[0].method == "POST"


def test_transfer_metrics_are_optional_and_unknown_eta_is_not_a_countdown():
    observed = parse_state(row(dlspeed=1048576, eta=120), properties(), files())
    assert observed.download_speed == 1048576 and observed.eta_seconds == 120
    observed = parse_state(row(dlspeed=0, eta=8640000), properties(), files())
    assert observed.download_speed == 0 and observed.eta_seconds is None
    observed = parse_state(row(dlspeed="invalid", eta=-1), properties(), files())
    assert observed.download_speed is None and observed.eta_seconds is None
