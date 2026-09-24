from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.sabnzbd import SabClient
from tests.nzb_fixture import nzb_bytes


def completed(name="book-search_attempt"):
    return {
        "nzo_id": "SABnzbd_nzo_finished",
        "nzb_name": name,
        "name": "Finished Book",
        "category": "books",
        "status": "Completed",
        "storage": "/downloads/books/Finished Book",
        "bytes": 4096,
        "fail_message": "",
    }


@pytest.fixture(params=["3.7.2", "4.5.1", "5.0.0", "5.1.1", "5.1.3"])
def transport(request):
    version = request.param
    calls = []

    def handler(request):
        calls.append(request)
        assert "private-sab-key" not in str(request.url)
        assert request.method == "POST"
        assert "x-api-key" not in request.headers
        mode = request.url.params["mode"]
        if mode == "addfile":
            assert request.headers["content-type"].startswith("multipart/form-data;")
            assert b'name="apikey"\r\n\r\nprivate-sab-key\r\n' in request.content
            assert nzb_bytes() in request.content
        else:
            assert parse_qs(request.content.decode())["apikey"] == ["private-sab-key"]
        if mode == "version":
            return httpx.Response(200, json={"version": version})
        if mode == "get_config" and request.url.params["section"] == "misc":
            return httpx.Response(
                200, json={"config": {"misc": {"complete_dir": "/downloads/complete"}}}
            )
        if mode == "get_config":
            return httpx.Response(
                200,
                json={
                    "config": {
                        "categories": [
                            {"name": "*", "dir": ""},
                            {"name": "books", "dir": "books"},
                        ]
                    }
                },
            )
        if mode == "addfile":
            assert request.url.params["nzbname"] == "book-search_attempt"
            assert request.url.params["cat"] == "books"
            return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_added"]})
        if mode == "queue":
            if "search" in request.url.params:
                assert request.url.params["search"] == "book-search_attempt"
            return httpx.Response(200, json={"queue": {"slots": []}})
        if mode == "history":
            row = completed()
            # SAB's search matches names, not the nzo_id. Status needs nzo_ids.
            search = request.url.params.get("search", "")
            identifier = request.url.params.get("nzo_ids", row["nzo_id"])
            matches = search in row["nzb_name"] and identifier == row["nzo_id"]
            return httpx.Response(200, json={"history": {"slots": [row] if matches else []}})
        raise AssertionError(mode)

    return calls, httpx.MockTransport(handler), version


async def test_connection_reads_category_folder_without_logging_the_key(transport):
    calls, mock, version = transport
    async with SabClient("http://sab.test:8080", "private-sab-key", transport=mock) as client:
        capabilities = await client.capabilities()
        assert capabilities.version == version
        assert capabilities.protocols == {"nzb"}
        assert await client.download_location("books") == "/downloads/complete/books"
    assert calls
    assert all(call.url.params["output"] == "json" for call in calls)


