from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.importing.grouping import ReviewedFile, ReviewedGroup, regroup
from app.importing.inspection import InspectedGroup, inspect_download
from app.importing.match_evidence import (
    MatchEvidence,
    catalog_identifiers,
    group_evidence,
    identifier,
    isbn_forms,
    isbn_key,
    language_key,
)
from app.importing.matching import candidate_evidence
from tests.media_fixtures import audio, epub, pdf


@pytest.mark.parametrize(
    "value",
    [
        "123456789X",
        "1-234-56789-X",
        "9781234567897",
        "urn:isbn:123456789X",
        "ISBN-13: 978-1-234-56789-7",
    ],
)
def test_equivalent_isbns_are_validated_and_normalized(value):
    assert isbn_key(value) == "9781234567897"
    assert isbn_forms(isbn_key(value)) == {"9781234567897", "123456789X"}


def test_identifiers_are_namespaced_and_invalid_values_do_not_gain_identity():
    assert identifier(None, "B012345678") is None
    assert identifier("ASIN", "b012345678") == ("asin", "B012345678")
    assert identifier(None, "urn:asin:B012345678") == ("asin", "B012345678")
    assert isbn_key("9781234567890") is None
    assert isbn_key("an ISBN mentioned in text 9781234567897") is None
    assert identifier("uuid", "123456789X") is None
    assert catalog_identifiers({"isbn_10": "123456789X", "isbn13": "9781234567897"}) == {
        ("isbn", "9781234567897")
    }
    assert language_key("eng") == language_key("EN")
    assert language_key("en-US") == language_key("en-GB") == "en"
    assert language_key("en-US") != language_key("de-DE")


def test_epub_identifier_assertions_are_preserved_and_never_read_from_filenames(tmp_path):
    root = tmp_path.resolve()
    epub(root / "pack/9780306406157.epub", isbn="urn:isbn:123456789X")
    snapshot = inspect_download(root, "pack")
    group = InspectedGroup(**snapshot["groups"][0])
    facts = group_evidence(snapshot, group)
    assert facts.model_dump()["identifiers"] == [{"namespace": "isbn", "value": "9781234567897"}]
    assert facts.titles == ["first harbor"] and not facts.issues
    assert snapshot["files"][0]["metadata"]["identifier_assertions"] == [
        {"scheme": None, "value": "urn:isbn:123456789X"}
    ]


def test_group_conflicts_and_companion_identifiers_are_distinct(tmp_path):
    root = tmp_path.resolve()
    audio(
        root / "pack/book.mp3",
        tags={"asin": "B012345678", "language": "eng", "date": "2024", "abridged": "false"},
    )
    pdf(root / "pack/notes.pdf", title="Other book", author="Other author")
    snapshot = inspect_download(root, "pack")
    group = regroup(
        snapshot,
        [
            ReviewedGroup(
                files=[
                    ReviewedFile(path="book.mp3"),
                    ReviewedFile(path="notes.pdf", role="supplement"),
                ]
            )
        ],
        [],
    ).groups[0]
    facts = group_evidence(snapshot, group)
    assert facts.model_dump()["identifiers"] == [{"namespace": "asin", "value": "B012345678"}]
    assert facts.titles == ["first harbor"]
    assert facts.narrators == [["jordan lee"]] and facts.languages == ["en"]
    assert facts.years == [2024] and facts.abridged == [False]
    assert not facts.issues


def test_multiple_narrator_credits_use_explicit_separator_not_person_name_commas(tmp_path):
    root = tmp_path.resolve()
    audio(root / "pack/book.mp3", narrator="Smith, Jane; Jordan Lee; JORDAN LEE")
    snapshot = inspect_download(root, "pack")
    facts = group_evidence(snapshot, InspectedGroup.model_validate(snapshot["groups"][0]))
    assert facts.narrators == [["jordan lee", "smith, jane"]]
    assert facts.authors == [["alex morgan"]]
    assert not facts.issues


