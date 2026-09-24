from copy import deepcopy

import pytest

from app.adapters.contracts import AdapterError
from app.adapters.hardcover_follows import FollowFilters, filter_reason, page
from app.domain.hardcover_subscriptions import advance


def response():
    return {
        "source": [{"id": 9, "name": "Writer"}],
        "books_aggregate": {"aggregate": {"count": 1}},
        "books": [
            {
                "id": 42,
                "title": "Main book",
                "cached_contributors": [{"author": {"name": "Writer"}, "contribution": "Author"}],
                "cached_tags": {},
                "release_date": "2030-01-01",
                "release_year": 2030,
                "is_partial_book": False,
                "book_series": [{"series_id": 9, "position": "1", "compilation": False}],
                "matching_editions": {"aggregate": {"count": 1}},
            }
        ],
    }


async def test_catalog_filters_keep_release_day_and_require_complete_verified_pass():
    data = response()

    async def query(statement, variables):
        assert variables == {"id": 9, "after": 0, "language": "en"}
        return data

    first = await page(query, "series", "9", 0, FollowFilters(language="en"))
    assert first.items[0]["release_date"] == "2030-01-01"
    assert first.items[0]["filter_reason"] is None
    stage, complete = advance(None, first)
    assert not complete
    empty = type(first)(first.info, [], first.cursor)
    stage, complete = advance(stage, empty)
    assert not complete and stage["phase"] == "verify"
    stage, complete = advance(stage, first)
    assert not complete
    _, complete = advance(stage, empty)
    assert complete
    changed = deepcopy(first)
    changed.items[0]["title"] = "Changed during read"
    collecting, _ = advance(None, first)
    verifying, _ = advance(collecting, empty)
    with pytest.raises(AdapterError):
        advance(verifying, changed)
    with pytest.raises(AdapterError):
        advance(None, type(first)(first.info, [], 0))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["books"][0].pop("is_partial_book"),
        lambda d: d["books"][0].update(release_date="sometime"),
        lambda d: d["books"].append(deepcopy(d["books"][0])),
        lambda d: d["books_aggregate"]["aggregate"].update(count=5001),
        lambda d: d.update(source=[]),
    ],
)
async def test_partial_or_malformed_catalog_is_rejected(mutate):
    data = response()
    mutate(data)

    async def query(*args):
        return data

    with pytest.raises(AdapterError):
        await page(query, "author", "9", 0, FollowFilters())


@pytest.mark.parametrize(
    ("change", "reason", "override"),
    [
        ({"classification": "the box set"}, "Box set", {"box_sets": True}),
        ({"classification": "an anthology"}, "Anthology", {"anthologies": True}),
        ({"compilation": True}, "Compilation", {"compilations": True}),
        ({"main_series": False}, "Non-main-series title", {"non_main_series": True}),
    ],
)
def test_default_exclusions_are_explicitly_opt_in(change, reason, override):
    record = {
        "classification": "novel",
        "compilation": False,
        "main_series": True,
        "authors": ["Writer"],
        "language_match": True,
        **change,
    }
    assert filter_reason(record, FollowFilters(), "series") == reason
    assert filter_reason(record, FollowFilters(**override), "series") is None


def test_language_and_coauthors_filters():
    record = {
        "classification": "novel",
        "compilation": False,
        "main_series": True,
        "authors": ["Writer", "Coauthor"],
        "language_match": False,
    }
    assert filter_reason(record, FollowFilters(coauthored=False), "author") == "Co-authored book"
    assert (
        filter_reason(record, FollowFilters(language="en"), "author")
        == "No matching language edition"
    )


async def test_future_year_without_street_date_is_explicitly_unreleased():
    data = response()
    data["books"][0].update(release_date=None, release_year=2099)

    async def query(*args):
        return data

    result = await page(query, "author", "9", 0, FollowFilters())
    assert result.items[0]["coming_soon"] is True
    assert result.items[0]["release_date"] is None
