import asyncio
import json

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.mam import MAMClient, MAMSearch, cookie_value, parse_page
from tests.mam_fixture import release_row, search_response


def test_search_contract_preserves_query_fields_media_and_pagination():
    body = MAMSearch(
        q='  "Harbor Stories"  ',
        medium="audio",
        fields=["title", "narrator"],
        language_ids=[],
        sort="seeders",
        offset=25,
    ).payload()
    assert body["tor"] == {
        "text": '"Harbor Stories"',
        "srchIn": ["title", "narrator"],
        "searchType": "all",
        "searchIn": "torrents",
        "main_cat": [13],
        "sortType": "seeders",
        "startNumber": 25,
    }
    assert "dlLink" not in body and body["perpage"] == 25
    assert MAMSearch(q="Harbor").payload()["tor"]["main_cat"] == [13, 14]
    assert MAMSearch(q="Harbor", medium="ebook").payload()["tor"]["main_cat"] == [14]


def test_rich_release_metadata_is_not_a_catalog_version_or_verified_pack():
    result = parse_page(search_response(), MAMSearch(q="Harbor"))
    item = result.items[0]
    assert item.raw_title == "Harbor &amp; Roads — Complete Stories"
    assert item.title == "Harbor & Roads — Complete Stories"
    assert item.authors == ["Alex Morgan"] and item.narrators == ["Jordan Lee"]
    assert item.series[0].position == "1-3" and item.category == "Audiobooks - Fiction"
    assert item.formats == ["m4b", "mp3"] and item.size_bytes == 1342177280
    assert item.details["size_is_estimate"]
    assert item.seeders == 42 and item.snatches == 321 and item.leechers == 0
    assert item.freeleech is True and item.vip is False
    assert item.description == "An invented three-book collection.\nNarrated by Jordan Lee."
    assert item.coverage == [] and not result.has_more
    assert "fixture-private-download-token" not in result.model_dump_json()


def test_unknown_stats_and_formats_remain_unknown_partial_rows_are_visible():
    page = parse_page(
        search_response(
            data=[
                release_row(
                    seeders=None, times_completed="not reported", filetype=None, size="unknown"
                ),
                {"bad": "row"},
            ],
            found=70,
        ),
        MAMSearch(q="Harbor"),
    )
    assert page.items[0].seeders is None and page.items[0].snatches is None
    assert page.items[0].formats == [] and page.items[0].size_bytes is None
    assert page.warnings and page.has_more and page.total == 70
    page = parse_page({"data": [], "found": 0, "total": 0}, MAMSearch(q="missing"))
    assert page.items == [] and page.total == 0 and not page.has_more
    page = parse_page({"error": "Nothing returned, out of 0"}, MAMSearch(q="missing"))
    assert page.items == [] and page.total == 0


@pytest.mark.parametrize(
    "value", ["", "mam_id=", "a; other=b", "line\r\nbreak", 'quote"', "é", "😀"]
)
def test_cookie_rejects_header_injection_and_non_ascii(value):
    with pytest.raises(ValueError):
        cookie_value(value)


def test_cookie_keeps_opaque_base64_padding():
    assert cookie_value("mam_id=abc123==") == "abc123=="


@pytest.mark.parametrize(
    "body,kind",
    [
        ({"error": "Error, you are not signed in"}, FailureKind.AUTHENTICATION),
        ({"error": "source failure with private-token"}, FailureKind.PARSER),
        ({"data": {}}, FailureKind.PARSER),
        ({"data": [{"bad": "row"}]}, FailureKind.PARSER),
        ({"error": "Nothing returned, out of 20"}, FailureKind.PARSER),
    ],
)
def test_source_failures_never_become_empty_results(body, kind):
    with pytest.raises(AdapterError) as error:
        parse_page(body, MAMSearch(q="Harbor"))
    assert error.value.kind == kind and "private-token" not in str(error.value)


async def test_http_search_detail_and_session_rotation_only_return_public_fields():
    seen = []

    async def handler(request):
        seen.append(request)
        if request.url.path.endswith("jsonLoad.php"):
            return httpx.Response(200, json={"uid": 99, "username": "private account"})
        body = json.loads(request.content)
        assert body.get("description") == "true" and "dlLink" not in body
        if "id" in body["tor"]:
            assert body["tor"]["id"] == 501
        return httpx.Response(
            200,
            json=search_response(),
            headers={"set-cookie": "mam_id=rotated-fixture; Path=/; HttpOnly"},
        )

    async with MAMClient(
        "https://mam.test", "original-fixture", transport=httpx.MockTransport(handler)
    ) as client:
        await client.test()
        page = await client.search(MAMSearch(q="Harbor"))
        assert client.rotated_cookie == "rotated-fixture"
        assert (await client.detail("501")).source_id == page.items[0].source_id
    assert [request.headers["cookie"] for request in seen] == [
        "mam_id=original-fixture",
        "mam_id=original-fixture",
        "mam_id=rotated-fixture",
    ]


