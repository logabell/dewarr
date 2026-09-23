import pytest

from app.adapters.goodreads_discovery import award_categories, parse_collection, source


def award_html(count=20):
    return (
        '<title>Fiction — Choice Awards</title><a class="winningTitle" href="/book/show/1">One</a>'
        + "".join(
            f'<div class="pollAnswer"><div class="result">{100 - i} votes</div>'
            f'<a class="pollAnswer__bookLink" href="/book/show/{i}">'
            f'<img title="Book {i} by Author {i}" src="https://i.gr-assets.com/{i}.jpg"></a></div>'
            for i in range(1, count + 1)
        )
    )


def test_awards_extract_identity_winner_and_votes():
    data = parse_collection(
        award_html(), "https://www.goodreads.com/choiceawards/best-fiction-books-2025", "Fiction"
    )
    assert len(data["books"]) == 20
    assert data["books"][0]["winner"] is True
    assert data["books"][1]["winner"] is False
    assert data["books"][0]["votes"] == 99
    assert data["books"][0]["authors"] == ["Author 1"]
    assert data["coverage"] == "complete"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/list/show/50",
        "http://www.goodreads.com/list/show/50",
        "https://www.goodreads.com@127.0.0.1/list/show/50",
        "https://www.goodreads.com/list/show/50?url=http://localhost",
        "https://www.goodreads.com/choiceawards/best-books-2025https://www.goodreads.com",
        "https://www.goodreads.com:444/list/show/50",
    ],
)
def test_reject_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        source(url)


def test_same_list_different_slug_is_same_identity():
    assert source("https://goodreads.com/list/show/50.Old_title") == source(
        "https://www.goodreads.com/list/show/50.New_title"
    )


def test_incomplete_or_challenged_awards_never_become_empty_snapshot():
    for content in ["<h1>Enable JavaScript</h1>", award_html(1)]:
        with pytest.raises(ValueError):
            parse_collection(
                content, "https://www.goodreads.com/choiceawards/best-fiction-books-2025"
            )


def test_listopia_preserves_rank_and_boxed_set_and_labels_partial():
    html = """<title>Epic fantasy (1,000 books)</title><h1>Score</h1>
    <tr itemtype="http://schema.org/Book"><td class="number">1</td>
    <td><a class="bookTitle" href="/book/show/10">Trilogy boxed set</a>
    <span class="minirating">4.32 avg rating — 12,345 ratings</span>
    <a class="authorName">A. Writer</a><img class="bookCover" src="http://localhost/secret"></td></tr>
    <a class="next_page">Next</a><p>1,000 books</p>"""
    data = parse_collection(html, "https://www.goodreads.com/list/show/50")
    assert data["title"] == "Epic fantasy"
    assert data["count"] == 1000
    assert data["coverage"] == "partial"
    assert data["books"][0]["title"] == "Trilogy boxed set"
    assert data["books"][0]["cover_url"] is None
    assert data["books"][0]["rating"] == 4.32


def test_categories_ignore_overview_cta_and_preserve_historical_labels():
    html = (
        '<a href="/choiceawards/best-fiction-books-2011">Fiction</a>'
        '<a href="/choiceawards/best-books-2011">2011</a>'
        '<a href="/choiceawards/favorite-book-2011">View results</a>'
    )
    assert list(award_categories(html, 2011).values()) == ["Fiction"]


@pytest.mark.parametrize(
    "destination,allowed",
    [
        ("/list/show/50.New_title", True),
        ("https://127.0.0.1/list/show/50", False),
        ("/list/show/51.Other_list", False),
        ("/user/sign_in", False),
    ],
)
async def test_public_list_redirect_stays_on_same_identity(destination, allowed):
    import httpx

    from app.adapters.contracts import AdapterError
    from app.adapters.goodreads import fetch_document

    seen = []

    async def resolver(host):
        return ["93.184.216.34"]

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"<html>books</html>"

    def handler(request):
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": destination})
        return httpx.Response(200, stream=Body())

    options = dict(collection_id="50", resolver=resolver, transport=httpx.MockTransport(handler))
    if allowed:
        response = await fetch_document("https://www.goodreads.com/list/show/50", **options)
        assert response.content == b"<html>books</html>"
        assert len(seen) == 2
    else:
        with pytest.raises(AdapterError):
            await fetch_document("https://www.goodreads.com/list/show/50", **options)
        assert len(seen) == 1


def test_bundled_catalog_has_consistent_identities_and_coverage():
    from app.domain.discovery_catalog import catalog, coverage

    values = catalog()
    for key, value in values.items():
        assert source(value["source_url"])[1] == key
        assert value["books"]
        assert len({b["external_id"] for b in value["books"]}) == len(value["books"])
        if value["kind"] == "award":
            assert sum(b["winner"] for b in value["books"]) == 1
        if value["coverage"] == "complete":
            assert value["count"] == len(value["books"])
    for year, report in coverage()["years"].items():
        assert report["available"] == sum(v.get("year") == int(year) for v in values.values())


def test_full_size_covers_and_series_evidence_preserve_distinct_books():
    from app.adapters.goodreads_discovery import CollectionBook
    from app.domain.discovery_matching import evidence_for

    entry = CollectionBook(
        external_id="1",
        title="Dune (Dune #1)",
        authors=["Frank Herbert"],
        cover_url="https://i.gr-assets.com/books/1._SY75_.jpg",
    )
    assert entry.cover_url.endswith("._SY600_.jpg")
    assert evidence_for(entry).title == "Dune"
    for title in ["Dune (Dune #1-3)", "Dune (Graphic Novel)", "Dune: Part 1"]:
        assert evidence_for(entry.model_copy(update={"title": title})).title == title


