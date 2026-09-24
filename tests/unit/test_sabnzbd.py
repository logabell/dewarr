import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.sabnzbd import SabClient
from tests.nzb_fixture import nzb_bytes


def completed(name="book-search:attempt"):
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


@pytest.fixture
def transport():
    calls = []

    def handler(request):
        calls.append(request)
        assert "private-sab-key" not in str(request.url)
        assert request.headers["x-api-key"] == "private-sab-key"
        mode = request.url.params["mode"]
        if mode == "version":
            return httpx.Response(200, json={"version": "4.5.1"})
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
            assert request.url.params["nzbname"] == "book-search:attempt"
            assert request.url.params["cat"] == "books"
            return httpx.Response(200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_added"]})
        if mode == "queue":
            return httpx.Response(200, json={"queue": {"slots": []}})
        if mode == "history":
            return httpx.Response(200, json={"history": {"slots": [completed()]}})
        raise AssertionError(mode)

    return calls, httpx.MockTransport(handler)


async def test_connection_reads_category_folder_without_logging_the_key(transport):
    calls, mock = transport
    async with SabClient("http://sab.test:8080", "private-sab-key", transport=mock) as client:
        capabilities = await client.capabilities()
        assert capabilities.version == "4.5.1"
        assert capabilities.protocols == {"nzb"}
        assert await client.download_location("books") == "/downloads/complete/books"
    assert calls
    assert all(call.url.params["output"] == "json" for call in calls)


async def test_sabnzbd_5_is_supported():
    def handler(request):
        assert request.url.params["mode"] == "version"
        return httpx.Response(200, json={"version": "5.1.3"})

    async with SabClient(
        "http://sab.test:8080",
        "private-sab-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.capabilities()

    assert capabilities.version == "5.1.3"


async def test_queue_filename_suffix_still_matches_the_attempt():
    def handler(request):
        mode = request.url.params["mode"]
        if mode == "version":
            return httpx.Response(200, json={"version": "4.3.2"})
        if mode == "queue":
            return httpx.Response(
                200,
                json={
                    "queue": {
                        "slots": [
                            {
                                "nzo_id": "SABnzbd_nzo_queued",
                                "filename": "book-search:attempt.nzb",
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
    assert found[0].names == {"book-search:attempt.nzb", "book-search:attempt"}
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
    _, mock = transport
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
    assert found[0].names == {"book-search:attempt"}


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
        if request.url.params.get("mode") == "version" and request.headers["x-api-key"] == "bad":
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
