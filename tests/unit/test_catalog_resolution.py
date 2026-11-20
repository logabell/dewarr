from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.importing import catalog_resolution
from app.importing.match_evidence import MatchEvidence


@pytest.mark.parametrize(
    "title,authors,series,expected",
    [
        (
            "We Are Legion (We Are Bob)",
            [["dennis e. taylor"]],
            [],
            True,
        ),
        (
            "We Are Legion (We Are Bob)",
            [["another author"]],
            [],
            False,
        ),
        (
            "We Are Legion (We Are Bob)",
            [],
            [],
            False,
        ),
        (
            "For We Are Many (Bobiverse Book 2)",
            [["dennis e. taylor"]],
            [{"name": "Bobiverse", "position": "2"}],
            True,
        ),
        (
            "For We Are Many (Bobiverse Book 3)",
            [["dennis e. taylor"]],
            [{"name": "Bobiverse", "position": "2"}],
            False,
        ),
        (
            "For We Are Many (Other Series Book 2)",
            [["dennis e. taylor"]],
            [{"name": "Bobiverse", "position": "2"}],
            False,
        ),
    ],
)
async def test_schedule_requires_catalogue_backed_title_and_author(
    monkeypatch, title, authors, series, expected
):
    requester = SimpleNamespace(id=uuid4())
    work = SimpleNamespace(
        id=uuid4(),
        title=("We Are Legion" if title.startswith("We Are Legion") else "For We Are Many"),
        authors=["Dennis E. Taylor"],
    )
    row = SimpleNamespace(
        id=uuid4(),
        inspection_id=uuid4(),
        evidence={},
        message="",
    )
    facts = MatchEvidence(
        titles=[title],
        authors=authors,
        identifiers=[{"namespace": "isbn", "value": "9781234567897"}],
    )
    match = SimpleNamespace(
        status="review",
        evidence=facts,
        truncated=False,
        candidates=[SimpleNamespace(identifier_match=False)],
        group_key="test-group",
    )
    inspection = SimpleNamespace(id=row.inspection_id)
    grouping = SimpleNamespace(groups=[SimpleNamespace(key="test-group", medium="ebook")])
    inputs = {"requirements": {"medium": "ebook"}}

    operations = []
    db = SimpleNamespace(
        add=operations.append,
        flush=AsyncMock(),
        get=AsyncMock(return_value=inspection),
    )

    async def fake_context(db, row, group_key):
        return inputs, requester, work, None

    async def fake_catalog_identity(db, work, owner_id):
        return {"title": work.title, "authors": work.authors, "series": series}

    async def fake_enqueue(db, kind, **kwargs):
        return 123

    monkeypatch.setattr(catalog_resolution, "context", fake_context)
    monkeypatch.setattr(
        catalog_resolution,
        "current_grouping",
        AsyncMock(return_value=(1, grouping)),
    )
    monkeypatch.setattr(catalog_resolution, "enqueue", fake_enqueue)
    monkeypatch.setattr("app.domain.book_sources.identity", fake_catalog_identity)

    result = await catalog_resolution.schedule(db, row, [match])

    assert result is expected

    if expected:
        assert len(operations) == 1
        operation = operations[0]
        assert operation.kind == "metadata.resolve-import"
        assert operation.owner_id == requester.id
        assert operation.job_id == 123
        assert row.evidence["catalog_resolution"] == str(operation.id)
        assert db.flush.await_count == 1
    else:
        assert operations == []
        assert "catalog_resolution" not in row.evidence
