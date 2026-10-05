import pytest

from app.adapters.audible import Audible, product
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.custom_metadata import CustomMetadata


def test_recording_date_does_not_replace_work_publication_and_html_is_plain_text():
    book = product(
        {
            "asin": "B012345678",
            "title": "Harbor",
            "authors": [{"name": "Writer"}],
            "release_date": "2026-01-01",
            "runtime_length_min": "650",
            "publisher_summary": "<p>A <b>story</b>.</p>",
            "language": "english",
            "product_images": {"500": "https://evil.example/cover.jpg"},
            "format_type": "unabridged",
        }
    )
    assert book.publication_year is None and book.editions[0].publication_year == 2026
    assert book.editions[0].runtime_minutes == 650 and book.language == "en"
    assert "<" not in book.description and book.cover_url is None
    assert book.editions[0].abridged is False


async def test_redirected_asin_and_stub_are_not_valid_recordings():
    async def request(*args, **kwargs):
        return {"product": {"asin": "B000000001", "title": "Wrong recording"}}

    with pytest.raises(AdapterError) as caught:
        await Audible(request).fetch("B012345678")
    assert caught.value.kind == FailureKind.PARSER
    with pytest.raises(AdapterError) as caught:
        product({"asin": "B012345678"})
    assert caught.value.kind == FailureKind.NOT_FOUND
    with pytest.raises(AdapterError):
        await Audible(request).fetch("../authors")


async def test_malformed_custom_source_is_a_provider_error_not_a_server_crash():
    async def request(*args, **kwargs):
        return {"matches": [{"title": "", "language": {"invalid": True}}]}

    with pytest.raises(AdapterError) as caught:
        await CustomMetadata(request).search("book")
    assert caught.value.kind == FailureKind.PARSER


async def test_storefront_selections_keep_order_and_reject_wrong_release_dates(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.domain.curation_sources import audible_collections, fetch_audible, storefront_asins

    html = """<div data-asin="B000000009">Unrelated carousel</div>
    <li class="productListItem" id="product-list-item-B000000002"></li>
    <li class="productListItem" id="product-list-item-B000000001"></li>"""
    assert storefront_asins(html) == ["B000000002", "B000000001"]
    with pytest.raises(AdapterError):
        storefront_asins('<form action="captcha"></form>')
    today = datetime.now(UTC).date()
    rows = [
        {
            "asin": key,
            "title": key,
            "authors": [{"name": "Writer"}],
            "release_date": str(today + timedelta(days=delta)),
        }
        for key, delta in [("B000000001", -1), ("B000000002", 10)]
    ]

    class Gateway:
        stale = False

        def __init__(self, provider, scope):
            self.provider = provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, path, params=None):
            if self.provider == "audible-storefront":
                return {"html": html}
            assert params["asins"] == "B000000002,B000000001"
            return {"products": rows}

    monkeypatch.setattr("app.domain.curation_sources.CatalogGateway", Gateway)
    shelves = audible_collections()
    popular = await fetch_audible(shelves["audible-us-popular"])
    assert [b["external_id"] for b in popular["books"]] == ["B000000002", "B000000001"]
    upcoming = await fetch_audible(shelves["audible-us-upcoming"])
    assert [b["external_id"] for b in upcoming["books"]] == ["B000000002"]
    recent = await fetch_audible(shelves["audible-us-releases"])
    assert [b["external_id"] for b in recent["books"]] == ["B000000001"]
