"""Boundaries for connector spelling without weakening title/author identity."""

import pytest

from app.adapters.catalog_types import BookData
from app.domain.hardcover_matching import MatchEvidence, compatible
from app.domain.title_matching import compatible_title, title_search_variants


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Angels and Demons", True),
        ("Angels & Demons", True),
        ("Angels ＆ Demons (Unabridged)", True),
        ("Angels Demons", False),
        ("Angels and Demons 2", False),
        ("Angels and Demons: A Study Guide", False),
        ("Angels and Demons: Volume 2", False),
        ("Angels or Demons", False),
    ],
)
def test_connector_variant_preserves_content_bearing_words(title, expected):
    assert compatible_title(title, "Angels & Demons") is expected


def test_connector_does_not_expand_embedded_initials_or_change_display_identity():
    assert not compatible_title("AT&T", "AT and T")
    assert title_search_variants("AT&T") == {"at&t"}
    assert title_search_variants("Angels and Demons (Unabridged)") == {
        "angels and demons",
        "angels & demons",
    }


@pytest.mark.parametrize(
    "authors,expected", [(["Dan Brown"], True), (["Other Writer"], False), ([], False)]
)
def test_hardcover_connector_match_still_requires_author_evidence(authors, expected):
    book = BookData(
        provider="hardcover", external_id="research", title="Angels & Demons", authors=["Dan Brown"]
    )
    assert compatible(MatchEvidence(title="Angels and Demons", authors=authors), book) is expected
