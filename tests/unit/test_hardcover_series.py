from copy import deepcopy

import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.hardcover_series import SeriesPage, advance, page, position


def member(key=1, book_id=42, **changes):
    return {
        "id": key,
        "position": "1.50",
        "details": "1.5",
        "compilation": False,
        "featured": True,
        "book": {
            "id": book_id,
            "title": f"Book {book_id}",
            "cached_contributors": [{"author": {"name": "Writer"}}],
            "is_partial_book": False,
            "release_date": "2020-01-01",
        },
        **changes,
    }


def header(items=None):
    return {
        "id": 9,
        "name": "Series",
        "description": "Catalog description",
        "book_series_aggregate": {"aggregate": {"count": 2}},
        "book_series": items if items is not None else [member(11), member(12, 43)],
    }


async def test_keyset_preserves_duplicate_positions_and_full_member_evidence():
    calls = []

    async def query(document, variables):
        calls.append((document, variables))
        return {"series": [header()]}

    observed = await page(query, "9", 10)
    assert observed.cursor == 12
    assert [item["position"] for item in observed.items] == ["1.5", "1.5"]
    assert observed.items[0]["book"]["authors"] == ["Writer"]
    assert observed.items[0]["release_date"] == "2020-01-01"
    assert "featured" not in observed.items[0]  # This is not primary membership.
    assert calls[0][1] == {"id": 9, "after": 10, "limit": 100}
    assert "book_series_aggregate" in calls[0][0]
    assert "distinct_on" not in calls[0][0]


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(id=10),
        lambda r: r.update(name=""),
        lambda r: r.update(description="x" * 50001),
        lambda r: r.update(book_series_aggregate={"aggregate": {"count": True}}),
        lambda r: r.update(book_series_aggregate={"aggregate": {"count": 1001}}),
        lambda r: r.update(book_series=[member(1), member(1)]),
        lambda r: r.update(book_series=[member(2), member(1)]),
        lambda r: r.update(book_series=[member(2**63)]),
        lambda r: r.update(book_series=[member(compilation="false")]),
        lambda r: r.update(book_series=[member(position="NaN")]),
        lambda r: r.update(book_series=[member(position=True)]),
        lambda r: r["book_series"][0]["book"].update(release_date="unknown"),
        lambda r: r["book_series"][0]["book"].update(is_partial_book=None),
        lambda r: r["book_series"][0]["book"].update(canonical_id=0),
        lambda r: r.update(book_series=None),
    ],
)
async def test_malformed_or_unbounded_observation_is_not_an_empty_catalog(change):
    row = header()
    change(row)

    async def query(*args):
        return {"series": [row]}

    with pytest.raises(AdapterError) as error:
        await page(query, "9")
    assert error.value.kind == FailureKind.PARSER


@pytest.mark.parametrize("value", [True, "Infinity", "-Infinity", "NaN", "bad", 10**9])
def test_positions_are_bounded_and_never_guessed(value):
    with pytest.raises(AdapterError):
        position(value)


async def test_unavailable_and_merged_series_have_distinct_errors():
    async def missing(*args):
        return {"series": []}

    async def merged(*args):
        return {"series": [{**header(), "canonical_id": 99}]}

    for query, kind in [(missing, FailureKind.NOT_FOUND), (merged, FailureKind.UNSUPPORTED)]:
        with pytest.raises(AdapterError) as error:
            await page(query, "9")
        assert error.value.kind == kind


def test_two_pass_observation_detects_same_size_replacement_and_incomplete_pages():
    info = {"count": 2, "name": "Series"}
    rows = [{"entry_id": "1"}, {"entry_id": "2"}]
    stage, done = advance(None, SeriesPage(info, rows, 2))
    assert not done
    assert stage["phase"] == "verify" and stage["cursor"] == 0
    for changed in [[], [{"entry_id": "3"}, {"entry_id": "4"}]]:
        with pytest.raises(AdapterError):
            advance(deepcopy(stage), SeriesPage(info, changed, 4))
    with pytest.raises(AdapterError):
        advance(stage, SeriesPage({**info, "name": "Changed"}, rows, 2))
    with pytest.raises(AdapterError):
        advance(None, SeriesPage(info, [], 0))
    stage, done = advance(stage, SeriesPage(info, rows[:1], 1))
    assert not done
    stage, done = advance(stage, SeriesPage(info, rows[1:], 2))
    assert done


def test_empty_series_requires_an_independent_empty_verification():
    empty = SeriesPage({"count": 0}, [], 0)
    stage, complete = advance(None, empty)
    assert not complete
    assert advance(stage, empty)[1]


def test_small_series_uses_two_requests_without_empty_terminators():
    info = {"count": 2, "name": "Series"}
    rows = [{"entry_id": "1"}, {"entry_id": "2"}]
    observed = SeriesPage(info, rows, 2)
    stage, complete = advance(None, observed)
    assert not complete and stage["cursor"] == 0
    assert advance(stage, observed)[1]
