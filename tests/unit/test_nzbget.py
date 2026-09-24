import base64
import json

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.nzbget import NzbClient, NzbState, verify_association
from tests.nzb_fixture import nzb_bytes

TAG = "book-search:attempt"


def open_client(handler, username="", password=""):
    return NzbClient(
        "http://nzb.test:6789",
        username,
        password,
        transport=httpx.MockTransport(handler),
    )


def group(**changes):
    row = {
        "NZBID": 42,
        "Kind": "NZB",
        "NZBName": "Finished Book",
        "Category": "books",
        "Status": "DOWNLOADING",
        "DestDir": "/downloads/books/Finished Book",
        "FinalDir": "",
        "DupeKey": TAG,
    }
    row.update(changes)
    return row


def config(category_dir="books"):
    return [
        {"Name": "DestDir", "Value": "/downloads/complete"},
        {"Name": "MainDir", "Value": "/downloads"},
        {"Name": "Category1.Name", "Value": "books"},
        {"Name": "Category1.DestDir", "Value": category_dir},
    ]


@pytest.fixture
def transport():
    calls = []

    def handler(request):
        calls.append(request)
        assert "private-nzb-password" not in str(request.url)
        assert request.headers["authorization"].startswith("Basic ")
        body = request.content
        assert b"private-nzb-password" not in body
        message = json.loads(body)
        method = message["method"]
        if method == "version":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": "25.4", "id": 1})
        if method == "config":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": config(), "id": 1})
        if method == "append":
            filename, content, category, priority, top, paused, key, score, mode = message["params"]
            assert filename == "book.nzb"
            assert base64.standard_b64decode(content) == nzb_bytes()
            assert category == "books"
            assert [priority, top, paused, key, score, mode] == [0, False, False, TAG, 0, "FORCE"]
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": 42, "id": 1})
        if method == "listgroups":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
        if method == "history":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "result": [
                        group(
                            Status="SUCCESS/ALL",
                            FinalDir="/downloads/books/Finished Book",
                        )
                    ],
                    "id": 1,
                },
            )
        raise AssertionError(method)

    return calls, httpx.MockTransport(handler)


async def test_connection_reads_category_folder_without_putting_the_password_in_the_url(transport):
    calls, mock = transport
    async with NzbClient(
        "http://nzb.test:6789", "private-user", "private-nzb-password", transport=mock
    ) as client:
        capabilities = await client.capabilities()
        assert capabilities.version == "25.4"
        assert capabilities.protocols == {"nzb"}
        assert await client.download_location("books") == "/downloads/complete/books"
        receipt = await client.submit(
            nzb_bytes(), attempt_tag=TAG, save_path="/downloads/books", category="books"
        )
        assert receipt.external_ids == ["42"]
        found = await client.find(attempt_tag=TAG, torrent_hash=None)
    assert found[0].completed
    assert found[0].save_path == "/downloads/books/Finished Book"
    assert calls
    assert all(str(call.url).endswith("/jsonrpc") for call in calls)


async def test_disabled_authentication_sends_no_authorization_header():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": "21.1", "id": 1})

    async with open_client(handler) as client:
        assert (await client.capabilities()).version == "21.1"


@pytest.mark.parametrize("version", ["21.1-r2311", "25.4", "26.3"])
async def test_supported_nzbget_versions(version):
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": version, "id": 1})

    async with open_client(handler) as client:
        assert (await client.capabilities()).version == version


@pytest.mark.parametrize(
    ("append_category", "expected"),
    [("yes", "/downloads/complete/books"), ("no", "/downloads/complete")],
)
def test_empty_category_folder_honors_append_category_dir(append_category, expected):
    from app.adapters.nzbget import category_folder

    options = {
        "DestDir": "/downloads/complete",
        "AppendCategoryDir": append_category,
        "Category1.Name": "books",
        "Category1.DestDir": "",
    }

    assert category_folder(options, "books") == expected


def test_missing_nzbget_category_is_rejected():
    from app.adapters.nzbget import category_folder, option_map

    with pytest.raises(AdapterError) as caught:
        category_folder(option_map(config()), "missing")

    assert caught.value.kind is FailureKind.NOT_FOUND
    assert "missing" in str(caught.value)


async def test_disabled_nzbget_history_is_rejected():
    def handler(request):
        method = json.loads(request.content)["method"]
        result = (
            "26.3"
            if method == "version"
            else config() + [{"Name": "KeepHistory", "Value": "0"}]
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})

    async with open_client(handler) as client:
        with pytest.raises(AdapterError) as caught:
            await client.download_location("books")

    assert caught.value.kind is FailureKind.UNSUPPORTED
    assert "history" in str(caught.value)


