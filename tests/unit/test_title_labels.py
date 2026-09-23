import json
from pathlib import Path

import pytest

from app.domain.catalog_titles import identity_authors, parse_title_labels, recording_kind

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "library-titles.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["title"] for case in CASES])
def test_library_titles_split_into_book_and_recording(case):
    labels = parse_title_labels(case["title"])
    assert labels.title == case["base"]
    assert labels.part == case.get("part")
    assert labels.part_total == case.get("part_total")
    assert labels.abridged == case.get("abridged")
    assert labels.series == case.get("series")
    assert labels.sequence == case.get("sequence")
    assert labels.series_title == case.get("series_title")
    assert recording_kind(case["title"], case["authors"]) == case.get("kind")
    kept, credits = identity_authors(case["authors"])
    assert kept == case.get("identity_authors", case["authors"])
    assert credits == case.get("credits", [])


@pytest.mark.parametrize(
    "title",
    [
        "The Giver of Stars: A Study Guide",
        "Red Rising: The Graphic Novel",
        "The Silo Saga Omnibus",
        "Harry Potter Box Set",
        "Dark Age: Summary",
    ],
)
def test_different_content_is_never_stripped(title):
    assert parse_title_labels(title).title == title


@pytest.mark.parametrize("title", ["Book 3 of 2", "Part 0 of 3", "(1 of 3)"])
def test_impossible_or_bare_part_numbers_are_kept(title):
    labels = parse_title_labels(title)
    assert labels.part is None
    assert labels.title == title


def test_labels_in_either_order_are_removed_once():
    labels = parse_title_labels("Dark Age [Dramatized Adaptation] (2 of 3) (Unabridged)")
    assert (labels.title, labels.part, labels.part_total) == ("Dark Age", 2, 3)
    assert labels.dramatized and labels.abridged is False
    assert parse_title_labels("Dark Age (1 of 3) (2 of 3)").title == "Dark Age (1 of 3)"