@pytest.mark.parametrize("version", ["4.3.2", "5.1.3"])
async def test_queue_filename_suffix_still_matches_the_attempt(version):
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "version":
            return httpx.Response(200, json={"version": version})
        if mode == "queue":
            return httpx.Response(
                200,
                json={
                    "queue": {
                        "slots": [
                            {
                                "nzo_id": "SABnzbd_nzo_queued",
                                "filename": "book-search_attempt.nzb",
                                "cat": "books",
                                "status": "Downloading",
                            }
                        ]
                    }
                },
            )
        if mode == "history":
            return httpx.Response(200, json={"history": {"slots": []}})
        raise AssertionError(mode)

    async with SabClient(
        "http://sab.test:8080", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        found = await client.find(attempt_tag="book-search:attempt", torrent_hash=None)
    assert found[0].names == {
        "book-search_attempt.nzb",
        "book-search_attempt",
        "book-search:attempt",
    }
    assert not found[0].completed


async def test_oversized_job_page_stays_retryable():
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "version":
            return httpx.Response(200, json={"version": "4.5.1"})
        if mode == "queue":
            return httpx.Response(
                200,
                json={
                    "queue": {
                        "slots": [
                            {
                                "nzo_id": f"SABnzbd_nzo_{index}",
                                "filename": "book-search:attempt",
                                "cat": "books",
                                "status": "Downloading",
                            }
                            for index in range(21)
                        ]
                    }
                },
            )
        raise AssertionError(mode)

    async with SabClient(
        "http://sab.test:8080", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.find(attempt_tag="book-search:attempt", torrent_hash=None)
    assert caught.value.kind == FailureKind.UNAVAILABLE


async def test_submit_and_find_use_the_attempt_name(transport):
    _, mock, _ = transport
    async with SabClient("http://sab.test:8080", "private-sab-key", transport=mock) as client:
        receipt = await client.submit(
            nzb_bytes(),
            attempt_tag="book-search:attempt",
            save_path="/downloads/complete/books",
            category="books",
        )
        assert receipt.external_ids == ["SABnzbd_nzo_added"]
        found = await client.find(attempt_tag="book-search:attempt", torrent_hash=None)
    assert len(found) == 1
    assert found[0].completed
    assert found[0].save_path == "/downloads/books/Finished Book"
    assert found[0].names == {"book-search_attempt", "book-search:attempt"}


async def test_unconfirmed_add_stays_uncertain():
    def handler(request):
        if request.url.params["mode"] == "version":
            return httpx.Response(200, json={"version": "4.5.1"})
        return httpx.Response(200, json={"status": False, "error": "private nzb body"})

    async with SabClient(
        "http://sab.test:8080", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.submit(
                nzb_bytes(),
                attempt_tag="book-search:attempt",
                save_path="/downloads",
                category="books",
            )
    assert caught.value.kind == FailureKind.UNCERTAIN
    assert "private" not in str(caught.value)


async def test_bad_api_key_and_old_version_are_rejected():
    def handler(request):
        if parse_qs(request.content.decode()).get("apikey") == ["bad"]:
            return httpx.Response(200, json={"error": "API Key Incorrect"})
        return httpx.Response(200, json={"version": "2.0.0"})

    async with SabClient(
        "http://sab.test", "bad", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.capabilities()
        assert caught.value.kind == FailureKind.AUTHENTICATION
    async with SabClient(
        "http://sab.test", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.capabilities()
        assert caught.value.kind == FailureKind.UNSUPPORTED


async def test_status_reads_completed_job(transport):
    calls, mock, _ = transport
    async with SabClient("http://sab.test:8080", "private-sab-key", transport=mock) as client:
        state = await client.status("SABnzbd_nzo_finished")
    assert state.external_id == "SABnzbd_nzo_finished"
    assert state.completed and not state.failed
    assert state.save_path == "/downloads/books/Finished Book"
    assert {call.url.params["mode"] for call in calls} == {"version", "queue", "history"}


@pytest.mark.parametrize(
    "version", ["2.3.9", "6.0.0", "15.1.3", "5.1.3RC1", "5.1.3-develop", "", " 5.1.3", None, 5, {}]
)
async def test_unqualified_versions_remain_unsupported(version):
    async with SabClient(
        "http://sab.test",
        "private-sab-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"version": version})
        ),
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.capabilities()
    assert caught.value.kind == FailureKind.UNSUPPORTED


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirect_never_forwards_form_credentials(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://other.test/api"})

    async with SabClient(
        "http://sab.test", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.capabilities()
    assert caught.value.kind == FailureKind.ROUTE
    assert len(calls) == 1
    assert "private-sab-key" not in str(caught.value)
    assert "private-sab-key" not in str(calls[0].url)


@pytest.mark.parametrize("mutating", [False, True])
async def test_transport_failure_keeps_read_and_submit_outcomes_distinct(mutating):
    def handler(request):
        if request.url.params["mode"] == "version":
            return httpx.Response(200, json={"version": "5.1.3"})
        raise httpx.ReadTimeout("connection interrupted", request=request)

    async with SabClient(
        "http://sab.test", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            if mutating:
                await client.submit(
                    nzb_bytes(), attempt_tag="book-search:attempt", save_path="/downloads"
                )
            else:
                await client.download_location("books")
    assert caught.value.kind == (FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE)
    assert "private-sab-key" not in str(caught.value)


async def test_protected_config_rejects_bad_form_key():
    def handler(request):
        assert parse_qs(request.content.decode())["apikey"] == ["bad"]
        if request.url.params["mode"] == "version":
            return httpx.Response(200, json={"version": "5.1.3"})
        return httpx.Response(403, text="API Key Incorrect")

    async with SabClient(
        "http://sab.test", "bad", transport=httpx.MockTransport(handler)
    ) as client:
        assert (await client.capabilities()).version == "5.1.3"
        with pytest.raises(AdapterError) as caught:
            await client.download_location("books")
    assert caught.value.kind == FailureKind.AUTHENTICATION


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_multipart_redirect_stays_uncertain_without_forwarding(status):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.params["mode"] == "version":
            return httpx.Response(200, json={"version": "5.1.3"})
        assert b'name="apikey"\r\n\r\nprivate-sab-key\r\n' in request.content
        return httpx.Response(status, headers={"Location": "http://other.test/api"})

    async with SabClient(
        "http://sab.test", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.submit(
                nzb_bytes(), attempt_tag="book-search:attempt", save_path="/downloads"
            )
    assert caught.value.kind == FailureKind.UNCERTAIN
    assert len(calls) == 2
    assert all(call.url.host == "sab.test" for call in calls)
    assert all("private-sab-key" not in str(call.url) for call in calls)
    assert "private-sab-key" not in str(caught.value)


async def test_find_does_not_associate_a_sanitized_name_prefix_collision():
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "version":
            return httpx.Response(200, json={"version": "5.1.3"})
        if mode == "queue":
            return httpx.Response(200, json={"queue": {"slots": []}})
        return httpx.Response(
            200, json={"history": {"slots": [completed("book-search_attempt-other")]}}
        )

    async with SabClient(
        "http://sab.test", "private-sab-key", transport=httpx.MockTransport(handler)
    ) as client:
        assert await client.find(attempt_tag="book-search:attempt", torrent_hash=None) == []


@pytest.mark.parametrize(
    "name",
    [
        "other_attempt",
        "book-search_",
        "book-search_a:b",
        "book-search_a b",
        "book-search_" + "a" * 81,
    ],
)
def test_name_decoding_does_not_expand_the_attempt_namespace(name):
    from app.adapters.sabnzbd import job_names

    assert job_names({"nzb_name": name}) == {name}


def test_verify_requires_one_named_job_inside_the_download_root():
    from app.adapters.sabnzbd import SabState, verify_association

    state = SabState(
        external_id="SABnzbd_nzo_finished",
        state="Completed",
        completed=True,
        save_path="/downloads/books/Finished Book",
        category="books",
        names={"book-search:attempt"},
        reported_complete=True,
    )
    verified = verify_association(
        [state], tag="book-search:attempt", save_path="/downloads/books", category="books"
    )
    assert verified.association_verified
    with pytest.raises(AdapterError) as caught:
        verify_association(
            [state], tag="book-search:attempt", save_path="/elsewhere", category="books"
        )
    assert caught.value.kind == FailureKind.UNCERTAIN
    unrelated = state.model_copy(update={"names": {"other"}})
    with pytest.raises(AdapterError) as caught:
        verify_association(
            [unrelated], tag="book-search:attempt", save_path="/downloads/books", category="books"
        )
    assert caught.value.kind == FailureKind.UNCERTAIN
