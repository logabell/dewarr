from uuid import UUID

import pytest
from pydantic import ValidationError

from app.importing.examples import naming_examples
from app.importing.naming import (
    ImportGroup,
    NamingMetadata,
    NamingProfile,
    PlannedSourceFile,
    component,
    plan_import,
)


def group(number=1, medium="ebook", **metadata):
    return ImportGroup(
        id=UUID(int=number),
        work_id=UUID(int=number),
        version_id=UUID(int=100 + number),
        medium=medium,
        metadata=NamingMetadata(title="Harbor", authors=["Writer"], **metadata),
        files=[
            PlannedSourceFile(
                path=f"book{number}.epub" if medium == "ebook" else f"book{number}.m4b"
            )
        ],
    )


def test_examples_produce_separate_items_for_media_recordings_and_series_children():
    plan = plan_import(naming_examples(), NamingProfile())
    assert plan.expected_items == 5 and plan.held_items == 0
    assert not plan.publication_available
    paths = [file.destination for item in plan.items for file in item.files]
    assert (
        "ebooks/Alex Morgan/Harbor Trilogy/01 - 2017 - The First Harbor - First edition/"
        "The First Harbor.epub" in paths
    )
    assert (
        "audiobooks/Alex Morgan/Harbor Trilogy/01 - 2024 - The First Harbor - Casey Reed/"
        "The First Harbor.m4b" in paths
    )
    harbor = next(item for item in plan.items if item.title == "Beyond the Harbor")
    assert harbor.conversion is None
    assert [path.split("/")[-1] for path in paths if path.endswith("Beyond the Harbor.mp3")] == [
        "001 - Beyond the Harbor.mp3",
        "002 - Beyond the Harbor.mp3",
    ]
    merged_plan = plan_import(naming_examples(), NamingProfile(merge_mp3_chapters=True))
    merged = next(item for item in merged_plan.items if item.title == "Beyond the Harbor")
    assert merged.conversion is not None
    assert merged.conversion.sources == [
        "Harbor.Complete/Book_02/track01.mp3",
        "Harbor.Complete/Book_02/track02.mp3",
    ]
    assert any("chapterized M4B" in warning for warning in merged.warnings)
    assert paths[-1] == "ebooks/Alex Morgan/A Standalone Story/A Standalone Story.epub"
    assert "edition_year" in plan.items[-1].missing_metadata


@pytest.mark.parametrize(
    "template",
    [
        "../{title}",
        "/{title}",
        "{title}/..",
        "{title.__class__}",
        "{unknown}",
        "{title}[missing]",
        "{title}[[{series}]]",
        "{title}\x00",
        "{title}/{track}",
    ],
)
def test_unsafe_or_unsupported_templates_are_rejected(template):
    with pytest.raises(ValidationError):
        NamingProfile(ebook_folder=template)


@pytest.mark.parametrize(
    "path",
    [
        "/outside/book.epub",
        "../book.epub",
        "books/../book.epub",
        "a\\book.epub",
        "a//book.epub",
        "book\x00.epub",
    ],
)
def test_source_paths_remain_confined_and_relative(path):
    with pytest.raises(ValidationError):
        PlannedSourceFile(path=path)


def test_missing_years_do_not_borrow_tracker_posting_year_and_decimal_sequence_is_preserved():
    item = plan_import(
        [group(sequence="1.5", series="Series", source_posted_year=2025)], NamingProfile()
    ).items[0]
    assert item.folder == "ebooks/Writer/Series/01.5 - Harbor"
    assert "2025" not in item.folder


def test_sanitization_unicode_device_names_and_long_components_are_stable():
    assert component("CON.txt") == "_CON.txt"
    assert component("A/B: C?") == "A B C"
    assert component("Cafe\u0301") == "Café"
    name = component("海" * 200)
    assert len(name.encode()) <= 180 and name == component("海" * 200)
    assert component("海" * 199 + "岸") != name


def test_distinct_versions_with_same_label_get_stable_collision_suffix():
    first, second = group(1), group(2)
    a = plan_import([first, second], NamingProfile())
    b = plan_import([second, first], NamingProfile())
    assert a == b and a.expected_items == 2
    assert a.items[0].folder != a.items[1].folder
    assert second.version_id.hex in a.items[1].folder


