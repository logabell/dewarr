from types import SimpleNamespace

import pytest

from app.importing.linked_download import agrees_with_request
from app.importing.match_evidence import MatchEvidence


@pytest.mark.parametrize(
    "facts",
    [
        MatchEvidence(),
        MatchEvidence(titles=["project hail mary"], authors=[["andy weir"]]),
        MatchEvidence(identifiers=[{"namespace": "asin", "value": "B012345678"}]),
    ],
)
def test_linked_book_does_not_need_embedded_edition_identifier(facts):
    work = SimpleNamespace(
        title="Project Hail Mary", authors=["Andy Weir"], language="en", metadata_fields={}
    )
    assert agrees_with_request(work, {"title": work.title, "authors": work.authors}, facts)


@pytest.mark.parametrize(
    "facts",
    [
        MatchEvidence(titles=["another book"]),
        MatchEvidence(authors=[["another author"]]),
        MatchEvidence(languages=["fr"]),
        MatchEvidence(issues=["Files disagree about narrator"]),
    ],
)
def test_linked_book_preserves_conflicting_file_metadata(facts):
    work = SimpleNamespace(
        title="Project Hail Mary", authors=["Andy Weir"], language="en", metadata_fields={}
    )
    assert not agrees_with_request(work, {"title": work.title, "authors": work.authors}, facts)


@pytest.mark.parametrize(
    ("catalog_title", "release_title", "file_title", "expected"),
    [
        ("Atmosphere: A Love Story", "Atmosphere", "Atmosphere: A Love Story", True),
        ("Atmosphere: A Love Story", "Atmosphere", "Atmosphere", True),
        ("Atmosphere: A Love Story", "Atmosphere", "Atmosphere: Another Story", False),
        ("Atmosphere: A Love Story", "Atmosphere: Another Story", "Atmosphere", False),
        ("Atmosphere: A Love Story", "Atmosphere", "Atmosphere (1 of 2)", False),
        ("Atmosphere: Volume Two", "Atmosphere", "Atmosphere", False),
        ("Atmosphere: A Love Story", "Atmosphere", "Atmosphere: A Study Guide", False),
    ],
)
def test_linked_download_accepts_omitted_subtitle_but_not_conflicting_content(
    catalog_title, release_title, file_title, expected
):
    work = SimpleNamespace(
        title=catalog_title, authors=["Taylor Jenkins Reid"], language="en", metadata_fields={}
    )
    facts = MatchEvidence(titles=[file_title], authors=[["taylor jenkins reid"]])
    assert (
        agrees_with_request(work, {"title": release_title, "authors": work.authors}, facts)
        is expected
    )


def test_audiobookbay_credit_and_leading_file_title_can_link_the_download():
    work = SimpleNamespace(title="Lantern", authors=["Writer"], language="en", metadata_fields={})
    facts = MatchEvidence(
        titles=["Lantern: North Sea, Book 5"],
        authors=[["reader", "writer"]],
    )
    assert agrees_with_request(
        work,
        {"source": "audiobookbay", "title": "North Sea 5 - Writer, Reader", "authors": []},
        facts,
        series=[{"name": "North Sea", "position": "5"}],
    )
    assert agrees_with_request(
        work,
        {"source": "audiobookbay", "title": "Lantern - Writer, Reader", "authors": ["Writer"]},
        MatchEvidence(titles=["Lantern"], authors=[["writer"]]),
    )
    assert not agrees_with_request(
        work,
        {"title": "North Sea 5 - Writer, Reader", "authors": []},
        MatchEvidence(titles=["Other Book: North Sea, Book 5"], authors=[["reader", "writer"]]),
    )


@pytest.mark.parametrize(
    ("release_title", "file_title"),
    [
        ("Lantern - Writer, Reader", "Lantern: A Study Guide"),
        ("Lantern - Writer, Reader", "Lantern: Another Story"),
        ("Another Book - Writer, Reader", "Lantern"),
        ("North Sea 4 - Writer, Reader", "Lantern: North Sea, Book 5"),
    ],
)
def test_abb_link_keeps_conflicting_content_held(release_title, file_title):
    work = SimpleNamespace(title="Lantern", authors=["Writer"], language="en", metadata_fields={})
    assert not agrees_with_request(
        work,
        {"source": "audiobookbay", "title": release_title, "authors": []},
        MatchEvidence(titles=[file_title], authors=[["writer"]]),
        series=[{"name": "North Sea", "position": "5"}],
    )


@pytest.mark.parametrize(
    ("file_title", "file_authors", "expected"),
    [
        ("Lantern: A Light: North Sea, Book 5", ["co writer", "writer"], True),
        ("Lantern: A Light (The North Sea Trilogy)", ["writer"], True),
        ("Lantern: A Light: North Sea, Book 5.5", ["writer"], False),
        ("Lantern: A Light: Other Coast, Book 5", ["writer"], False),
        ("Lantern: A Different Light", ["writer"], False),
        ("Lantern: A Light: A Study Guide", ["writer"], False),
        ("Lantern: A Light", ["other writer"], False),
    ],
)
def test_abb_link_requires_known_annotation_after_the_full_title(
    file_title, file_authors, expected
):
    work = SimpleNamespace(
        title="Lantern: A Light", authors=["Writer"], language="en", metadata_fields={}
    )
    assert (
        agrees_with_request(
            work,
            {"source": "audiobookbay", "title": "North Sea 5 - Writer, Reader", "authors": []},
            MatchEvidence(titles=[file_title], authors=[file_authors]),
            series=[{"name": "North Sea", "position": "5"}],
        )
        is expected
    )


@pytest.mark.parametrize(
    "facts",
    [MatchEvidence(), MatchEvidence(titles=["Lantern"]), MatchEvidence(authors=[["writer"]])],
)
def test_new_credit_forms_need_independent_title_and_author_tags(facts):
    work = SimpleNamespace(title="Lantern", authors=["Writer"], language="en", metadata_fields={})
    assert not agrees_with_request(
        work,
        {"source": "audiobookbay", "title": "Lantern - Writer, Reader", "authors": ["Writer"]},
        facts,
    )
