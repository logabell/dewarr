import json
from uuid import uuid4

import pytest
from defusedxml.ElementTree import fromstring

from app.importing.metadata import ExportMetadata, initial_sidecars, valid_isbn
from app.importing.naming import NamingMetadata


def test_export_preserves_unicode_and_escapes_untrusted_metadata():
    value = ExportMetadata(
        medium="audio",
        naming=NamingMetadata(
            title='海 & Harbor <title> "one"',
            authors=["A & B", "A & B"],
            narrators=["Reader <One>"],
            series="Series & Stories",
            sequence="1.5",
            recording_year=2024,
            original_year=1999,
            publisher="Example <Press>",
            isbn="978-0-306-40615-7",
            asin="B012345678",
            language="en",
        ),
        description="A <script>fictional description</script> & text",
        genres=["Fantasy"],
    )
    files = initial_sidecars(value)
    assert list(files) == ["metadata.opf"]
    assert files == initial_sidecars(value)
    xml = fromstring(files["metadata.opf"])
    dc = "{http://purl.org/dc/elements/1.1/}"
    assert xml.find(f".//{dc}title").text == value.naming.title
    assert [node.text for node in xml.findall(f".//{dc}creator")] == ["A & B", "Reader <One>"]
    assert xml.find(f".//{dc}date").text == "2024"
    assert not xml.findall(".//script")


def test_export_omits_unknown_dates_fake_identifiers_and_unsupported_version_fields():
    value = ExportMetadata(
        medium="ebook",
        naming=NamingMetadata(
            title="A book",
            original_year=1999,
            recording_year=2024,
            source_posted_year=2026,
            narrators=["Not an ebook narrator"],
            isbn="9780306406158",
            asin="internal-version-id",
            edition="Revised",
            abridged=True,
        ),
    )
    text = initial_sidecars(value)["metadata.opf"]
    assert all(
        word not in text
        for word in ("1999", "2024", "2026", "identifier", "nrt", "Revised", "abridged")
    )


@pytest.mark.parametrize("value", ["Bad\x00title", "Bad\ud800title", "Bad\ufffftitle"])
def test_invalid_xml_characters_are_held(value):
    with pytest.raises(ValueError, match="XML-incompatible|unicode"):
        ExportMetadata(medium="ebook", naming=NamingMetadata(title=value))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0-306-40615-2", "0306406152"),
        ("0-8044-2957-X", "080442957X"),
        ("9780306406157", "9780306406157"),
        ("1234567890", None),
    ],
)
def test_export_only_accepts_valid_isbn_checksum(value, expected):
    assert valid_isbn(value) == expected


def test_grimmory_sidecar_matches_the_published_media_stem(tmp_path):
    from app.importing.metadata import grimmory_sidecars
    from app.importing.naming import fingerprint
    from app.importing.publication import PublicationSpec, PublishFile

    facts = {
        "title": "The First Harbor",
        "authors": ["Alex Morgan"],
        "edition_year": 2024,
        "isbn": "9780306406157",
        "asin": "B012345678",
        "series": "Harbor",
        "sequence": "1",
        "language": "en",
    }
    files = grimmory_sidecars(facts, "ebook", ["The First Harbor.epub", ".hidden.epub"])
    assert list(files) == ["The First Harbor.metadata.json"]
    assert files == grimmory_sidecars(facts, "ebook", ["The First Harbor.epub", ".hidden.epub"])
    document = json.loads(files["The First Harbor.metadata.json"])
    assert document["version"] == "1.0"
    assert document["generatedBy"] == "grimmory"
    assert document["metadata"]["isbn13"] == "9780306406157"
    assert document["metadata"]["identifiers"] == {"asin": "B012345678"}
    assert document["metadata"]["series"] == {"name": "Harbor", "number": 1.0}
    assert "publishedDate" not in document["metadata"]
    source, library, staging = (
        tmp_path.resolve() / name for name in ("downloads", "library", "staging")
    )
    for path in (source, library, staging):
        path.mkdir()
    PublicationSpec(
        entry_id=uuid4(),
        plan_revision=fingerprint({"test": True}),
        source_root=source,
        source_relative="pack",
        source_directory={"device": 1, "inode": 1},
        destination_root=library,
        staging_root=staging,
        folder="Harbor",
        files=[
            PublishFile(
                source="book.epub",
                name="The First Harbor.epub",
                sha256="a" * 64,
                identity={"device": 1, "inode": 2, "size": 3},
            )
        ],
        sidecars=files,
    )
    with pytest.raises(ValueError, match="sidecars"):
        PublicationSpec(
            entry_id=uuid4(),
            plan_revision=fingerprint({"test": True}),
            source_root=source,
            source_relative="pack",
            source_directory={"device": 1, "inode": 1},
            destination_root=library,
            staging_root=staging,
            folder="Harbor",
            files=[
                PublishFile(
                    source="book.epub",
                    name="The First Harbor.epub",
                    sha256="a" * 64,
                    identity={"device": 1, "inode": 2, "size": 3},
                )
            ],
            sidecars={"metadata.json": "{}"},
        )


def test_a_part_keeps_its_label_in_the_title_so_sync_groups_it_under_the_book():
    from app.importing.metadata import grimmory_sidecars

    facts = NamingMetadata(title="Dark Age", authors=["Pierce Brown"], part_index=2, part_total=3)
    opf = fromstring(initial_sidecars(ExportMetadata(medium="audio", naming=facts))["metadata.opf"])
    title = opf.find(".//{http://purl.org/dc/elements/1.1/}title").text
    assert title == "Dark Age (Part 2 of 3)"
    document = grimmory_sidecars(facts.model_dump(), "audio", ["Dark Age.m4b"])
    assert json.loads(document["Dark Age.metadata.json"])["metadata"]["title"] == title
