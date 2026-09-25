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
