from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.adapters.audiobookshelf import ABSFile, ABSItem
from app.importing.execution import (
    detection_needs_another_scan,
    find_item,
    matches,
    verified_ebook_files,
)
from app.importing.publication import PublicationError, PublicationSpec, PublishFile


@pytest.mark.parametrize("indices", [(2, 1), (None, None), (1, 1), (1, None)])
def test_incorrect_or_unverified_playback_order_cannot_confirm(indices):
    files = [
        ABSFile(path=f"/books/Harbor/{number}.mp3", size=100, format="mp3", playback_index=index)
        for number, index in zip((1, 2), indices, strict=True)
    ]
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source=f"{number}.mp3",
                name=f"{number}.mp3",
                sha256="b" * 64,
                identity={"size": 100},
            )
            for number in (1, 2)
        ],
    )
    entry = SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata={
            "medium": "audio",
            "title": "Harbor",
            "authors": [],
            "narrators": [],
            "recording_year": None,
            "audio_order": [file.path for file in files],
        },
    )
    item = ABSItem(
        id="item",
        library_id="library",
        title="Harbor",
        authors=[],
        narrators=[],
        audio=files,
        library_files=files,
        full_audio=True,
        path="/books/Harbor",
    )
    with pytest.raises(PublicationError, match="playback order"):
        matches(entry, item)
    for index, file in enumerate(item.audio, start=1):
        file.playback_index = index
    # API list order itself is not authoritative; explicit playback indices are.
    item.audio.reverse()
    assert matches(entry, item)


def test_regional_language_and_series_index_confirm():
    file = ABSFile(path="/books/Harbor/story.epub", size=100, format="epub")
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source="story.epub",
                name="story.epub",
                sha256="b" * 64,
                identity={"size": 100},
            )
        ],
    )
    entry = SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata={
            "medium": "ebook",
            "title": "Harbor",
            "authors": ["Alex Morgan"],
            "narrators": [],
            "edition_year": 2024,
            "language": "en-US",
            "series": "Harbor",
            "sequence": "01",
        },
    )
    item = ABSItem(
        id="item",
        library_id="library",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        language="en",
        year=2024,
        ebook=[file],
        library_files=[file],
        full_ebook=True,
        path="/books/Harbor",
        series=[{"name": "Harbor", "sequence": "1"}],
        cover_path="grimmory:item",
    )
    assert matches(entry, item)
    item.language = "fr"
    with pytest.raises(PublicationError, match="Grimmory language"):
        matches(entry, item)


async def test_confirmation_writes_catalog_metadata_before_comparing():
    file = ABSFile(path="/books/Harbor/story.epub", size=100, format="epub")
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source="story.epub",
                name="story.epub",
                sha256="b" * 64,
                identity={"size": 100},
            )
        ],
    )
    entry = SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata={
            "medium": "ebook",
            "title": "Harbor",
            "authors": ["Alex Morgan"],
            "narrators": [],
            "edition_year": 2024,
            "language": "en",
        },
    )
    embedded = ABSItem(
        id="1",
        library_id="7",
        title="Embedded title",
        authors=["Someone Else"],
        narrators=[],
        ebook=[file],
        library_files=[file],
        full_ebook=True,
        path="/books/Harbor",
        cover_path="grimmory:1",
    )
    corrected = embedded.model_copy(
        update={"title": "Harbor", "authors": ["Alex Morgan"], "language": "en", "year": 2024}
    )

    class Library:
        page_size = 20
        applied = None

        async def page(self, library_id, page):
            assert library_id == "7" and page == 0
            return [{"id": "1"}], 1

        async def expanded(self, ids):
            assert ids == ["1"]
            return [embedded]

        async def item(self, item_id):
            assert item_id == "1"
            return corrected if self.applied else embedded

        async def metadata_persistence(self):
            return {"move_files": False, "write_formats": []}

        async def apply_catalog_metadata(self, book_id, facts, *, observed_year=None):
            assert book_id == "1"
            self.applied = facts
            self.observed_year = observed_year

    library = Library()
    found = await find_item(library, entry, "7")
    assert library.applied["title"] == "Harbor"
    assert found.title == "Harbor"


