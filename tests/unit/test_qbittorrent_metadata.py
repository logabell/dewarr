from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.qbittorrent import QbitClient
from app.adapters.torrent_descriptor import inspect_torrent
from tests.torrent_fixture import torrent_bytes


@pytest.mark.parametrize("existing", [False, True])
async def test_resolve_real_torrent_metadata_without_adding_or_mutating_transfer(existing):
    raw = torrent_bytes()
    descriptor = await inspect_torrent(raw)
    digest = descriptor.infohash_v1
    magnet = f"magnet:?xt=urn:btih:{digest}"
    calls = []

    async def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "login":
            return httpx.Response(200, text="Ok.")
        if path == "version":
            return httpx.Response(200, text="v5.2.3")
        if path == "webapiVersion":
            return httpx.Response(200, text="2.15.1")
        if path == "fetchMetadata":
            assert parse_qs(request.content.decode()) == {"source": [magnet]}
            if calls.count(path) == 1:
                return httpx.Response(202, json={"hash": digest})
            return httpx.Response(200, json={"hash": digest, "info": {"files": []}})
        if path == "saveMetadata":
            return httpx.Response(409 if existing else 200, content=b"" if existing else raw)
        if path == "export" and existing:
            assert request.url.params["hash"] == digest
            return httpx.Response(200, content=raw)
        raise AssertionError("Unexpected client action: " + path)

    async with QbitClient(
        "http://qbit.test", "fixture", "fixture", transport=httpx.MockTransport(handler)
    ) as client:
        assert await client.resolve_magnet(magnet, interval=0) == raw
    assert "add" not in calls and "start" not in calls and "stop" not in calls


async def test_older_client_rejects_metadata_work_without_submitting_magnet():
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        return httpx.Response(
            200, text={"login": "Ok.", "version": "v5.1.4", "webapiVersion": "2.11.4"}[path]
        )

    async with QbitClient(
        "http://qbit.test", "fixture", "fixture", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.resolve_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert error.value.kind == FailureKind.UNSUPPORTED
    assert calls == ["login", "version", "webapiVersion"]


@pytest.mark.parametrize("failure", ["pending", "wrong-torrent", "unreadable", "missing-info"])
async def test_metadata_resolution_holds_on_timeout_or_inconsistent_evidence(failure):
    calls = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path in {"login", "version", "webapiVersion"}:
            return httpx.Response(
                200, text={"login": "Ok.", "version": "v5.2.3", "webapiVersion": "2.15.1"}[path]
            )
        if path == "fetchMetadata":
            if failure == "pending":
                return httpx.Response(202, json={})
            if failure == "unreadable":
                return httpx.Response(200, text="not JSON")
            return httpx.Response(200, json={} if failure == "missing-info" else {"info": {}})
        if path == "saveMetadata":
            return httpx.Response(200, content=torrent_bytes())
        raise AssertionError(path)

    async with QbitClient(
        "http://qbit.test", "fixture", "fixture", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as error:
            # Only the pending case should run out of time. The others start a torrent probe
            # process, which can take longer than 0.2 seconds on a busy machine.
            await client.resolve_magnet(
                "magnet:?xt=urn:btih:" + "a" * 40,
                interval=0.01,
                wait_seconds=0.2 if failure == "pending" else 10,
            )
        assert error.value.kind == (
            FailureKind.TIMEOUT if failure == "pending" else FailureKind.PARSER
        )
    assert "add" not in calls


async def test_metadata_rejects_web_fetch_parameters_before_contacting_client():
    def unexpected(request):
        raise AssertionError("Must reject source-supplied web fetch before network I/O")

    async with QbitClient(
        "http://qbit.test", "fixture", "fixture", transport=httpx.MockTransport(unexpected)
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.resolve_magnet(
                "magnet:?xt=urn:btih:" + "a" * 40 + "&xs=http://localhost/private"
            )
        assert error.value.kind == FailureKind.UNSUPPORTED