async def test_old_nzbget_is_unsupported():
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": "16.0", "id": 1})

    async with open_client(handler) as client:
        with pytest.raises(AdapterError) as caught:
            await client.capabilities()
    assert caught.value.kind is FailureKind.UNSUPPORTED


async def test_rejected_credentials_stay_an_authentication_failure():
    def handler(request):
        return httpx.Response(401)

    async with NzbClient(
        "http://nzb.test:6789", "user", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.capabilities()
    assert caught.value.kind is FailureKind.AUTHENTICATION


async def test_unconfirmed_append_stays_uncertain():
    def handler(request):
        method = json.loads(request.content)["method"]
        if method == "version":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": "24.7", "id": 1})
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": 0, "id": 1})

    async with open_client(handler) as client:
        with pytest.raises(AdapterError) as caught:
            await client.submit(
                nzb_bytes(), attempt_tag=TAG, save_path="/downloads", category="books"
            )
    assert caught.value.kind is FailureKind.UNCERTAIN


async def test_oversized_append_response_stays_uncertain():
    def handler(request):
        return httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","result":42,"id":1}' + b" " * 20,
        )

    async with open_client(handler) as client:
        with pytest.raises(AdapterError) as caught:
            await client._rpc("append", mutating=True, limit=20)

    assert caught.value.kind is FailureKind.UNCERTAIN


async def test_oversized_history_stays_retryable():
    def handler(request):
        method = json.loads(request.content)["method"]
        if method == "version":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": "25.4", "id": 1})
        if method == "listgroups":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": [], "id": 1})
        return httpx.Response(
            200, content=b'{"jsonrpc":"2.0","result":[]}' + b" " * (32 * 1024 * 1024)
        )

    async with open_client(handler) as client:
        with pytest.raises(AdapterError) as caught:
            await client.find(attempt_tag=TAG, torrent_hash=None)
    assert caught.value.kind is FailureKind.UNAVAILABLE


async def test_queue_duplicate_key_matches_before_history_completion():
    calls = []

    def handler(request):
        method = json.loads(request.content)["method"]
        calls.append(method)
        if method == "version":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": "21.2", "id": 1})
        if method == "listgroups":
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": [group()], "id": 1})
        raise AssertionError(method)

    async with open_client(handler) as client:
        found = await client.find(attempt_tag=TAG, torrent_hash=None)
    assert found[0].completed is False
    assert found[0].external_id == "42"
    assert found[0].dupe_key == TAG
    assert "history" not in calls


def test_failed_and_script_warning_history():
    failed = parse(group(Status="FAILURE/UNPACK", NZBID=7))
    warned = parse(
        group(Status="WARNING/SCRIPT", NZBID=8, FinalDir="/downloads/books/Finished Book")
    )
    space = parse(group(Status="WARNING/SPACE", NZBID=9))
    plain = parse(group(Status="SUCCESS/ALL", NZBID=10, FinalDir=""))
    assert failed.failed and not failed.completed
    assert warned.completed and warned.save_path == "/downloads/books/Finished Book"
    assert space.failed
    assert plain.completed and plain.save_path == "/downloads/books/Finished Book"


def parse(row):
    from app.adapters.nzbget import parse_group

    return parse_group(row, completed=True)


def test_completed_folder_must_sit_inside_the_saved_download_path():
    state = NzbState(
        external_id="42",
        state="SUCCESS/ALL",
        completed=True,
        save_path="/downloads/books/Finished Book",
        category="books",
        dupe_key=TAG,
        reported_complete=True,
    )
    verified = verify_association([state], tag=TAG, save_path="/downloads/books", category="books")
    assert verified.association_verified
    with pytest.raises(AdapterError) as caught:
        verify_association([state], tag=TAG, save_path="/downloads/other", category="books")
    assert caught.value.kind is FailureKind.UNCERTAIN


def test_two_matching_jobs_are_not_adopted():
    other = NzbState(
        external_id="43",
        state="DOWNLOADING",
        completed=False,
        save_path="/pending",
        category="books",
        dupe_key=TAG,
    )
    first = other.model_copy(update={"external_id": "42"})
    with pytest.raises(AdapterError) as caught:
        verify_association([first, other], tag=TAG, save_path="/downloads/books", category="books")
    assert caught.value.kind is FailureKind.UNCERTAIN
