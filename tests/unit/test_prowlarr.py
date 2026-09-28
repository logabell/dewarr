import logging

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.prowlarr import ProwlarrClient, ProwlarrSearch
from tests.prowlarr_fixture import indexer, release
from tests.torrent_fixture import torrent_bytes


@pytest.mark.parametrize(
    ("title", "formats"),
    [
        ("Cory.Doctorow-Enshittification.2025.RETAIL.EPUB", ["epub"]),
        ("[M4B] Writer - Book", ["m4b"]),
        ("Writer - Book [EPUB] [PDF]", ["epub", "pdf"]),
        ("Mel Robbins - The Let Them Theory (retail) (epub)", ["epub"]),
        ("Writer - Book ( M4B ) [ MP3 ]", ["m4b", "mp3"]),
        ("Writer - Book (EPUB handbook)", []),
        ("Writer - Book (epub]", []),
        ("Writer - Book.epub.part01.rar", []),
        ("Writer - Book.epub.par2", []),
        ("Writer - The EPUB Handbook (2025)", []),
    ],
)
async def test_only_explicit_media_labels_supply_format_metadata(title, formats):
    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[release(title=title)])),
    ) as client:
        item = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0].release
    assert item.formats == formats
    assert item.authors == [] and item.narrators == [] and item.language is None


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.test/7/download?link=secret",
        "https://prowlarr.test/base/api/v1/command?link=secret",
        "https://prowlarr.test/base/8/download?link=secret",
        "https://prowlarr.test/base/7/download?link=a&link=b",
        "https://prowlarr.test/base/7/download?link=a&target=http://localhost",
        "https://prowlarr.test/base/7/download?link=../bad",
        "https://prowlarr.test/base/7/download?link=a#fragment",
        "magnet:?xt=urn:btih:123",
        None,
    ],
)
async def test_untrusted_links_are_not_acquirable(bad_url):
    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=[release(downloadUrl=bad_url)])
        ),
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        assert hit.reference is None and not hit.release.acquisition_supported


async def test_fields_query_proxy_and_log_privacy(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    calls = []

    async def handler(req):
        calls.append(req)
        assert req.headers["x-api-key"] == "secret-api"
        if req.url.path.endswith("/search"):
            assert req.url.params.get_list("categories") == ["7020", "3030"]
            assert req.url.params["indexerIds"] == "7"
            return httpx.Response(200, json=[release(), release()])
        if req.url.path.endswith("/indexer"):
            return httpx.Response(
                200, json=[indexer(), indexer(id=8, definitionName="MyAnonamouse")]
            )
        assert req.url.path == "/base/7/download"
        assert "apikey" not in req.url.params
        assert req.url.params["link"] == "secret_proxy_link"
        return httpx.Response(200, content=torrent_bytes())

    async with ProwlarrClient(
        "https://prowlarr.test/base", "secret-api", transport=httpx.MockTransport(handler)
    ) as client:
        indexes = await client.indexers()
        assert indexes[1].native_mam and indexes[0].categories == [3000, 3030, 7020]
        batch = await client.search(ProwlarrSearch(q="Book", indexer_id=7))
        assert batch.returned_count == 2
        hits = batch.hits
        assert len(hits) == 1
        hit = hits[0]
        assert hit.release.seeders == 0 and hit.release.language is None
        assert hit.release.narrators == [] and hit.release.formats == ["m4b"]
        assert hit.release.details["format_basis"] == "release_title"
        assert hit.release.medium == "audio"
        artifact = await client.resolve((hit.release, hit.reference))
        assert artifact.content == torrent_bytes()
        assert "secret" not in hit.release.model_dump_json() + str(indexes) + caplog.text
        assert "hidden" not in hit.release.model_dump_json()
    assert all(req.method == "GET" for req in calls)


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, FailureKind.AUTHENTICATION),
        (403, FailureKind.PERMISSION),
        (429, FailureKind.RATE_LIMIT),
        (500, FailureKind.UNAVAILABLE),
        (302, FailureKind.ROUTE),
    ],
)
async def test_safe_failures(status, kind):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            status,
            text="private body",
            headers={"Location": "http://private.test", "Retry-After": "60"},
        )

    async with ProwlarrClient(
        "https://prowlarr.test", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await client.test()
        assert caught.value.kind == kind and "private" not in str(caught.value)
        assert client.cooldown == 60
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body", [{}, [None], [release(indexerId=8)], [release(guid=None)], [release(title="")]]
)
async def test_invalid_search_response(body):
    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    ) as client:
        with pytest.raises(AdapterError, match="Prowlarr returned"):
            await client.search(ProwlarrSearch(q="Book", indexer_id=7))


