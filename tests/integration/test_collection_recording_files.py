"""Actual MP3 tags corroborate a frozen collection choice, never a folder label alone."""

from types import SimpleNamespace

import pytest

from app.importing.collection_recordings import conflict
from app.importing.grouping import proposed
from app.importing.inspection import inspect_download
from app.importing.match_evidence import group_evidence
from tests.media_fixtures import audio

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "embedded_narrator,confirmed", [("Garrick Hagon", True), ("Bruce Huntey", False)]
)
def test_recording_folder_requires_matching_embedded_tags(tmp_path, embedded_narrator, confirmed):
    # The directory claims the desired narration even when the bytes contradict it.
    relative = "The Stand/Garrick Hagon/01.mp3"
    source = tmp_path / "Pack" / relative
    audio(source, title="The Stand", author="Stephen King", narrator=embedded_narrator)
    before = source.read_bytes()
    snapshot = inspect_download(tmp_path, "Pack")
    group = proposed(snapshot).groups[0]
    facts = group_evidence(snapshot, group)
    assert facts.titles == ["the stand"]
    assert facts.authors == [["stephen king"]]
    member = SimpleNamespace(
        frozen={
            "descriptor": {"name": "Pack"},
            "collection_review": {
                "paths": ["Pack/" + relative],
                "recording": {"narrator_claim": "Garrick Hagon"},
            },
        }
    )
    reason = conflict([member], group, facts)
    assert (reason is None) == confirmed
    assert source.read_bytes() == before
