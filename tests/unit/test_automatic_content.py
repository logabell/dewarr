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


@pytest.mark.parametrize(
    "names,tags,accepted",
    [
        (["01.mp3", "02.mp3", "03.mp3"], [{}, {}, {}], True),
        (["01.mp3", "03.mp3"], [{}, {}], False),
        (["01.mp3", "01.MP3"], [{}, {}], False),
        (["disc1/01.mp3", "disc2/02.mp3"], [{}, {}], False),
        (["01.mp3", "02.mp3"], [{"disc": "1/2"}, {}], False),
        (["01.mp3", "02.mp3"], [{"track": "2/2"}, {"track": "1/2"}], False),
        (["01.mp3", "02.mp3"], [{"track": "1/3"}, {}], False),
    ],
)
def test_completed_manifest_numbered_audio_without_totals(names, tags, accepted):
    files = {
        name: {"path": name, "state": "inspected", "extension": "mp3", "technical": {"tags": tag}}
        for name, tag in zip(names, tags, strict=True)
    }
    group = SimpleNamespace(medium="audio", files=[SimpleNamespace(path=path) for path in files])
    assert (content_reason(group, files, {"title": "A whole book"}) is None) is accepted


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("extension", ["mp3", "m4b"])
def test_selected_audio_prefix_never_establishes_whole_book_content(count, extension):
    names = [f"Book/{i:02}.{extension}" for i in range(1, 4)]
    files = {
        name: {
            "path": name,
            "state": "inspected",
            "extension": extension,
            "technical": {"tags": {"track": str(i)}},
        }
        for i, name in enumerate(names[:count], 1)
    }
    group = SimpleNamespace(
        medium="audio", title="Book", files=[SimpleNamespace(path=path) for path in files]
    )
    assert "omitted" in content_reason(
        group, files, {"title": "Writer collection"}, omitted_audio_paths=set(names[count:])
    )
    assert (
        content_reason(group, files, {"title": "Writer collection"}, omitted_audio_paths=set())
        is None
    )


@pytest.mark.parametrize(
    "omitted,held",
    [
        ({"Book/CD2/01.mp3"}, True),
        ({"Other Book/CD1/01.mp3"}, False),
    ],
)
def test_selected_audio_scope_includes_omitted_discs_but_not_other_books(omitted, held):
    path = "Book/CD1/01.mp3"
    group = SimpleNamespace(medium="audio", title="Book", files=[SimpleNamespace(path=path)])
    files = {path: {"path": path, "state": "inspected", "extension": "mp3"}}
    reason = content_reason(
        group, files, {"title": "Writer collection"}, omitted_audio_paths=omitted
    )
    assert bool(reason) is held


@pytest.mark.parametrize(
    "omitted,held",
    [
        ({"Other Book.m4b", "1984.m4b"}, False),
        ({"Book (Part 2 of 3).m4b"}, True),
        ({"02.m4b"}, True),
    ],
)
def test_named_whole_m4b_can_be_selected_from_flat_author_pack(omitted, held):
    group = SimpleNamespace(medium="audio", title="Book", files=[SimpleNamespace(path="Book.m4b")])
    files = {"Book.m4b": {"path": "Book.m4b", "state": "inspected", "extension": "m4b"}}
    reason = content_reason(
        group, files, {"title": "Writer collection"}, omitted_audio_paths=omitted
    )
    assert bool(reason) is held
