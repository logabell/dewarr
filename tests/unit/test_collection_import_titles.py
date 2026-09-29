"""Reviewed file mappings permit descriptive subtitles, never different works."""

from types import SimpleNamespace

import pytest

from app.importing.linked_download import request_file_conflicts


@pytest.mark.parametrize(
    "title,author,accepted",
    [
        ("The Short Second Life of Bree Tanner: An Eclipse Novella", "Stephenie Meyer", True),
        ("The Short Second Life of Bree Tanner: A Graphic Novel", "Stephenie Meyer", False),
        ("The Short Second Life of Bree Tanner: Study Guide", "Stephenie Meyer", False),
        ("Eclipse", "Stephenie Meyer", False),
        ("The Short Second Life of Bree Tanner", "Another Writer", False),
    ],
)
def test_reviewed_collection_subtitle_still_requires_same_primary_title_and_author(
    title, author, accepted
):
    work = SimpleNamespace(
        title="The Short Second Life of Bree Tanner", authors=["Stephenie Meyer"], language="en"
    )
    facts = SimpleNamespace(titles=[title], authors=[[author.lower()]], languages=["en"], issues=[])
    conflicts = request_file_conflicts(work, {"source": "mam"}, facts, reviewed_collection=True)
    assert (not conflicts) == accepted
    if accepted:
        assert request_file_conflicts(work, {"source": "mam"}, facts)