async def test_unknowns_and_unsupported_protocols():
    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json=[release(protocol="usenet", categories=[{"id": 7020}], seeders=-1, size=0)],
            )
        ),
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        assert hit.release.protocol == "nzb" and hit.release.medium == "ebook"
        assert hit.release.seeders is None and hit.release.size_bytes is None
        assert hit.release.acquisition_supported and hit.reference == "secret_proxy_link"


async def test_proxy_redirect_does_not_follow_or_log_private_reference(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=[release()])
        return httpx.Response(301, headers={"Location": "magnet:?xt=urn:btih:private"})

    async with ProwlarrClient(
        "https://prowlarr.test/base", "secret-api", transport=httpx.MockTransport(handler)
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        with pytest.raises(AdapterError) as caught:
            await client.resolve((hit.release, hit.reference))
        assert caught.value.kind == FailureKind.UNSUPPORTED
    assert len(calls) == 2
    assert "secret_proxy_link" not in caplog.text and "private" not in str(caught.value)


async def test_oversized_page_is_bounded():
    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b" " * (8 * 1024 * 1024 + 1))
        ),
    ) as client:
        with pytest.raises(AdapterError, match="size limit"):
            await client.indexers()


async def public_resolver(host, port):
    return ["93.184.216.34"]


@pytest.mark.parametrize("protocol", ["usenet", "torrent"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_download_redirects_pin_dns_strip_credentials_and_keep_secrets_private(
    protocol, status, caplog
):
    from tests.nzb_fixture import nzb_bytes

    caplog.set_level(logging.DEBUG)
    calls = []
    lookups = []
    content = nzb_bytes() if protocol == "usenet" else torrent_bytes()

    async def resolver(host, port):
        lookups.append((host, port))
        return ["93.184.216.34"]

    def proxy(req):
        assert req.headers["x-api-key"] == "private-api"
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=[release(protocol=protocol)])
        return httpx.Response(
            status,
            headers={
                "location": "https://indexer.test:8443/api?apikey=private-indexer",
                "set-cookie": "session=private-cookie",
            },
        )

    def indexer(req):
        calls.append(req)
        assert req.url.host == "93.184.216.34"
        assert req.url.port == 8443
        assert req.headers["host"] == "indexer.test:8443"
        assert req.extensions["sni_hostname"] == "indexer.test"
        assert not {"x-api-key", "authorization", "cookie", "referer"} & set(req.headers)
        if len(calls) == 1:
            assert req.url.params["apikey"] == "private-indexer"
            return httpx.Response(
                302,
                headers={
                    "location": "/file/private-passkey.nzb",
                    "set-cookie": "session=private-cookie",
                },
            )
        assert req.url.path == "/file/private-passkey.nzb" and not req.url.query
        return httpx.Response(200, content=content)

    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "private-api",
        transport=httpx.MockTransport(proxy),
        redirect_transport=httpx.MockTransport(indexer),
        resolver=resolver,
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        artifact = await client.resolve((hit.release, hit.reference))
        assert artifact.content == content
    assert lookups == [("indexer.test", 8443)] * 2
    assert len(calls) == 2
    assert "private-" not in caplog.text
    # The context must reset so unrelated requests still log normally.
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        await c.get("https://ordinary.test")
    assert "ordinary.test" in caplog.text


@pytest.mark.parametrize(
    "location,addresses",
    [
        ("http://indexer.test/file", ["93.184.216.34"]),
        ("https://user:password@indexer.test/file", ["93.184.216.34"]),
        ("file:///etc/passwd", ["93.184.216.34"]),
        ("magnet:?xt=urn:btih:private", ["93.184.216.34"]),
        ("https://indexer.test/file#fragment", ["93.184.216.34"]),
        ("https://indexer.test:bad/file", ["93.184.216.34"]),
        ("", ["93.184.216.34"]),
        ("https://127.0.0.1/file", ["127.0.0.1"]),
        ("https://indexer.test/file", ["10.0.0.1"]),
        ("https://indexer.test/file", ["169.254.169.254"]),
        ("https://indexer.test/file", ["::1"]),
        ("https://indexer.test/file", ["::ffff:127.0.0.1"]),
        ("https://indexer.test/file", ["93.184.216.34", "192.168.1.2"]),
        ("https://indexer.test/file", []),
    ],
)
async def test_unsafe_redirects_are_rejected_before_connection(location, addresses):
    calls = []

    async def resolver(host, port):
        return addresses

    def proxy(req):
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=[release(protocol="usenet")])
        return httpx.Response(301, headers={"location": location})

    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(proxy),
        resolver=resolver,
        redirect_transport=httpx.MockTransport(lambda req: calls.append(req)),
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        with pytest.raises(AdapterError) as caught:
            await client.resolve((hit.release, hit.reference))
    assert caught.value.kind == (
        FailureKind.ROUTE if ":bad/" in location else FailureKind.UNSUPPORTED
    )
    assert not calls and "password" not in str(caught.value)