def test_a_skipped_grimmory_refresh_is_requested_again_while_the_book_is_absent():
    capabilities = {"scan_capable": True, "watcher_enabled": False}
    assert detection_needs_another_scan("grimmory", capabilities, published_now=False, found=False)
    assert not detection_needs_another_scan(
        "grimmory", capabilities, published_now=True, found=False
    )
    assert not detection_needs_another_scan(
        "grimmory",
        {**capabilities, "watcher_enabled": True},
        published_now=False,
        found=False,
    )
    assert not detection_needs_another_scan(
        "grimmory", capabilities, published_now=False, found=True
    )
    assert not detection_needs_another_scan(
        "audiobookshelf", capabilities, published_now=False, found=False
    )


async def test_grimmory_file_move_after_metadata_update_still_confirms():
    file = ABSFile(path="/books/Harbor/story.epub", size=100, format="epub")
    moved_file = ABSFile(path="/books/Author/Harbor/story.epub", size=180, format="epub")
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source="story.epub",
                name="story.epub",
                sha256="b" * 64,
                identity={"size": 100},
            )
        ],
    )
    entry = SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata={
            "medium": "ebook",
            "title": "Harbor",
            "authors": ["Alex Morgan"],
            "narrators": [],
            "edition_year": 2024,
            "language": "en",
        },
    )
    embedded = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        language="en",
        year=2024,
        ebook=[file],
        library_files=[file],
        full_ebook=True,
        path="/books/Harbor",
        cover_path="grimmory:1",
    )
    moved = embedded.model_copy(
        update={
            "path": "/books/Author/Harbor",
            "ebook": [moved_file],
            "library_files": [moved_file],
        }
    )

    class Library:
        page_size = 20
        applied = False

        async def page(self, library_id, page):
            return [{"id": "1"}], 1

        async def expanded(self, ids):
            return [embedded]

        async def item(self, item_id):
            return moved if self.applied else embedded

        async def metadata_persistence(self):
            return {"move_files": False, "write_formats": []}

        async def apply_catalog_metadata(self, book_id, facts, *, observed_year=None):
            self.applied = True

    found = await find_item(Library(), entry, "7")
    assert found.path == "/books/Author/Harbor"
    assert found.ebook[0].size == 180


def test_a_comma_inside_a_grimmory_narrator_name_still_confirms():
    file = ABSFile(path="/books/Harbor/story.m4b", size=100, format="m4b")
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source="story.m4b",
                name="story.m4b",
                sha256="b" * 64,
                identity={"size": 100},
            )
        ],
    )
    entry = SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata={
            "medium": "audio",
            "title": "Harbor",
            "authors": [],
            "narrators": ["van Helsing, Abraham"],
            "recording_year": None,
        },
    )
    item = ABSItem(
        id="item",
        library_id="library",
        title="Harbor",
        authors=[],
        narrators=["van Helsing", "Abraham"],
        audio=[file],
        library_files=[file],
        full_audio=True,
        path="/books/Harbor",
        cover_path="grimmory:item",
    )
    assert matches(entry, item)
    item.narrators = ["Someone Else"]
    with pytest.raises(PublicationError, match="narrators"):
        matches(entry, item)
    item.narrators = ["van Helsing", "Abraham"]
    item.cover_path = None
    with pytest.raises(PublicationError, match="narrators"):
        matches(entry, item)


