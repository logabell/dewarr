"""Reported indexer layouts must preserve the full catalog-backed identity."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.adapters.prowlarr import ProwlarrRelease
from app.domain.release_profiles import (
    catalog_language_release,
    co_author_credits,
    indexer_title_authors,
)
from app.domain.series_identity import series_note_agrees


def authors(title, book="The Drawing of the Three", position="2", **changes):
    release = SimpleNamespace(source="prowlarr", title=title, raw_title=title, authors=[])
    release.__dict__.update(changes)
    work = {
        "title": book,
        "authors": ["Stephen King"],
        "series": [{"name": "The Dark Tower", "position": position}],
    }
    return indexer_title_authors(release, work)


def test_reported_release_names_corroborate_exact_books():
    cases = [
        ("Wolves of the Calla: Dark Tower V - Stephen King", "Wolves of the Calla", "5"),
        ("Song of Susannah: The Dark Tower VI - Stephen King", "Song of Susannah", "6"),
        ("Dark Tower II: The Drawing of the Three - Stephen King", "The Drawing of the Three", "2"),
        ("Dark Tower III: The Waste Lands - Stephen King", "The Waste Lands", "3"),
        (
            "Stephen King - The Dark Tower, Bk 2 The Drawing Of The Three mp3",
            "The Drawing of the Three",
            "2",
        ),
        ("Stephen King - The Dark Tower, Bk 3 The Waste Lands mp3", "The Waste Lands", "3"),
        (
            "Stephen.King.The.Wind.Through.The.Keyhole.2012.RETAiL.ePub.eBook-LiBRiCiDE",
            "The Wind through the Keyhole",
            None,
        ),
    ]
    actual = [(title, authors(title, book, position)) for title, book, position in cases]
    assert actual == [(title, ["Stephen King"]) for title, _, _ in cases]


@pytest.mark.parametrize(
    "note,position,accepted",
    [
        ("Dark Tower V", "5", True),
        ("The Dark Tower, Book vi", "6", True),
        ("Dark Tower, Bk. IV", "4", True),
        ("Dark Tower #2.5", "2.5", True),
        ("Dark Tower V", "6", False),
        ("Dark Tower V", "5.5", False),
        ("Dark Tower V-VI", "5", False),
        ("Dark Tower V & VI", "5", False),
        ("Dark Tower V/VI", "5", False),
        ("Dark Tower V 5", "5", False),
        ("Dark Tower IIV", "3", False),
        ("Dark Tower IIII", "4", False),
        ("Dark Tower VX", "5", False),
        ("Dark Tower V.5", "5.5", False),
        ("Dark Tower V excerpt", "5", False),
        ("Dark Tower", "5", False),
        ("Other Tower V", "5", False),
    ],
)
def test_series_position_notes_are_exact_and_unambiguous(note, position, accepted):
    assert (
        series_note_agrees(
            note, [{"name": "The Dark Tower", "position": position}], require_position=True
        )
        is accepted
    )


@pytest.mark.parametrize("source", ["audiobookbay", "prowlarr"])
@pytest.mark.parametrize(
    "title,accepted",
    [
        ("Dark Tower II: The Drawing of the Three - Stephen King", True),
        ("Dark Tower 2 - The Drawing of the Three - Stephen King", True),
        ("Stephen King - The Dark Tower, Bk 2 The Drawing Of The Three mp3", True),
        ("Dark Tower III: The Drawing of the Three - Stephen King", False),
        ("Dark Tower II-III: The Drawing of the Three - Stephen King", False),
        ("Dark Tower II & III: The Drawing of the Three - Stephen King", False),
        ("Dark Tower 2.5: The Drawing of the Three - Stephen King", False),
        ("Other Tower II: The Drawing of the Three - Stephen King", False),
        ("Dark Tower II: The Drawing of the Three - Other Writer", False),
        ("Dark Tower II: The Drawing of the Three: A Study Guide - Stephen King", False),
        ("Dark Tower II: The Drawing of the Three and The Waste Lands - Stephen King", False),
        ("Stephen King - The Dark Tower, Bk 3 The Drawing Of The Three mp3", False),
        ("Stephen King - The Dark Tower, Bk 2 Collection The Drawing Of The Three mp3", False),
        ("Stephen King - The Dark Tower, Bk 2 The Drawing mp3", False),
    ],
)
def test_series_prefixed_releases_preserve_catalog_constraints(source, title, accepted):
    assert authors(title, source=source) == (["Stephen King"] if accepted else [])


@pytest.mark.parametrize(
    "suffix,accepted",
    [
        (".2012.RETAiL.ePub.eBook-LiBRiCiDE", True),
        (".2012.retail.EPUB-LiBRiCiDE", True),
        (".ePub.eBook-LIBRICIDE", True),
        (".mobi.eBook-Group", True),
        (".ePub.eBook-Group42", True),
        (".ePub.eBook-Sampler01", True),
        (".ePub.eBook-sample", False),
        (".ePub.eBook-PART01", False),
        (".ePub.eBook-preview", False),
        (".ePub.eBook-Collection", False),
        (".ePub.eBook-Summary", False),
        (".ePub.eBook-Group.part01.rar", False),
        (".ePub.eBook-Unknown-words", False),
        (".and.The.Gunslinger.ePub.eBook-Group", False),
    ],
)
def test_ebook_packaging_does_not_erase_content_warnings(suffix, accepted):
    title = "Stephen.King.The.Wind.Through.The.Keyhole" + suffix
    assert authors(title, "The Wind through the Keyhole", None) == (
        ["Stephen King"] if accepted else []
    )


@pytest.mark.parametrize(
    "warning",
    [
        "Sample01",
        "Excerpt1",
        "Preview02",
        "Incomplete3",
        "Truncated4",
        "Summary5",
        "Collection6",
        "Pack7",
        "Omnibus8",
        "Anthology9",
        "Boxset10",
        "Part11",
        "CD12",
        "Disc13",
    ],
)
def test_numbered_content_warnings_are_not_release_groups(warning):
    title = "Stephen.King.The.Wind.Through.The.Keyhole.ePub.eBook-" + warning
    assert authors(title, "The Wind through the Keyhole", None) == []


def test_supported_layout_still_requires_structured_author_agreement_and_catalog_series():
    title = "Dark Tower II: The Drawing of the Three - Stephen King"
    assert authors(title, authors=["Other Writer"]) == []
    assert authors(title, position=None) == []
    assert authors(title) == ["Stephen King"]


CO_WRITTEN = {"title": "The Long Earth", "authors": ["Terry Pratchett", "Stephen Baxter"]}


def co_written(title):
    release = SimpleNamespace(source="prowlarr", title=title, raw_title=title, authors=[])
    return indexer_title_authors(release, CO_WRITTEN)


def test_every_co_author_together_is_one_credit():
    credits = co_author_credits(CO_WRITTEN["authors"])
    assert "terry pratchett stephen baxter" in credits
    assert "stephen baxter and terry pratchett" in credits
    assert co_author_credits(["Terry Pratchett"]) == set()


@pytest.mark.parametrize(
    "title",
    [
        "Terry Pratchett  Stephen Baxter - The Long Earth 2012 MP3",
        "Terry Pratchett & Stephen Baxter - The Long Earth",
        "Stephen Baxter and Terry Pratchett - The Long Earth",
        "The Long Earth - Terry Pratchett & Stephen Baxter",
    ],
)
def test_release_names_crediting_every_co_author_match(title):
    assert co_written(title) == ["Stephen Baxter", "Terry Pratchett"]


@pytest.mark.parametrize(
    "title",
    [
        "Terry Pratchett Neil Gaiman - The Long Earth",
        "Terry Pratchett & Stephen Baxter - The Long Mars",
    ],
)
def test_another_co_author_or_book_does_not_match(title):
    assert co_written(title) == []


def test_language_after_every_co_author_and_title_is_inferred():
    title = "Terry Pratchett & Stephen Baxter - The Long Earth German Hoerbuch"
    source = ProwlarrRelease(
        source_id="co-author-fixture",
        acquisition_supported=True,
        title=title,
        raw_title=title,
        medium="audio",
        protocol="nzb",
        indexer_name="Fixture",
        categories=[3030],
        observed_at=datetime.now(UTC),
    )
    assert catalog_language_release(source, CO_WRITTEN).language == "de"