@pytest.mark.parametrize(
    "case,kind",
    [
        ("loop", FailureKind.UNSUPPORTED),
        ("private_second_hop", FailureKind.UNSUPPORTED),
        ("large", FailureKind.PARSER),
        ("compressed", FailureKind.PARSER),
        ("rate", FailureKind.RATE_LIMIT),
        ("timeout", FailureKind.TIMEOUT),
        ("dns", FailureKind.ROUTE),
    ],
)
async def test_redirect_failures_are_bounded_and_safe(case, kind):
    calls = []

    def proxy(req):
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=[release(protocol="usenet")])
        return httpx.Response(301, headers={"location": "https://indexer.test/private-passkey"})

    async def resolver(host, port):
        if case == "dns":
            raise OSError("private-passkey")
        return ["127.0.0.1"] if host == "private.test" else ["93.184.216.34"]

    def indexer(req):
        calls.append(req)
        if case == "timeout":
            raise httpx.ReadTimeout("private-passkey")
        return {
            "loop": lambda: httpx.Response(302, headers={"location": "/private-passkey"}),
            "private_second_hop": lambda: httpx.Response(
                302, headers={"location": "https://private.test/file"}
            ),
            "large": lambda: httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)),
            "compressed": lambda: httpx.Response(200, headers={"content-encoding": "br"}),
            "rate": lambda: httpx.Response(429, headers={"retry-after": "60"}),
        }[case]()

    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(proxy),
        redirect_transport=httpx.MockTransport(indexer),
        resolver=resolver,
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        with pytest.raises(AdapterError) as caught:
            await client.resolve((hit.release, hit.reference))
        assert caught.value.kind == kind and "private-passkey" not in str(caught.value)
        if case == "rate":
            assert client.cooldown == caught.value.retry_after == 60
    assert len(calls) == (3 if case == "loop" else 0 if case == "dns" else 1)


async def test_redirect_tries_only_prevalidated_addresses_on_connect_failure():
    calls = []
    lookups = []

    async def resolver(host, port):
        lookups.append((host, port))
        return ["2606:4700:4700::1111", "1.1.1.1"]

    def proxy(req):
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=[release()])
        return httpx.Response(301, headers={"location": "https://indexer.test/file"})

    def indexer(req):
        calls.append(req.url.host)
        if len(calls) == 1:
            raise httpx.ConnectError("unreachable IPv6")
        return httpx.Response(200, content=torrent_bytes())

    async with ProwlarrClient(
        "https://prowlarr.test/base",
        "secret",
        transport=httpx.MockTransport(proxy),
        redirect_transport=httpx.MockTransport(indexer),
        resolver=resolver,
    ) as client:
        hit = (await client.search(ProwlarrSearch(q="Book", indexer_id=7))).hits[0]
        assert (await client.resolve((hit.release, hit.reference))).content == torrent_bytes()
    assert lookups == [("indexer.test", None)]
    assert calls == ["2606:4700:4700::1111", "1.1.1.1"]
