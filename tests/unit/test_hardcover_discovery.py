from datetime import date

import pytest

from app.adapters.catalog_providers import Hardcover
from app.adapters.contracts import AdapterError
from app.adapters.hardcover_discovery import (
    HC_DISCOVERY_BOOKS,
    HC_RECENT,
    HC_RELATED,
    HC_TRENDING,
    HC_UPCOMING,
)


def row(key, **extra):
    return {"id": key, "title": f"Book {key}", **extra}


def adapter(responses):
    calls = []

    async def request(method, path, *, json):
        calls.append(json)
        return {"data": responses[json["query"]]}

    return Hardcover(request), calls


async def test_trending_keeps_rank_and_batches_hydration_without_editions():
    source, calls = adapter(
        {
            HC_TRENDING: {"books_trending": {"ids": list(range(21, 0, -1))}},
            HC_DISCOVERY_BOOKS: {"books": [row(key, rating=4.25) for key in range(2, 22)]},
        }
    )
    result = await source.discovery("trending", 2, date(2026, 9, 18))
    assert result.has_more
    assert result.items[0].rating == 4.25
    assert "rating" in calls[1]["query"]
    assert [b.external_id for b in result.items] == [str(key) for key in range(21, 1, -1)]
    assert len(calls) == 2 and calls[0]["variables"] == {"offset": 20}
    # Hydration IDs are sorted for cache reuse; presentation retains provider rank.
    assert calls[1]["variables"]["ids"] == list(range(2, 22))


@pytest.mark.parametrize("value", [None, {}, [True], [1, 1], [-1], [2147483648], ["42"]])
async def test_malformed_trending_never_looks_like_an_empty_success(value):
    source, _ = adapter({HC_TRENDING: {"books_trending": {"ids": value}}})
    with pytest.raises(AdapterError):
        await source.discovery("trending", 1, date(2026, 9, 18))


async def test_provider_declared_error_is_not_an_empty_shelf():
    source, _ = adapter({HC_TRENDING: {"books_trending": {"ids": [], "error": "private"}}})
    with pytest.raises(AdapterError, match="unexpected response"):
        await source.discovery("trending", 1, date(2026, 9, 18))


async def test_empty_shelf_does_not_fetch_editions_or_books():
    source, calls = adapter({HC_TRENDING: {"books_trending": {"ids": []}}})
    assert not (await source.discovery("trending", 1, date(2026, 9, 18))).items
    assert len(calls) == 1


async def test_missing_hydrated_book_is_explicit_and_unrequested_ids_rejected():
    responses = {
        HC_TRENDING: {"books_trending": {"ids": [1, 2]}},
        HC_DISCOVERY_BOOKS: {"books": [row(2)]},
    }
    source, _ = adapter(responses)
    result = await source.discovery("trending", 1, date(2026, 9, 18))
    assert result.warning and [b.external_id for b in result.items] == ["2"]
    responses[HC_DISCOVERY_BOOKS] = {"books": [row(3)]}
    with pytest.raises(AdapterError):
        await source.discovery("trending", 1, date(2026, 9, 18))


async def test_new_releases_use_a_bounded_date_window_and_not_search_popularity():
    source, calls = adapter({HC_RECENT: {"books": [row(1, release_date="2026-09-17")]}})
    result = await source.discovery("new-releases", 1, date(2026, 9, 18))
    assert result.items[0].release_date == date(2026, 9, 17)
    assert calls[0]["variables"] == {"from": "2026-06-20", "to": "2026-09-18", "offset": 0}


@pytest.mark.parametrize(
    "extra",
    [
        {"release_date": "2027-01-01"},
        {"release_date": "2020-01-01"},
        {"release_date": None},
        {"release_date": "invalid"},
        {"release_date": "2026-09-17", "canonical_id": 22},
    ],
)
async def test_invalid_release_date_or_redirect_does_not_claim_new_publication(extra):
    source, _ = adapter({HC_RECENT: {"books": [row(1, **extra)]}})
    with pytest.raises(AdapterError):
        await source.discovery("new-releases", 1, date(2026, 9, 18))


def edition(key, edition_day, *, work_day=None, tags=None):
    return {
        "id": key + 100,
        "release_date": edition_day,
        "book": row(
            key,
            release_date=work_day,
            cached_tags=tags,
            cached_contributors=[{"contribution": "Author", "author": {"name": "Ruby Dixon"}}],
        ),
    }


