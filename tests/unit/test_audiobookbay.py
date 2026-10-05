import base64
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.adapters.audiobookbay import (
    ABBClient,
    ABBSearch,
    detail_path,
    parse_detail,
    parse_search,
    size,
)
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.qbittorrent import magnet_hashes
from tests.abb_fixture import HASH, ORIGIN, PATH, detail, post, search


def test_search_preserves_explicit_metadata_without_inventing_seed_counts():
    page = parse_search(search(more=True), ORIGIN, 1)
    assert page.has_more and len(page.items) == 1
    release = page.items[0]
    assert release.title == "Harbor" and release.raw_title == "Harbor - Alex Morgan"
    assert release.authors == ["Alex Morgan"] and release.narrators == ["Casey Reader"]
    assert release.language == "en" and release.formats == ["m4b"]
    assert release.size_bytes == 1610612736 and release.seeders is None
    assert release.abridged is False
    assert release.detail_path == PATH and release.files == []


def test_unlabelled_author_and_narrator_remain_unknown():
    release = parse_search(post(body="Format: MP3"), ORIGIN, 1).items[0]
    assert release.authors == [] and release.narrators == []
    assert release.title == release.raw_title and release.abridged is None


def encoded_post(markup):
    payload = base64.b64encode(markup.encode()).decode()
    return f'<div class="post re-ab" style="display:none;">{payload}</div>'


def test_search_decodes_marked_postings_without_losing_other_results_or_pagination():
    # The site wraps the inner post markup in base64, expanding it in main.js.
    markup = """<div class="postTitle"><h2>
      <a href="/abss/harbor-lights/">Harbor Lights - René Morgan</a>
      </h2></div><div class="postInfo">Language: French</div>
      <div class="postContent">Format: MP3</div>"""
    html = search(more=True).replace(post(), post() + encoded_post(markup))
    page = parse_search(html, ORIGIN, 1)
    assert page.has_more
    assert [item.raw_title for item in page.items] == [
        "Harbor - Alex Morgan",
        "Harbor Lights - René Morgan",
    ]
    assert page.items[1].detail_path == "/abss/harbor-lights/"
    assert page.items[1].language == "fr" and page.items[1].formats == ["mp3"]


@pytest.mark.parametrize("payload", ["not base64!", "/w=="])
def test_malformed_encoded_postings_fail_explicitly(payload):
    with pytest.raises(AdapterError, match="encoded posting could not be read"):
        parse_search(f'<div class="post re-ab">{payload}</div>', ORIGIN, 1)


@pytest.mark.parametrize("path", ["https://other.test/abss/book/", "/abss/%2e%2e/"])
def test_encoded_postings_retain_posting_link_validation(path):
    with pytest.raises(AdapterError, match="invalid posting link"):
        parse_search(encoded_post(post(path=path)), ORIGIN, 1)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("This abridged edition was replaced by an unabridged recording.", None),
        ("<p>Unabridged</p><p>Abridged</p>", None),
        ("<p>Abridgment: Abridged</p><p>Also available unabridged elsewhere.</p>", True),
        ("<p>Unabridged</p><p>Earlier abridged editions omitted chapters.</p>", False),
    ],
)
def test_abridgment_requires_unambiguous_explicit_posting_label(body, expected):
    release = parse_search(post(body=body), ORIGIN, 1).items[0]
    assert release.abridged is expected


def test_detail_claims_remain_claims_and_magnet_is_not_exposed_in_repr():
    result = parse_detail(detail(), ORIGIN, PATH)
    assert magnet_hashes(result.magnet) == {HASH}
    assert [(f.path, f.size_bytes, f.evidence) for f in result.release.files] == [
        ("Harbor/Harbor.m4b", 12, "claimed"),
        ("Harbor/cover.jpg", 2, "claimed"),
    ]
    assert result.release.size_bytes == 14
    assert "magnet" not in repr(result)
    assert parse_qs(urlsplit(result.magnet).query)["tr"] == [
        "udp://tracker.example.com:80/announce"
    ]