@pytest.mark.parametrize(
    "status,body,headers,kind",
    [
        (401, {}, {}, FailureKind.AUTHENTICATION),
        (407, {}, {}, FailureKind.ROUTE),
        (302, {}, {"location": "https://elsewhere.test"}, FailureKind.ROUTE),
        (429, {}, {"retry-after": "120"}, FailureKind.RATE_LIMIT),
        (503, {}, {}, FailureKind.UNAVAILABLE),
        (
            200,
            "<form>private token</form>",
            {"content-type": "text/html"},
            FailureKind.AUTHENTICATION,
        ),
        (200, "invalid private token", {}, FailureKind.PARSER),
    ],
)
async def test_bounded_transport_failures_are_classified_without_body_disclosure(
    status, body, headers, kind
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, content=body if isinstance(body, str) else json.dumps(body), headers=headers
        )

    async with MAMClient(
        "https://mam.test", "cookie-fixture", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.search(MAMSearch(q="Harbor"))
        assert error.value.kind == kind and "private token" not in str(error.value)
        if status == 429:
            assert client.cooldown == 120 and error.value.retry_after == 120
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,retryable",
    [
        (407, True),
        (502, True),
        (503, True),
        (504, True),
        (302, False),
        (401, False),
        (429, False),
        (500, False),
    ],
)
async def test_only_proxy_route_failures_are_eligible_for_direct_fallback(status, retryable):
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    async with MAMClient(
        "https://mam.test",
        "cookie-fixture",
        transport=transport,
    ) as client:
        # MockTransport replaces HTTPX's proxy transport, so mark this request as
        # proxied explicitly while exercising the response-classification policy.
        client.uses_proxy = True
        with pytest.raises(AdapterError) as error:
            await client.test()
    assert error.value.proxy_retryable is retryable


async def test_required_http_proxy_receives_request_and_never_falls_back_direct():
    direct_requests, proxy_requests = [], []

    async def direct(reader, writer):
        direct_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.close()
        await writer.wait_closed()

    async def proxy(reader, writer):
        proxy_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with (
        await asyncio.start_server(direct, "127.0.0.1", 0) as target,
        await asyncio.start_server(proxy, "127.0.0.1", 0) as route,
    ):
        url = f"http://127.0.0.1:{target.sockets[0].getsockname()[1]}"
        proxy_url = f"http://127.0.0.1:{route.sockets[0].getsockname()[1]}"
        async with MAMClient(
            url,
            "fixture-cookie",
            proxy_url=proxy_url,
            proxy_username="proxy-user",
            proxy_password="proxy-secret",
        ) as client:
            with pytest.raises(AdapterError) as error:
                await client.test()
            assert error.value.kind == FailureKind.ROUTE
    assert len(proxy_requests) == 1 and not direct_requests
    assert b"Proxy-Authorization: Basic" in proxy_requests[0]
    assert url.encode() + b"/jsonLoad.php" in proxy_requests[0]


async def test_environment_proxies_do_not_override_explicit_route(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=search_response()))
    async with MAMClient("https://mam.test", "fixture", transport=transport) as client:
        assert (await client.search(MAMSearch(q="Harbor"))).items[0].source_id == "501"


async def test_response_budget_and_wrong_detail_id_fail_without_exposing_body(monkeypatch):
    from app.adapters import mam

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=search_response()))
    async with MAMClient("https://mam.test", "fixture", transport=transport) as client:
        with pytest.raises(AdapterError, match="different release"):
            await client.detail("999")
        monkeypatch.setattr(mam, "MAX_RESPONSE_BYTES", 64)
        with pytest.raises(AdapterError, match="page limit"):
            await client.search(MAMSearch(q="Harbor"))


def test_inconsistent_result_totals_do_not_prove_an_empty_search():
    with pytest.raises(AdapterError, match="nonempty"):
        parse_page({"data": [], "found": 20}, MAMSearch(q="Harbor"))
    page = parse_page(search_response(found=0), MAMSearch(q="Harbor"))
    assert page.items and page.total is None and page.warnings


async def test_unrelated_cookie_domain_cannot_replace_the_mam_session():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=search_response(),
            headers={"set-cookie": "mam_id=foreign; Domain=foreign.test; Path=/"},
        )
    )
    async with MAMClient("https://mam.test", "fixture", transport=transport) as client:
        await client.search(MAMSearch(q="Harbor"))
        assert client.rotated_cookie is None


async def test_refused_proxy_reports_actionable_error_without_exposing_credentials():
    # Reserve a local port, then close it so the real HTTP transport is refused.
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    async with MAMClient(
        "https://mam.test",
        "private-session",
        proxy_url=f"http://127.0.0.1:{port}",
        proxy_username="private-user",
        proxy_password="private-password",
    ) as client:
        with pytest.raises(AdapterError, match="proxy refused the connection") as error:
            await client.test()
    assert error.value.kind == FailureKind.ROUTE
    assert error.value.proxy_retryable
    assert "No direct fallback" in str(error.value)
    assert "private-" not in str(error.value)


async def test_https_proxy_tunnel_rejection_has_safe_actionable_error():
    async def proxy(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 407 private-proxy-error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with MAMClient(
            "https://mam.test", "private-session", proxy_url=f"http://127.0.0.1:{port}"
        ) as client:
            with pytest.raises(AdapterError, match="proxy rejected the HTTPS tunnel") as error:
                await client.test()
    assert error.value.kind == FailureKind.ROUTE
    assert error.value.proxy_retryable
    assert "private-" not in str(error.value)


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, True),
        (0, False),
        ("1", True),
        ("0", False),
        (True, True),
        (False, False),
        (None, None),
        ("unknown", None),
        (2, None),
    ],
)
def test_mam_badges_accept_explicit_boolean_and_numeric_flags(value, expected):
    page = parse_page(
        search_response(data=[release_row(free=value, vip=value)]), MAMSearch(q="Harbor")
    )
    assert page.items[0].freeleech is expected
    assert page.items[0].vip is expected