async def test_a_broad_discovery_search_falls_back_to_the_exact_title():
    from app.adapters.catalog_types import BookData, SearchPage
    from app.adapters.goodreads_discovery import CollectionBook
    from app.domain.discovery_matching import resolve_entry

    dune = BookData(provider="hardcover", external_id="1", title="Dune", authors=["Frank Herbert"])
    calls = []

    async def call(operation, *args):
        calls.append(operation)
        if operation == "fetch":
            return dune, False, None
        more = operation == "search"
        return SearchPage(provider="hardcover", items=[dune], page=1, has_more=more), False, None

    entry = CollectionBook(external_id="1", title="Dune", authors=["Frank Herbert"])
    assert (await resolve_entry(entry, call)).status == "matched"
    assert calls == ["search", "title_search", "fetch"]


def list_page_html(page, count=201, length=None):
    start = (page - 1) * 100 + 1
    length = min(100, count - start + 1) if length is None else length
    return (
        f"<title>Readers' list ({count:,} books)</title>"
        f'<div class="pagination"><a href="/list/show/50?page=1">1</a>'
        f'<em class="current">{page}</em>'
        + (
            f'<a class="next_page" href="/list/show/50?page={page + 1}">Next</a>'
            if page * 100 < count
            else ""
        )
        + "</div>"
    ) + "".join(
        f'<tr itemtype="http://schema.org/Book"><td class="number">{n}</td>'
        f'<td><a class="bookTitle" href="/book/show/{n}">Book {n}</a>'
        '<a class="authorName">An Author</a></td></tr>'
        for n in range(start, start + length)
    )


async def test_fetch_listopia_page_preserves_rank_and_detects_last_page(monkeypatch):
    from types import SimpleNamespace

    from app.adapters.goodreads_discovery import fetch_collection_page

    calls = []

    async def document(url, **options):
        calls.append((url, options))
        return SimpleNamespace(content=list_page_html(options["collection_page"]))

    monkeypatch.setattr("app.adapters.goodreads_discovery.fetch_document", document)
    second = await fetch_collection_page("https://www.goodreads.com/list/show/50", 2)
    assert second["books"][0]["rank"] == 101 and len(second["books"]) == 100
    assert second["count"] == 201 and second["has_more"]
    last = await fetch_collection_page("https://www.goodreads.com/list/show/50", 3)
    assert last["books"][0]["rank"] == 201 and not last["has_more"]
    assert calls[0][0] == "https://www.goodreads.com/list/show/50?page=2"
    assert calls[0][1]["collection_id"] == "50"


@pytest.mark.parametrize("html", [list_page_html(1), list_page_html(2, length=50)])
async def test_paginated_lists_reject_repeated_or_truncated_pages(monkeypatch, html):
    from types import SimpleNamespace

    from app.adapters.contracts import AdapterError
    from app.adapters.goodreads_discovery import fetch_collection_page

    async def document(*args, **kwargs):
        return SimpleNamespace(content=html)

    monkeypatch.setattr("app.adapters.goodreads_discovery.fetch_document", document)
    with pytest.raises(AdapterError):
        await fetch_collection_page("https://www.goodreads.com/list/show/50", 2)


@pytest.mark.parametrize(
    "destination,allowed",
    [
        ("/list/show/50.New_title?page=2", True),
        ("/list/show/50.New_title?page=1", False),
        ("/list/show/50.New_title", False),
        ("/list/show/50.New_title?page=2&url=http://localhost", False),
        ("/list/show/51.Other_list?page=2", False),
    ],
)
async def test_paginated_redirect_preserves_page_and_identity(destination, allowed):
    import httpx

    from app.adapters.contracts import AdapterError
    from app.adapters.goodreads import fetch_document

    calls = []

    async def resolver(host):
        return ["93.184.216.34"]

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"<html>books</html>"

    def handler(request):
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": destination})
        return httpx.Response(200, stream=Body())

    options = dict(
        collection_id="50",
        collection_page=2,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    if allowed:
        await fetch_document("https://www.goodreads.com/list/show/50?page=2", **options)
        assert len(calls) == 2
        assert calls[1].params["page"] == "2"
    else:
        with pytest.raises(AdapterError):
            await fetch_document("https://www.goodreads.com/list/show/50?page=2", **options)
        assert len(calls) == 1


@pytest.mark.parametrize("cross_page", [False, True])
async def test_paginated_lists_allow_tied_votes(monkeypatch, cross_page):
    from types import SimpleNamespace

    from app.adapters.goodreads_discovery import fetch_collection_page

    async def document(*args, **kwargs):
        import re

        html = list_page_html(2)
        if cross_page:
            html = re.sub(r'<td class="number">\d+</td>', '<td class="number">100</td>', html)
        return SimpleNamespace(
            content=html.replace('<td class="number">129</td>', '<td class="number">128</td>')
        )

    monkeypatch.setattr("app.adapters.goodreads_discovery.fetch_document", document)
    result = await fetch_collection_page("https://www.goodreads.com/list/show/50", 2)
    assert len(result["books"]) == 100
    assert result["books"][28]["rank"] == (100 if cross_page else 128)


async def test_last_book_page_does_not_follow_comment_pagination(monkeypatch):
    from types import SimpleNamespace

    from app.adapters.goodreads_discovery import fetch_collection_page

    async def document(*args, **kwargs):
        return SimpleNamespace(
            content=list_page_html(3)
            + (
                '<div class="pagination"><em class="current">1</em>'
                '<a class="next_page" href="/list/comments/50?page=2">next</a></div>'
            )
        )

    monkeypatch.setattr("app.adapters.goodreads_discovery.fetch_document", document)
    result = await fetch_collection_page("https://www.goodreads.com/list/show/50", 3)
    assert len(result["books"]) == 1 and not result["has_more"]