def test_ambiguous_children_do_not_block_valid_pack_siblings():
    first, second = group(1), group(2, medium="audio")
    second.files = [PlannedSourceFile(path="book2/1.mp3"), PlannedSourceFile(path="book2/2.mp3")]
    plan = plan_import([first, second], NamingProfile())
    assert plan.expected_items == 1 and plan.held_items == 1
    assert plan.items[1].files == [] and "track" in plan.items[1].reason


def test_multiple_discs_keep_order_and_custom_names_cannot_discard_track_order():
    item = group(medium="audio")
    item.files = [
        PlannedSourceFile(path="CD2/track.mp3", disc=2, track=1),
        PlannedSourceFile(path="CD1/track.mp3", disc=1, track=1),
    ]
    separate = NamingProfile(merge_mp3_chapters=False)
    plan = plan_import([item], separate)
    assert [file.destination.split("/")[-1] for file in plan.items[0].files] == [
        "01-001 - Harbor.mp3",
        "02-001 - Harbor.mp3",
    ]
    assert plan.items[0].conversion is None
    held = plan_import(
        [item], NamingProfile(audio_filename="{title}", merge_mp3_chapters=False)
    ).items[0]
    assert held.state == "held" and "playback order" in held.reason
    merged = plan_import(
        [item], NamingProfile(audio_filename="{title}", merge_mp3_chapters=True)
    ).items[0]
    assert merged.state == "ready"
    assert merged.conversion.output_name == "Harbor.m4b"
    assert merged.conversion.sources == ["CD1/track.mp3", "CD2/track.mp3"]


def test_multi_file_m4b_stays_separate_when_chapter_merge_is_mp3_only():
    item = group(medium="audio")
    item.files = [
        PlannedSourceFile(path="disc1.m4b", disc=1, track=1),
        PlannedSourceFile(path="disc2.m4b", disc=2, track=1),
    ]
    planned = plan_import([item], NamingProfile()).items[0]
    assert planned.conversion is None
    assert [file.destination.split(".")[-1] for file in planned.files] == ["m4b", "m4b"]


def test_duplicate_representations_and_shared_source_files_are_held():
    first, second = group(1), group(2)
    second.version_id = first.version_id
    assert plan_import([first, second], NamingProfile()).held_items == 2
    second.version_id = UUID(int=1000)
    second.files = first.files
    assert plan_import([first, second], NamingProfile()).held_items == 2


def test_companion_only_incomplete_and_wrong_medium_are_not_valid_items():
    for files in (
        [PlannedSourceFile(path="Companion.pdf", role="supplement")],
        [PlannedSourceFile(path="Book.epub", complete=False)],
        [PlannedSourceFile(path="Audio.mp3")],
    ):
        item = group()
        item.files = files
        assert plan_import([item], NamingProfile()).items[0].state == "held"


def test_same_edition_formats_keep_extensions_and_primary_warning():
    item = group()
    item.files = [PlannedSourceFile(path="Book.epub"), PlannedSourceFile(path="Book.pdf")]
    planned = plan_import([item], NamingProfile()).items[0]
    assert planned.state == "ready" and len(planned.files) == 2
    assert [file.destination.split(".")[-1] for file in planned.files] == ["epub", "pdf"]
    assert "primary" in planned.warnings[0]


def test_nested_layout_is_explicitly_uncertified_and_owned_children_skip():
    first, second = group(1, sequence="1", series="Series"), group(2)
    second.decision = "skip-owned"
    plan = plan_import([first, second], NamingProfile(layout="nested"))
    assert plan.expected_items == 1 and plan.skipped_items == 1
    assert plan.items[0].folder == "ebooks/Writer/Series/01 - Harbor/01 - Harbor"
    assert "certification" in plan.items[0].warnings[0]
    assert not plan.publication_available


def test_item_ancestor_collision_holds_both_before_scanner_can_merge_them():
    first, second = group(1), group(2, series="Harbor")
    second.metadata.title = "Book two"
    plan = plan_import([first, second], NamingProfile())
    assert plan.held_items == 2
    assert all(not item.files and "contain another book" in item.reason for item in plan.items)


