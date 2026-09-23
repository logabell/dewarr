from types import SimpleNamespace

import pytest

from app.importing.automatic import content_reason


@pytest.mark.parametrize(
    "tracks,discs,accepted",
    [
        (["1/2", "2/2"], ["1", "1"], True),
        (["1/3", "2/3"], ["1", "1"], False),
        (["1/2", "1/2"], ["1", "1"], False),
        (["1/2", "2/2"], ["2/2", "2/2"], False),
        (["1/999999999999999999999", "2/999999999999999999999"], ["1", "1"], False),
        (["1", "2"], ["1", "1"], False),
        (["1/1"], ["1/1"], True),
        (["2/5"], ["1"], False),
        (["1"], ["2"], False),
    ],
)
def test_audio_completeness_rejects_gaps_parts_and_unbounded_track_counts(tracks, discs, accepted):
    files = {
        f"{i}.mp3": {
            "path": f"{i}.mp3",
            "state": "inspected",
            "extension": "mp3",
            "technical": {"tags": {"track": track, "disc": disc}},
        }
        for i, (track, disc) in enumerate(zip(tracks, discs, strict=True))
    }
    group = SimpleNamespace(medium="audio", files=[SimpleNamespace(path=path) for path in files])
    assert (content_reason(group, files, {"title": "A whole book"}) is None) == accepted


@pytest.mark.parametrize(
    "extension,name,title",
    [
        ("epub", "sample.epub", "Book"),
        ("epub", "book.epub", "Book excerpt"),
        ("pdf", "book.pdf", "Book"),
    ],
)
def test_partial_labels_and_noncertified_ebook_containers_need_review(extension, name, title):
    group = SimpleNamespace(medium="ebook", files=[SimpleNamespace(path=name)])
    assert content_reason(
        group,
        {name: {"path": name, "extension": extension, "state": "inspected"}},
        {"title": title},
    )


def test_one_part_of_a_book_released_in_parts_needs_review():
    group = SimpleNamespace(medium="ebook", files=[SimpleNamespace(path="book.epub")])
    files = {"book.epub": {"path": "book.epub", "extension": "epub", "state": "inspected"}}
    assert "one part" in content_reason(group, files, {"title": "Dark Age (Part 1 of 3)"})
    assert content_reason(group, files, {"title": "Dark Age"}) is None


def test_automatic_chapter_merge_goes_straight_to_the_library():
    from app.importing.automatic import importer_message

    plain = {"plan": {"items": [{"title": "Harbor", "conversion": None}]}}
    merged = {"plan": {"items": [{"title": "Harbor", "conversion": {"output_name": "Harbor.m4b"}}]}}
    assert importer_message(plain) == (
        "Matched books sent to the importer; awaiting library confirmation"
    )
    status = importer_message(merged)
    assert "M4B" in status
    assert "review" not in status.lower()