@pytest.mark.parametrize(
    "value,expected",
    [(HASH.upper(), HASH), (" \n".join([HASH[:20], HASH[20:]]), HASH), ("a" * 64, "a" * 64)],
)
def test_v1_whitespace_and_v2_hashes_have_correct_magnet_namespaces(value, expected):
    result = parse_detail(detail(digest=value), ORIGIN, PATH)
    assert magnet_hashes(result.magnet) == {expected}
    xt = parse_qs(urlsplit(result.magnet).query)["xt"][0]
    assert xt.startswith("urn:btmh:1220" if len(expected) == 64 else "urn:btih:")


def test_canonical_magnet_fallback_strips_webseeds_and_private_trackers():
    digest = base64.b32encode(bytes.fromhex(HASH)).decode()
    html = detail(
        digest="not-a-hash",
        extra=f'<a href="magnet:?xt=urn:btih:{digest}&amp;xs=http://127.0.0.1/private&amp;tr=http://127.0.0.1/tracker">Download</a>',
    )
    magnet = parse_detail(html, ORIGIN, PATH).magnet
    assert magnet_hashes(magnet) == {HASH}
    assert "127.0.0.1" not in magnet and "xs=" not in magnet


def test_conflicting_identities_fail_instead_of_selecting_a_different_release():
    with pytest.raises(AdapterError, match="conflicting torrent identities"):
        parse_detail(
            detail(extra=f'<a href="magnet:?xt=urn:btih:{"b" * 40}">Other</a>'), ORIGIN, PATH
        )
    result = parse_detail(detail(digest=""), ORIGIN, PATH)
    assert result.magnet is None and not result.release.acquisition_supported


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/abss/book/",
        "//other.test/abss/book/",
        "/abss/../admin/",
        "/abss/%252e%252e/",
        "/abss/%2e%2e/",
        "/abss/book/?token=secret",
        "/abss/book/#comment",
        "/abss/book%5cadmin/",
        "/login/",
        "https://u:p@abb.test/abss/book/",
    ],
)
def test_detail_links_cannot_change_origin_or_address_arbitrary_resources(url):
    with pytest.raises(ValueError):
        detail_path(url, ORIGIN)


@pytest.mark.parametrize(
    "html",
    [
        "<html>Maintenance</html>",
        '<div class="post"><p>Changed layout</p></div>',
        '<form><input type="password"></form>',
        '<div id="challenge-form"></div>',
    ],
)
def test_layout_and_auth_failures_never_become_empty_searches(html):
    with pytest.raises(AdapterError):
        parse_search(html, ORIGIN, 1)
    empty = parse_search('<div id="content"><h2>Nothing Found</h2></div>', ORIGIN, 1)
    assert not empty.items and not empty.has_more


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.5 GiB", 1610612736),
        ("25 Bytes", 25),
        ("-1 MB", None),
        ("NaN MB", None),
        ("999999999999 TB", None),
        ("1 XB", None),
    ],
)
def test_sizes_are_bounded_and_unknown_is_not_zero(value, expected):
    assert size(value) == expected


