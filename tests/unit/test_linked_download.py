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
