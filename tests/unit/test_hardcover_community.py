import json

import pytest

from app.adapters.contracts import AdapterError
from app.adapters.hardcover_community import (
    COMMUNITY_BOOKS,
    COMMUNITY_LIST,
    COMMUNITY_LISTS,
    COMMUNITY_MATCHES,
    COMMUNITY_SEARCH,
)
from tests.unit.test_hardcover_discovery import adapter


def row(key=91, **extra):
    return {
        "id": key,
        "name": "Sea stories",
        "description": "From the community",
        "public": True,
        "books_count": 2,
        "followers_count": None,
        "updated_at": "2026-09-18T00:00:00Z",
        "list_books": [{"id": 1, "book_id": 42}, {"id": 2, "book_id": 43}],
        **extra,
    }


def books(*keys):
    return {"books": [{"id": key, "title": f"Book {key}"} for key in keys]}


async def test_public_browse_preserves_order_and_hydrates_one_bounded_batch():
    source, calls = adapter(
        {
            COMMUNITY_LISTS: {"lists": [row(k) for k in range(21, 0, -1)]},
            COMMUNITY_BOOKS: books(43, 42),
        }
    )
    result = await source.community_lists("", 2)
    assert result.has_more and len(result.items) == 20
    assert result.items[0].external_id == "21" and result.items[0].followers is None
    assert [b.external_id for b in result.items[0].books] == ["42", "43"]
    assert calls[0]["variables"] == {"offset": 20}
    assert calls[1]["variables"] == {"ids": [42, 43]}
    assert len(calls) == 2 and all("_ilike" not in c["query"] for c in calls)


@pytest.mark.parametrize("encoded", [False, True])
async def test_search_uses_real_search_endpoint_and_revalidates_public_rows(encoded):
    results = {
        "hits": [
            {"document": {"id": str(k), "name": "Untrusted search name"}} for k in [92, 91, 93]
        ],
        "found": 25,
    }
    source, calls = adapter(
        {
            COMMUNITY_SEARCH: {"search": {"results": json.dumps(results) if encoded else results}},
            COMMUNITY_MATCHES: {"lists": [row(91), row(92)]},
            COMMUNITY_BOOKS: books(42, 43),
        }
    )
    result = await source.community_lists("  Sea % tales  ", 1)
    assert result.has_more and result.warning
    assert [v.external_id for v in result.items] == ["92", "91"]
    assert all(v.name == "Sea stories" for v in result.items)
    assert calls[0]["variables"] == {"query": "Sea % tales", "page": 1}
    assert calls[1]["variables"] == {"ids": [92, 91, 93]}


@pytest.mark.parametrize(
    "extra",
    [
        {"public": False},
        {"public": 1},
        {"name": " "},
        {"books_count": True},
        {"books_count": -1},
        {"followers_count": -1},
        {"updated_at": "2026-09-18"},
        {"id": True},
        {"list_books": None},
        {"list_books": [{"id": 1, "book_id": 42}, {"id": 1, "book_id": 43}]},
    ],
)
async def test_malformed_or_private_list_never_becomes_a_public_preview(extra):
    source, _ = adapter({COMMUNITY_LIST: {"lists": [row(**extra)]}})
    with pytest.raises(AdapterError):
        await source.community_list("91", 0)


async def test_detail_uses_keyset_lookahead_not_invented_membership_snapshot():
    entries = [{"id": k, "book_id": k} for k in range(21, 42)]
    source, calls = adapter(
        {
            COMMUNITY_LIST: {"lists": [row(list_books=entries, books_count=60)]},
            COMMUNITY_BOOKS: books(*range(21, 40)),
        }
    )
    result = await source.community_list("91", 20)
    assert result.next_cursor == 40 and len(result.books) == 19 and result.warning
    assert calls[0]["variables"] == {"id": 91, "after": 20}
    assert calls[1]["variables"] == {"ids": list(range(21, 41))}


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "provider detail"},
        {"results": None},
        {"results": {"hits": [], "found": True}},
        {"results": {"hits": [{"document": {"id": "1"}}] * 2, "found": 2}},
    ],
)
async def test_search_errors_never_become_empty_success(payload):
    source, _ = adapter({COMMUNITY_SEARCH: {"search": payload}})
    with pytest.raises(AdapterError):
        await source.community_lists("Sea", 1)


async def test_private_search_hits_and_unrequested_hydration_are_rejected():
    responses = {
        COMMUNITY_SEARCH: {"search": {"results": {"hits": [{"document": {"id": 91}}], "found": 1}}},
        COMMUNITY_MATCHES: {"lists": [row(public=False)]},
    }
    source, _ = adapter(responses)
    with pytest.raises(AdapterError):
        await source.community_lists("Sea", 1)
    responses[COMMUNITY_MATCHES] = {"lists": [row()]}
    responses[COMMUNITY_BOOKS] = books(999)
    with pytest.raises(AdapterError):
        await source.community_lists("Sea", 1)


async def test_empty_search_does_not_hydrate_and_missing_detail_is_not_found():
    source, calls = adapter(
        {
            COMMUNITY_SEARCH: {"search": {"results": {"hits": [], "found": 0}}},
            COMMUNITY_LIST: {"lists": []},
        }
    )
    assert not (await source.community_lists("absent", 1)).items and len(calls) == 1
    with pytest.raises(AdapterError, match="no longer available"):
        await source.community_list("91", 0)


async def test_public_browse_hydrates_embedded_books_without_a_second_request():
    entries = [{"id": 1, "book_id": 42, "book": books(42)["books"][0]}]
    source, calls = adapter({COMMUNITY_LISTS: {"lists": [row(list_books=entries)]}})
    result = await source.community_lists("", 1)
    assert result.items[0].books[0].title == "Book 42"
    assert len(calls) == 1