async def test_capitalized_homepage_redirect_retries_the_lowercase_search():
    queries = []

    async def handler(request):
        if not request.url.query:
            return httpx.Response(200, text=search(), headers={"content-type": "text/html"})
        queries.append(request.url.params["s"])
        if request.url.params["s"] != request.url.params["s"].casefold():
            return httpx.Response(301, headers={"location": ORIGIN + "/"})
        return httpx.Response(200, text=search(), headers={"content-type": "text/html"})

    async with ABBClient(
        ORIGIN, transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        page = await client.search(ABBSearch(q="Harbor Lights"))
    assert queries == ["Harbor Lights", "harbor lights"]
    assert page.items[0].title == "Harbor"


async def test_redirect_to_another_host_is_not_retried_in_lowercase():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if not request.url.query:
            return httpx.Response(200, text=search(), headers={"content-type": "text/html"})
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async with ABBClient(
        ORIGIN, transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.search(ABBSearch(q="Harbor Lights"))
    assert error.value.kind == FailureKind.ROUTE
    assert calls == ["/", "/"]


@pytest.mark.parametrize("location", ["/", ORIGIN + "/"])
async def test_homepage_retry_is_bounded_and_preserves_pagination(location):
    requests = []

    async def handler(request):
        requests.append(request)
        if not request.url.query:
            return httpx.Response(200, text=search(), headers={"content-type": "text/html"})
        return httpx.Response(302, headers={"location": location})

    async with ABBClient(
        ORIGIN, transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.search(ABBSearch(q="Große Harbor", page=2))
    assert error.value.kind == FailureKind.ROUTE
    assert [r.url.params.get("s") for r in requests] == [None, "Große Harbor", "große harbor"]
    assert [r.url.path for r in requests] == ["/", "/page/2/", "/page/2/"]
    assert all(r.url.params["cat"] == "undefined" for r in requests[1:])


@pytest.mark.parametrize(
    "query,location",
    [
        ("harbor", "/"),
        ("Harbor", "/login"),
        ("Harbor", "/?s=other"),
        ("Harbor", "http://abb.test/"),
        ("Harbor", "https://[invalid/"),
        ("Harbor", "//elsewhere.test/"),
        ("Harbor", ""),
    ],
)
async def test_nonretryable_search_redirects_keep_the_route_error(query, location):
    calls = []

    async def handler(request):
        calls.append(request)
        if not request.url.query:
            return httpx.Response(200, text=search(), headers={"content-type": "text/html"})
        return httpx.Response(302, headers={"location": location})

    async with ABBClient(
        ORIGIN, transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        with pytest.raises(AdapterError) as error:
            await client.search(ABBSearch(q=query))
    assert error.value.kind == FailureKind.ROUTE
    assert len(calls) == 2


async def test_bounded_client_initializes_session_and_uses_search_page_contract():
    requests = []

    async def handler(request):
        requests.append(request)
        if not request.url.query:
            return httpx.Response(
                200,
                text=search(),
                headers={"content-type": "text/html", "set-cookie": "public=fixture; Path=/"},
            )
        assert request.headers["cookie"] == "public=fixture"
        assert dict(request.url.params) == {"s": "Harbor", "cat": "undefined"}
        return httpx.Response(200, text=search(), headers={"content-type": "text/html"})

    async with ABBClient(ORIGIN, transport=httpx.MockTransport(handler)) as client:
        assert (await client.search(ABBSearch(q="Harbor", page=2))).items[0].title == "Harbor"
    assert [r.url.path for r in requests] == ["/", "/page/2/"]


@pytest.mark.parametrize(
    "code,kind",
    [
        (302, FailureKind.ROUTE),
        (403, FailureKind.PERMISSION),
        (429, FailureKind.RATE_LIMIT),
        (503, FailureKind.UNAVAILABLE),
    ],
)
async def test_http_failures_are_classified_without_following_redirects(code, kind):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            code, headers={"location": "http://127.0.0.1/private", "Retry-After": "17"}
        )

    async with ABBClient(ORIGIN, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError) as error:
            await client.search(ABBSearch(q="Harbor"))
        assert error.value.kind == kind and len(calls) == 1


async def test_large_and_non_html_responses_are_rejected(monkeypatch):
    from app.adapters import audiobookbay

    monkeypatch.setattr(audiobookbay, "MAX_HTML", 50)
    for content, headers in [
        ("x" * 51, {"content-type": "text/html"}),
        ("{}", {"content-type": "application/json"}),
    ]:
        async with ABBClient(
            ORIGIN,
            transport=httpx.MockTransport(
                lambda request, body=content, head=headers: httpx.Response(
                    200, text=body, headers=head
                )
            ),
        ) as client:
            with pytest.raises(AdapterError) as error:
                await client.test()
            assert error.value.kind == FailureKind.PARSER
