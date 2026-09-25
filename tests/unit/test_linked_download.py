from types import SimpleNamespace

import pytest

from app.importing.linked_download import agrees_with_request
from app.importing.match_evidence import MatchEvidence


@pytest.mark.parametrize(
    "facts", [MatchEvidence(), MatchEvidence(titles=["project hail mary"], authors=[["andy weir"]])]
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
        MatchEvidence(identifiers=[{"namespace": "asin", "value": "B012345678"}]),
    ],
)
def test_linked_book_preserves_conflicts_and_identifier_matching(facts):
    work = SimpleNamespace(
        title="Project Hail Mary", authors=["Andy Weir"], language="en", metadata_fields={}
    )
    assert not agrees_with_request(work, {"title": work.title, "authors": work.authors}, facts)