def test_contradictory_ebook_formats_cannot_be_silently_matched(tmp_path):
    root = tmp_path.resolve()
    epub(root / "pack/book.epub", isbn="urn:isbn:123456789X")
    pdf(root / "pack/book.pdf", title="Other book")
    snapshot = inspect_download(root, "pack")
    group = regroup(
        snapshot,
        [
            ReviewedGroup(
                files=[ReviewedFile(path="book.epub"), ReviewedFile(path="book.pdf")],
                same_edition=True,
            )
        ],
        [],
    ).groups[0]
    facts = group_evidence(snapshot, group)
    assert "Files disagree about title" in facts.issues
    snapshot["files"][0]["metadata"]["identifier_assertions"].append(
        {"scheme": None, "value": "urn:isbn:invalid"}
    )
    assert "An embedded edition identifier is invalid" in group_evidence(snapshot, group).issues


def facts_and_version():
    work = SimpleNamespace(
        id=uuid4(), title="First Harbor", authors=["Alex Morgan"], language="en", metadata_fields={}
    )
    version = SimpleNamespace(
        id=uuid4(),
        work_id=work.id,
        title="First Harbor",
        medium="audio",
        language="eng",
        publication_year=2024,
        narrators=["Jordan Lee"],
        abridged=False,
        identifiers={"asin": "B012345678"},
    )
    facts = MatchEvidence(
        titles=["first harbor"],
        authors=[["alex morgan"]],
        narrators=[["jordan lee"]],
        languages=["en"],
        years=[2024],
        abridged=[False],
        identifiers=[{"namespace": "asin", "value": "B012345678"}],
    )
    return facts, version, work


@pytest.mark.parametrize(
    "changed",
    ["narrator", "language", "year", "abridged", "title", "author", "identifier", "pending"],
)
def test_identifier_does_not_override_contradictory_recording_evidence(changed):
    facts, version, work = facts_and_version()
    assert not candidate_evidence(facts, version, work, work, False).conflicts
    if changed == "narrator":
        version.narrators = ["Casey Reed"]
    elif changed == "language":
        version.language = "fr"
    elif changed == "year":
        version.publication_year = 2025
    elif changed == "abridged":
        version.abridged = True
    elif changed == "title":
        work.title = version.title = "Second Harbor"
    elif changed == "author":
        work.authors = ["Different Writer"]
    elif changed == "identifier":
        version.identifiers = {"asin": "B098765432"}
    candidate = candidate_evidence(facts, version, work, work, changed == "pending")
    assert candidate.conflicts


def test_edition_labels_are_not_a_different_title():
    from app.domain.catalog_titles import stripped_title, titles_agree

    facts, version, work = facts_and_version()
    facts.titles = ["first harbor (unabridged)"]
    assert stripped_title("First Harbor (Unabridged)") == "First Harbor"
    assert titles_agree(facts.titles, [work.title, version.title])
    candidate = candidate_evidence(facts, version, work, work, False)
    assert "Embedded title agrees" in candidate.reasons
    facts.titles = ["first harbor: the graphic novel"]
    assert (
        "Embedded title is missing or differs"
        in candidate_evidence(facts, version, work, work, False).conflicts
    )


def test_file_edition_survives_identifier_noise_and_a_language_name():
    from app.importing.file_editions import edition_blocker, edition_fields

    work = SimpleNamespace(title="Cloud Atlas", language="en")
    group = SimpleNamespace(
        title="Cloud Atlas (Unabridged)", medium="audio", narrators=["Scott Brick"]
    )
    facts = MatchEvidence(
        languages=["english"],
        identifiers=[
            {"namespace": "isbn", "value": "9780812994735"},
            {"namespace": "isbn", "value": "9780340822780"},
        ],
        issues=[
            "An embedded edition identifier is invalid",
            "Multiple edition identifiers require review",
        ],
    )
    assert edition_blocker(facts) is None
    fields = edition_fields(work, group, facts)
    assert fields["title"] == "Cloud Atlas"
    assert fields["language"] == "en"
    assert fields["abridged"] is False
    assert fields["identifiers"] == {}
    assert fields["narrators"] == ["Scott Brick"]
    facts.issues = ["Some files have not passed content inspection"]
    assert edition_blocker(facts) == "Some files have not passed content inspection"
    facts.issues = ["Files disagree about title"]
    assert edition_blocker(facts) == "Files disagree about title"


def test_unknown_narrator_and_missing_title_are_not_automatic_evidence():
    facts, version, work = facts_and_version()
    facts.narrators = []
    assert (
        "Narrator evidence is missing or differs"
        in candidate_evidence(facts, version, work, work, False).conflicts
    )
    facts.titles = []
    assert (
        "Embedded title is missing or differs"
        in candidate_evidence(facts, version, work, work, False).conflicts
    )