async def test_moved_grimmory_tracks_confirm_with_grimmory_playback_order():
    published = [
        ABSFile(path=f"/books/Harbor/{name}", size=100, format="mp3", playback_index=index)
        for index, name in enumerate(("02.mp3", "01.mp3"), start=1)
    ]
    moved = [
        ABSFile(
            path=f"/books/Author/Harbor/{name}",
            size=180,
            format="mp3",
            playback_index=index,
        )
        for index, name in enumerate(("01.mp3", "02.mp3"), start=1)
    ]
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source=name,
                name=name,
                sha256="b" * 64,
                identity={"size": 100},
            )
            for name in ("01.mp3", "02.mp3")
        ],
    )
    entry = SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata={
            "medium": "audio",
            "title": "Harbor",
            "authors": [],
            "narrators": ["Smith, John"],
            "recording_year": None,
            "audio_order": [
                "/books/Harbor/01.mp3",
                "/books/Harbor/02.mp3",
            ],
        },
    )
    embedded = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=[],
        narrators=["Smith, John"],
        audio=published,
        library_files=published,
        full_audio=True,
        path="/books/Harbor",
        cover_path="grimmory:1",
    )
    relocated = embedded.model_copy(
        update={
            "path": "/books/Author/Harbor",
            "audio": moved,
            "library_files": moved,
            "narrators": ["Smith", "John"],
        }
    )

    class Library:
        page_size = 20
        applied = False

        async def page(self, library_id, page):
            return [{"id": "1"}], 1

        async def expanded(self, ids):
            return [embedded]

        async def item(self, item_id):
            return relocated if self.applied else embedded

        async def metadata_persistence(self):
            return {"move_files": False, "write_formats": []}

        async def apply_catalog_metadata(self, book_id, facts, *, observed_year=None):
            self.applied = True

    found = await find_item(Library(), entry, "7")
    assert [file.path for file in found.audio] == [
        "/books/Author/Harbor/01.mp3",
        "/books/Author/Harbor/02.mp3",
    ]
    assert found.narrators == ["Smith", "John"]


def _audio_entry(names, *, audio_order=None):
    spec = PublicationSpec(
        entry_id=uuid4(),
        plan_revision="a" * 64,
        source_root="/downloads",
        source_relative="pack",
        source_directory={},
        destination_root="/library",
        staging_root="/staging",
        folder="Harbor",
        files=[
            PublishFile(
                source=name,
                name=name,
                sha256="b" * 64,
                identity={"size": 100},
            )
            for name in names
        ],
    )
    metadata = {
        "medium": "audio",
        "title": "Harbor",
        "authors": [],
        "narrators": [],
        "recording_year": None,
    }
    if audio_order:
        metadata["audio_order"] = audio_order
    return SimpleNamespace(
        specification=spec.model_dump(mode="json"),
        configuration={"destination": {"backend_path": "/books"}},
        expected_metadata=metadata,
    )


def test_grimmory_recovery_confirms_moved_tracks_without_inode_or_mtime():
    from app.domain.recovery_publication import match_files
    from app.domain.recovery_scans import ScanHeld

    names = ("01.mp3", "02.mp3")
    entry = _audio_entry(names, audio_order=[f"/books/Harbor/{name}" for name in names])
    moved = [
        ABSFile(
            path=f"/books/Author/Harbor/{name}",
            size=100,
            format="mp3",
            playback_index=index,
            size_unit="byte",
        )
        for index, name in enumerate(("02.mp3", "01.mp3"), start=1)
    ]
    item = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=[],
        narrators=[],
        audio=moved,
        library_files=moved,
        full_audio=True,
        path="/books/Author/Harbor",
        cover_path="grimmory:1",
    )
    evidence = {
        "media_identities": {
            name: {"size": 100, "inode": 4, "mtime_ns": 1_000_000} for name in names
        }
    }
    match_files(entry, item, evidence)
    evidence["media_identities"]["02.mp3"]["size"] = 50
    with pytest.raises(ScanHeld, match="identities"):
        match_files(entry, item, evidence)


def _finding(item, *, library_id="7", medium="audio"):
    return SimpleNamespace(
        evidence={
            "external_library_id": library_id,
            "medium": medium,
            "item": item.model_dump(mode="json"),
        }
    )


