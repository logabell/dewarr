"""File evidence stays consistent from inspection through automatic import."""

from types import SimpleNamespace

import pytest

from app.importing.automatic import content_reason
from app.importing.inspection import InspectedGroup, inspect_download
from app.importing.match_evidence import group_evidence
from tests.media_fixtures import audio
from tests.unit.test_automatic_eligibility import RULE, descriptor, release


def test_equivalent_album_titles_form_one_ordered_recording(tmp_path):
    for number, title in [(1, "Angels & Demons"), (2, "Angels and Demons")]:
        audio(
            tmp_path / "book" / f"Angels and Demons Chapter {number:02}.mp3",
            title=title,
            author="Dan Brown",
            tags={"track": ""},
        )
    snapshot = inspect_download(tmp_path.resolve(), "book")
    assert len(snapshot["groups"]) == 1
    group = InspectedGroup.model_validate(snapshot["groups"][0])
    assert [file.track for file in group.files] == [1, 2]
    assert not group_evidence(snapshot, group).issues
    assert (
        content_reason(
            group, {file["path"]: file for file in snapshot["files"]}, {"title": "Angels & Demons"}
        )
        is None
    )


@pytest.mark.parametrize(
    "filename,title,held",
    [
        ("Book (Part 1 of 3).m4b", "Book", True),
        ("Chapter 37.mp3", "Book", True),
        ("Book Chapter 01.mp3", "Book", True),
        ("Book.m4b", "Book", False),
        ("1984.m4b", "1984", False),
        ("Chapter 37.m4b", "Chapter 37", False),
    ],
)
def test_single_file_partial_names_cannot_claim_complete_recording(filename, title, held):
    from app.domain.automatic_eligibility import eligibility
    from app.domain.release_profiles import ReleasePreferences

    value = release().model_copy(update={"title": title, "raw_title": title})
    assert (
        bool(
            eligibility(
                value,
                {"title": title, "authors": value.authors},
                RULE,
                ReleasePreferences(),
                descriptor=descriptor([filename]),
            )
        )
        is held
    )
    group = SimpleNamespace(medium="audio", title=title, files=[SimpleNamespace(path=filename)])
    files = {
        filename: {
            "path": filename,
            "state": "inspected",
            "extension": filename.rsplit(".", 1)[1],
            "technical": {"tags": {"track": "1/1", "disc": "1/1"}},
        }
    }
    assert bool(content_reason(group, files, {"title": title})) is held