def test_author_series_sequence_title_names_keep_medium_year_optional():
    audio = group(
        medium="audio",
        sequence="1",
        series="Harry Potter",
        recording_year=1999,
        original_year=1997,
        source_posted_year=2024,
    )
    audio.metadata.authors = ["J. K. Rowling"]
    audio.metadata.title = "Harry Potter and the Philosopher's Stone"
    planned = plan_import(
        [audio],
        NamingProfile(
            audio_folder="{author}/[{series}/][{sequence} ]{title}",
            audio_filename="[{sequence} - ][{series} - ]{title}[ ({year})]",
        ),
    ).items[0]
    assert planned.state == "ready"
    assert planned.files[0].destination == (
        "audiobooks/J. K. Rowling/Harry Potter/"
        "01 Harry Potter and the Philosopher's Stone/"
        "01 - Harry Potter - Harry Potter and the Philosopher's Stone (1999).m4b"
    )
    ebook = plan_import(
        [
            group(
                sequence="2",
                series="Harbor Trilogy",
                edition_year=2017,
                recording_year=2020,
            )
        ],
        NamingProfile(
            ebook_folder="{author}/[{series}/][{sequence} ]{title}",
            ebook_filename="[{sequence} - ][{series} - ]{title}[ ({year})]",
        ),
    ).items[0]
    assert ebook.files[0].destination == (
        "ebooks/Writer/Harbor Trilogy/02 Harbor/02 - Harbor Trilogy - Harbor (2017).epub"
    )
    standalone = plan_import(
        [group(original_year=1990, source_posted_year=2025)],
        NamingProfile(ebook_filename="[{sequence} - ][{series} - ]{title}[ ({year})]"),
    ).items[0]
    assert standalone.state == "ready"
    assert standalone.files[0].destination.endswith("/Harbor.epub")
    required = plan_import(
        [group(sequence="1", series="Harbor Trilogy")],
        NamingProfile(ebook_filename="{sequence} - {series} - {title} ({year})"),
    ).items[0]
    assert required.state == "held" and required.reason == "Required metadata is missing: year"


def test_keep_original_names_and_format_tokens_do_not_convert_media():
    item = group(medium="audio")
    item.files = [PlannedSourceFile(path="Download/Actual.MP3")]
    plan = plan_import([item], NamingProfile(rename_files=False)).items[0]
    assert plan.files[0].destination.endswith("/Actual.mp3")
    plan = plan_import(
        [item],
        NamingProfile(
            audio_folder="{author}/{title} - {formats}", audio_filename="{title} - {format}"
        ),
    ).items[0]
    assert plan.files[0].destination.endswith("/Harbor - MP3/Harbor - MP3.mp3")


def test_a_lone_part_keeps_its_own_labelled_item_and_parts_share_a_version():
    parts = [group(index, medium="audio", part_index=index, part_total=3) for index in (1, 2)]
    for part in parts:
        part.version_id = UUID(int=500)
    plan = plan_import(parts, NamingProfile())
    assert [item.state for item in plan.items] == ["ready", "ready"]
    folders = [item.files[0].destination.rsplit("/", 1)[0] for item in plan.items]
    assert folders[0].endswith("Harbor (Part 1 of 3)")
    assert folders[1].endswith("Harbor (Part 2 of 3)")
    assert any(
        "until every part is in the library" in warning and "disc folders" in warning
        for warning in plan.items[0].warnings
    )
    separate = plan_import(parts, NamingProfile(), combine_parts=False).items[0]
    assert any("grouped with the other parts" in warning for warning in separate.warnings)
    labelled = plan_import(
        [group(medium="audio", part_index=1, part_total=3)],
        NamingProfile(audio_folder="{author}/{title} - {part}"),
    ).items[0]
    assert labelled.files[0].destination.split("/")[-2] == "Harbor - Part 1 of 3"


def test_a_part_number_needs_its_total():
    with pytest.raises(ValidationError):
        NamingMetadata(title="Harbor", authors=["Writer"], part_index=2)
    with pytest.raises(ValidationError):
        NamingMetadata(title="Harbor", authors=["Writer"], part_index=3, part_total=2)