def test_grimmory_recovery_confirms_a_file_renamed_onto_the_library_pattern():
    from app.domain.recovery_publication import match_files, publication_candidates
    from app.domain.recovery_scans import ScanHeld

    entry = _audio_entry(("story.m4b",))
    entry.specification["files"][0]["identity"]["size"] = 1500
    entry.expected_metadata["authors"] = ["Alex Morgan"]
    entry.expected_metadata["recording_year"] = 2024
    # A single Grimmory book file is stored in whole kilobytes, and the default
    # upload pattern renames it to the author, series, and title.
    renamed = ABSFile(
        path="/books/Alex Morgan/Harbor/Harbor - Alex Morgan (2024).m4b",
        size=1024,
        format="m4b",
        size_unit="kilobyte",
    )
    item = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        year=2024,
        audio=[renamed],
        library_files=[renamed],
        full_audio=True,
        path="/books/Alex Morgan/Harbor",
        cover_path="grimmory:1",
    )
    evidence = {"media_identities": {"story.m4b": {"size": 1500, "inode": 4, "mtime_ns": 1}}}
    match_files(entry, item, evidence)

    ebook_entry = _audio_entry(("story.epub",))
    ebook_entry.specification["files"][0]["identity"]["size"] = 12 * 1024 + 500
    ebook_entry.expected_metadata = {
        "medium": "ebook",
        "title": "Harbor",
        "authors": ["Alex Morgan"],
        "narrators": [],
        "edition_year": 2024,
    }
    ebook = ABSFile(
        path="/books/Alex Morgan/Harbor/01 - Harbor/Harbor - Alex Morgan (2024).epub",
        size=12 * 1024,
        format="epub",
        size_unit="kilobyte",
    )
    ebook_item = ABSItem(
        id="2",
        library_id="7",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        year=2024,
        ebook=[ebook],
        library_files=[ebook],
        full_ebook=True,
        path="/books/Alex Morgan/Harbor/01 - Harbor",
        cover_path="grimmory:2",
    )
    match_files(
        ebook_entry,
        ebook_item,
        {"media_identities": {"story.epub": {"size": 12 * 1024 + 500}}},
    )

    other = item.model_copy(update={"id": "other", "title": "Other Harbor"})
    smaller = item.model_copy(deep=True, update={"id": "smaller"})
    smaller.library_files[0].size = 50 * 1024
    smaller.audio[0].size = 50 * 1024
    chosen = publication_candidates(
        entry,
        [_finding(item), _finding(other), _finding(smaller)],
        "7",
        "/books/Harbor",
        grimmory=True,
    )
    assert [row.evidence["item"]["id"] for row in chosen] == ["1"]

    duplicate = item.model_copy(update={"id": "duplicate"})
    assert (
        len(
            publication_candidates(
                entry,
                [_finding(item), _finding(duplicate)],
                "7",
                "/books/Harbor",
                grimmory=True,
            )
        )
        == 2
    )

    placed_file = ABSFile(
        path="/books/Harbor/story.m4b", size=1024, format="m4b", size_unit="kilobyte"
    )
    placed = item.model_copy(
        update={
            "id": "placed",
            "path": "/books/Harbor",
            "audio": [placed_file],
            "library_files": [placed_file],
        }
    )
    assert [
        row.evidence["item"]["id"]
        for row in publication_candidates(
            entry,
            [_finding(item), _finding(placed)],
            "7",
            "/books/Harbor",
            grimmory=True,
        )
    ] == ["placed"]

    plain = item.model_copy(update={"cover_path": None})
    with pytest.raises(ScanHeld, match="outside"):
        match_files(entry, plain, evidence)


def test_audiobookshelf_recovery_still_requires_inode_and_mtime():
    from app.domain.recovery_publication import match_files
    from app.domain.recovery_scans import ScanHeld

    entry = _audio_entry(("story.m4b",))
    file = ABSFile(path="/books/Harbor/story.m4b", size=100, format="m4b", size_unit="byte")
    item = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=[],
        narrators=[],
        audio=[file],
        library_files=[file],
        full_audio=True,
        path="/books/Harbor",
    )
    evidence = {"media_identities": {"story.m4b": {"size": 100, "inode": 4, "mtime_ns": 1_000_000}}}
    with pytest.raises(ScanHeld, match="identities"):
        match_files(entry, item, evidence)
    file.inode = "4"
    file.modified = 1
    match_files(entry, item, evidence)