async def test_upcoming_accepts_a_future_audiobook_date_and_prefers_it_over_the_work_date():
    source, calls = adapter(
        {
            HC_UPCOMING: {
                "editions": [
                    edition(
                        7,
                        "2026-10-27",
                        work_day="2026-09-01",
                        tags=[
                            "Romance",
                            {"tag": "Science Fiction"},
                            {"name": "Not a tracked shelf"},
                        ],
                    )
                ]
            }
        }
    )
    result = await source.upcoming(date(2026, 10, 1), date(2026, 10, 31), 1)
    book = result.items[0]
    assert book.release_date == date(2026, 10, 27)
    assert book.date_basis == "audiobook"
    assert book.genres == ["romance", "science-fiction"]
    assert book.title == "Book 7"
    assert calls[0]["variables"] == {"from": "2026-10-01", "to": "2026-10-31", "offset": 0}
    assert "reading_format_id: {_eq: 2}" in calls[0]["query"]
    rejected, _ = adapter({HC_RECENT: {"books": [row(1, release_date="2026-10-27")]}})
    with pytest.raises(AdapterError):
        await rejected.discovery("new-releases", 1, date(2026, 9, 22))


async def test_upcoming_skips_a_redirect_and_a_second_edition_without_failing_the_month():
    source, calls = adapter(
        {
            HC_UPCOMING: {
                "editions": [
                    edition(7, "2026-10-02"),
                    {
                        "id": 200,
                        "release_date": "2026-10-03",
                        "book": row(9, canonical_id=22, release_date="2026-10-03"),
                    },
                    edition(7, "2026-10-20"),
                    edition(8, "2026-10-04"),
                ]
            }
        }
    )
    result = await source.upcoming(date(2026, 10, 1), date(2026, 10, 31), 1)
    assert [(item.external_id, item.release_date) for item in result.items] == [
        ("7", date(2026, 10, 2)),
        ("8", date(2026, 10, 4)),
    ]
    assert "book: {canonical_id: {_is_null: true}}" in calls[0]["query"]


async def test_upcoming_reads_genres_grouped_by_category():
    source, _ = adapter(
        {
            HC_UPCOMING: {
                "editions": [
                    edition(
                        4,
                        "2026-10-08",
                        tags={
                            "Genre": [
                                {
                                    "tag": "Dark Fantasy",
                                    "count": 4,
                                    "category": "Genre",
                                },
                                {"tag": "Science Fiction & Fantasy", "count": 2},
                            ],
                            "Mood": [{"tag": "Adventurous", "count": 3}],
                            "Content Warning": [{"tag": "Violence", "count": 1}],
                        },
                    )
                ]
            }
        }
    )
    result = await source.upcoming(date(2026, 10, 1), date(2026, 10, 31), 1)
    assert result.items[0].genres == ["fantasy", "science-fiction"]


async def test_upcoming_rejects_a_date_outside_the_month_and_an_unknown_tag_shape():
    outside, _ = adapter({HC_UPCOMING: {"editions": [edition(1, "2026-11-01")]}})
    with pytest.raises(AdapterError):
        await outside.upcoming(date(2026, 10, 1), date(2026, 10, 31), 1)
    unknown, _ = adapter(
        {HC_UPCOMING: {"editions": [edition(1, "2026-10-02", tags=[{"slug": "romance"}])]}}
    )
    with pytest.raises(AdapterError):
        await unknown.upcoming(date(2026, 10, 1), date(2026, 10, 31), 1)


async def test_upcoming_month_walks_every_page_and_keeps_the_first_edition():
    calls = []

    async def request(method, path, *, json):
        calls.append(json)
        offset = json["variables"]["offset"]
        if offset == 0:
            editions = [edition(key, "2026-10-02") for key in range(1, 102)]
        else:
            editions = [
                edition(100, "2026-10-20"),
                edition(101, "2026-10-03"),
                edition(102, "2026-10-04"),
            ]
        return {"data": {"editions": editions}}

    result = await Hardcover(request).upcoming_month(date(2026, 10, 1), date(2026, 10, 31))
    assert [call["variables"]["offset"] for call in calls] == [0, 100]
    assert "limit: 101" in calls[0]["query"]
    kept = {item.external_id: item.release_date for item in result.items}
    assert list(kept) == [str(key) for key in range(1, 103)]
    assert kept["100"] == date(2026, 10, 2)
    assert kept["101"] == date(2026, 10, 3)
    assert not result.has_more and result.warning is None


async def test_related_suggestions_preserve_provider_order_and_exclude_the_seed():
    source, calls = adapter(
        {
            HC_RELATED: {"books": [row(1, cached_similar_book_ids=[3, 1, 2])]},
            HC_DISCOVERY_BOOKS: {"books": [row(2), row(3)]},
        }
    )
    result = await source.related("1")
    assert [b.external_id for b in result.items] == ["3", "2"]
    assert calls[1]["variables"] == {"ids": [2, 3]}


async def test_related_rejects_an_unknown_cached_similarity_shape():
    source, _ = adapter({HC_RELATED: {"books": [row(1, cached_similar_book_ids={})]}})
    with pytest.raises(AdapterError):
        await source.related("1")
