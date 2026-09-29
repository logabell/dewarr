import pytest

from app.adapters.contracts import AdapterError
from app.adapters.hardcover_bibliography import bibliography


async def test_bibliography_keeps_only_full_canonical_english_candidates_with_aliases():
    calls = []

    async def query(document, variables):
        calls.append(variables)
        if "CollectionBibliography(" in document:
            return {
                "books": [{"id": 42, "contributions": [{"author": {"id": 7, "name": "Writer"}}]}]
            }
        return {
            "books": [
                {
                    "id": 42,
                    "title": "Angels & Demons",
                    "cached_contributors": [{"author": {"name": "Writer"}}],
                    "english": [{"title": "Angels and Demons"}],
                    "book_series": [{"position": 1, "series": {"id": 8, "name": "Sequence"}}],
                },
                {"id": 43, "title": "Translation", "english": [], "book_series": []},
            ]
        }

    result = await bibliography(query, "42")
    assert [b["title"] for b in result["books"]] == ["Angels & Demons"]
    assert result["books"][0]["aliases"] == ["Angels and Demons"]
    assert result["books"][0]["series"][0]["position"] == "1"
    assert calls == [{"id": 42}, {"ids": [7]}]
    assert not result["truncated"]


async def test_bibliography_missing_work_does_not_look_up_unrelated_author():
    async def query(*args):
        return {"books": []}

    with pytest.raises(AdapterError):
        await bibliography(query, "42")