def test_grimmory_recovery_confirms_a_book_after_the_folder_is_gone():
    from app.domain.recovery_publication import match_files, publication_candidates
    from app.domain.recovery_scans import ScanHeld

    entry = _audio_entry(("story.epub",))
    entry.specification["files"][0]["identity"]["size"] = 12 * 1024 + 400
    entry.expected_metadata = {
        "medium": "ebook",
        "title": "Harbor",
        "authors": ["Alex Morgan"],
        "narrators": [],
        "edition_year": 2024,
    }
    renamed = ABSFile(
        path="/books/Alex Morgan/Harbor/Harbor - Alex Morgan (2024).epub",
        size=12 * 1024,
        format="epub",
        size_unit="kilobyte",
    )
    item = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        year=2024,
        ebook=[renamed],
        library_files=[renamed],
        full_ebook=True,
        path="/books/Alex Morgan/Harbor",
        cover_path="grimmory:1",
    )
    match_files(entry, item, {"relocated": True})
    chosen = publication_candidates(
        entry,
        [_finding(item, medium="ebook")],
        "7",
        "/books/Harbor",
        grimmory=True,
        relocated=True,
    )
    assert [row.evidence["item"]["id"] for row in chosen] == ["1"]

    other = item.model_copy(update={"id": "other", "title": "Other Harbor"})
    rewritten = renamed.model_copy(update={"size": 80 * 1024})
    rewritten_item = item.model_copy(update={"ebook": [rewritten], "library_files": [rewritten]})
    with pytest.raises(ScanHeld, match="no longer in their folder"):
        match_files(entry, rewritten_item, {"relocated": True})
    assert [
        row.evidence["item"]["id"]
        for row in publication_candidates(
            entry,
            [
                _finding(item, medium="ebook"),
                _finding(other, medium="ebook"),
                _finding(rewritten_item, medium="ebook"),
            ],
            "7",
            "/books/Harbor",
            grimmory=True,
            relocated=True,
        )
    ] == ["1"]
    duplicate = item.model_copy(update={"id": "duplicate"})
    assert (
        len(
            publication_candidates(
                entry,
                [_finding(item, medium="ebook"), _finding(duplicate, medium="ebook")],
                "7",
                "/books/Harbor",
                grimmory=True,
                relocated=True,
            )
        )
        == 2
    )


def test_grimmory_recovery_accepts_an_in_place_metadata_rewrite():
    from app.domain.recovery_publication import match_files

    entry = _audio_entry(("story.m4b",))
    entry.specification["files"][0]["identity"]["size"] = 1500
    entry.expected_metadata["authors"] = ["Alex Morgan"]
    entry.expected_metadata["recording_year"] = 2024
    rewritten = ABSFile(
        path="/books/Harbor/story.m4b",
        size=80 * 1024,
        format="m4b",
        size_unit="kilobyte",
    )
    item = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        year=2024,
        audio=[rewritten],
        library_files=[rewritten],
        full_audio=True,
        path="/books/Harbor",
        cover_path="grimmory:1",
    )
    match_files(
        entry,
        item,
        {"media_identities": {"story.m4b": {"size": 80 * 1024 + 200}}},
    )
    plain = item.model_copy(update={"cover_path": None})
    with pytest.raises(PublicationError, match="boundaries"):
        match_files(
            entry,
            plain,
            {"media_identities": {"story.m4b": {"size": 80 * 1024 + 200}}},
        )


def test_grimmory_confirmation_keeps_a_renamed_ebook_file():
    entry = _audio_entry(("story.epub",))
    entry.expected_metadata = {
        "medium": "ebook",
        "title": "Harbor",
        "authors": ["Alex Morgan"],
        "narrators": [],
        "ebook_media_paths": ["/books/Harbor/story.epub"],
    }
    renamed = ABSFile(
        path="/books/Alex Morgan/Harbor/Harbor - Alex Morgan (2024).epub",
        size=12 * 1024,
        format="epub",
        size_unit="kilobyte",
    )
    item = ABSItem(
        id="1",
        library_id="7",
        title="Harbor",
        authors=["Alex Morgan"],
        narrators=[],
        ebook=[renamed],
        library_files=[renamed],
        full_ebook=True,
        path="/books/Alex Morgan/Harbor",
        cover_path="grimmory:1",
    )
    recorded = verified_ebook_files(entry, item, grimmory=True)
    assert recorded[0]["path"] == renamed.path
    assert recorded[0]["import_verified"] is True
    assert verified_ebook_files(entry, item, grimmory=False) == []
