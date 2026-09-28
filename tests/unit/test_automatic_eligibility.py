from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.adapters.mam import MAMRelease
from app.adapters.torrent_descriptor import TorrentDescriptor
from app.domain.automatic_eligibility import eligibility
from app.domain.release_profiles import ReleasePreferences

WORK = {"title": "Harbor", "authors": ["Writer"]}
RULE = {
    "medium": "audio",
    "language": "en",
    "version_id": None,
    "abridged": None,
    "standalone": False,
}


def release(**changes):
    return MAMRelease(
        source_id="501",
        title="Harbor",
        raw_title="Harbor",
        authors=["Writer"],
        medium="audio",
        language="en",
        formats=["m4b"],
        seeders=5,
        protocol="torrent",
        observed_at=datetime.now(UTC),
        **changes,
    )


def descriptor(paths):
    return TorrentDescriptor(
        name="Harbor",
        artifact_sha256="a" * 64,
        infohash_v1="b" * 40,
        private=True,
        content_bytes=12 * len(paths),
        torrent_bytes=100,
        padding_bytes=0,
        files=[{"index": i, "path": "Harbor/" + p, "size_bytes": 12} for i, p in enumerate(paths)],
        parser="fixture",
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"authors": ["Another writer"]}, "corroborate"),
        ({"medium": None}, "identify"),
        ({"language": None}, "language"),
        ({"language": "fr"}, "language"),
        ({"seeders": None}, "seeder"),
        ({"seeders": 0}, "seeder"),
        ({"raw_title": "Harbor sample"}, "partial"),
        ({"raw_title": "Harbor Books 1-3"}, "Collection"),
        ({"formats": ["aax"]}, "supported"),
    ],
)
def test_unknown_or_conflicting_source_facts_do_not_become_eligible(change, reason):
    value = release().model_copy(update=change)
    assert any(reason in r for r in eligibility(value, WORK, RULE, ReleasePreferences()))


@pytest.mark.parametrize(
    ("title", "authors", "allowed"),
    [
        (
            "Cory.Doctorow-Enshittification.Why.Everything.Suddenly.Got.Worse."
            "And.What.To.Do.About.It.2025.RETAIL.EPUB",
            [],
            True,
        ),
        (
            "Doctorow, Cory - Enshittification- Why Everything Suddenly Got Worse "
            "and What to Do About It (2025)",
            [],
            True,
        ),
        ("Cory Doctorow - Enshittification (2025) [EPUB]", [], True),
        ("Cory Doctorow - Enshittification.RETAIL.EPUB", [], True),
        ("Cory Doctorow - Enshittification (retail) (epub)", [], True),
        ("Cory Doctorow - Enshittification (summary) (epub)", [], False),
        ("Cory Doctorow - Enshittification (sample) (epub)", [], False),
        ("Cory Doctorow - Enshittification (retail) (epub)", ["Other Writer"], False),
        ("Other Writer - Enshittification.2025.RETAIL.EPUB", [], False),
        ("Doctorow Cory - Enshittification (2025)", [], False),
        ("Cory Doctorow - Enshittification.2025.RETAIL.EPUB", ["Other Writer"], False),
        ("Cory Doctorow - Enshittification summary.2025.RETAIL.EPUB", [], False),
        ("Cory Doctorow - Enshittification sample.2025.RETAIL.EPUB", [], False),
        ("Cory Doctorow - Enshittification and Another Book (2025)", [], False),
        ("Cory Doctorow - Enshittification.2025.RETAIL.part01.rar", [], False),
        ("Cory Doctorow - Enshittification.2025.RETAIL.par2", [], False),
    ],
)
def test_indexer_ebook_labels_preserve_complete_author_title_evidence(title, authors, allowed):
    from app.adapters.prowlarr import ProwlarrRelease

    value = ProwlarrRelease(
        source_id="fixture",
        title=title,
        raw_title=title,
        authors=authors,
        medium="ebook",
        language="en",
        protocol="nzb",
        indexer_name="Fixture",
        categories=[7020],
        observed_at=datetime.now(UTC),
        acquisition_supported=True,
    )
    work = {
        "title": "Enshittification: Why Everything Suddenly Got Worse and What to Do About It",
        "authors": ["Cory Doctorow"],
    }
    reasons = eligibility(value, work, {**RULE, "medium": "ebook"}, ReleasePreferences())
    assert (not reasons) is allowed, reasons


@pytest.mark.parametrize(
    ("title", "allowed"),
    [
        (
            "Robbins, Mel - The Let Them Theory- A Life Changing Tool That Millions "
            "of People Cant Stop Talking About",
            True,
        ),
        ("Mel Robbins - The Let Them Theory: A Life Changing Tool [EPUB]", True),
        ("Mel Robbins - The Let Them Theory — A Life Changing Tool", True),
        ("Other Writer - The Let Them Theory: A Life Changing Tool", False),
        ("Robbins Mel - The Let Them Theory: A Life Changing Tool", False),
        ("Mel Robbins - The Let Them Theory Workbook: A Life Changing Tool", False),
        ("Mel Robbins - The Let Them Theory- A Summary", False),
        ("Mel Robbins - The Let Them Theory: Workbook", False),
        ("Mel Robbins - The Let Them Theory: Volume 2", False),
        ("Mel Robbins - The Let Them Theory and Another Book", False),
        ("Mel Robbins - The Let Them Theory: A Life Changing Tool.part01.rar", False),
    ],
)
def test_indexer_descriptive_subtitle_with_exact_author(title, allowed):
    from app.adapters.prowlarr import ProwlarrRelease

    value = ProwlarrRelease(
        source_id="fixture",
        title=title,
        raw_title=title,
        medium="ebook",
        protocol="nzb",
        indexer_name="Fixture",
        categories=[7020],
        observed_at=datetime.now(UTC),
        acquisition_supported=True,
    )
    reasons = eligibility(
        value,
        {"title": "The Let Them Theory", "authors": ["Mel Robbins"]},
        {**RULE, "medium": "ebook", "language": None},
        ReleasePreferences(),
    )
    assert (not reasons) is allowed, reasons


@pytest.mark.parametrize(
    ("paths", "allowed"),
    [
        (["Harbor.m4b"], True),
        (["01.mp3", "02.mp3"], True),
        (["Harbor Chapter 01.mp3", "Harbor Chapter 02.mp3"], True),
        (["01 - Harbor.m4b", "02 - Roads.m4b"], False),
        (["Book 1/01.mp3", "Book 2/02.mp3"], False),
        (["Harbor.m4b", "Harbor.mp3"], False),
        (["Books 1-3.m4b"], False),
        (["Harbor.m4b", "archive.zip"], False),
        (["Harbor.m4b", "cover.jpg", "notes.pdf"], True),
    ],
)
def test_single_book_manifests_are_distinct_from_packs_and_alternative_encodings(paths, allowed):
    reasons = eligibility(release(), WORK, RULE, ReleasePreferences(), descriptor=descriptor(paths))
    assert (not reasons) is allowed, reasons


@pytest.mark.parametrize("format_label", ["MP3", "[MP3]", "(mp3)", ".mp3"])
def test_usenet_audio_title_labels_identify_the_catalog_book(format_label):
    from app.adapters.nzb_descriptor import inspect_nzb
    from app.adapters.prowlarr import ProwlarrRelease
    from app.importing.linked_download import agrees_with_request
    from app.importing.match_evidence import MatchEvidence
    from tests.nzb_fixture import nzb_bytes

    title = (
        "Annie Jacobsen - Operation Paperclip: The Secret Intelligence Program "
        f"(2014) {format_label}"
    )
    value = ProwlarrRelease(
        source_id="fixture",
        title=title,
        raw_title=title,
        medium="audio",
        language="en",
        protocol="nzb",
        indexer_name="Fixture",
        categories=[3030],
        observed_at=datetime.now(UTC),
        acquisition_supported=True,
    )
    work = {
        "title": "Operation Paperclip: The Secret Intelligence Program",
        "authors": ["Annie Jacobsen"],
    }
    assert value.formats == ["mp3"]
    assert not eligibility(
        value,
        work,
        RULE,
        ReleasePreferences(),
        unattended=True,
        descriptor=inspect_nzb(nzb_bytes(filename="encoded.part01.rar")),
    )
    assert agrees_with_request(
        SimpleNamespace(**work, language="en", metadata_fields={}),
        value.model_dump(),
        MatchEvidence(),
    )


@pytest.mark.parametrize(
    ("change", "preferences", "reason"),
    [
        ({"authors": ["Other Writer"]}, {}, "corroborate"),
        ({"medium": "ebook"}, {}, "medium"),
        ({"language": "fr"}, {}, "language"),
        ({"raw_title": "Harbor sample"}, {}, "partial"),
        ({}, {"blocked_formats": ["m4b"]}, "blocked"),
        ({"formats": ["aax"]}, {}, "supported"),
        ({"formats": ["flac"]}, {}, "reviewed importing"),
        ({}, {"audio_formats": ["mp3"]}, "preferred"),
        ({"raw_title": "Harbor Books 1-3"}, {}, "coverage review"),
    ],
)
def test_usenet_transport_exception_preserves_selection_constraints(change, preferences, reason):
    from app.adapters.nzb_descriptor import inspect_nzb
    from tests.nzb_fixture import nzb_bytes

    value = release().model_copy(update={"protocol": "nzb", **change})
    reasons = eligibility(
        value,
        WORK,
        RULE,
        ReleasePreferences(**preferences),
        unattended=True,
        descriptor=inspect_nzb(nzb_bytes(filename="encoded.rar")),
    )
    assert any(reason in item.lower() for item in reasons), reasons


def test_narrator_agreement_does_not_prove_exact_recording_and_unknown_abridgment_is_held():
    version = SimpleNamespace(medium="audio", language="en", narrators=["Reader"], identifiers={})
    assert any(
        "recording identity" in r
        for r in eligibility(
            release(narrators=["Reader"]), WORK, RULE, ReleasePreferences(), version=version
        )
    )
    assert any(
        "abridgment" in r
        for r in eligibility(release(), WORK, {**RULE, "abridged": False}, ReleasePreferences())
    )
    assert not eligibility(
        release(tags=["Unabridged"]), WORK, {**RULE, "abridged": False}, ReleasePreferences()
    )


def test_exact_ebook_requires_corroborated_isbn_and_actual_formats_obey_profile():
    value = release().model_copy(
        update={"medium": "ebook", "formats": ["epub"], "isbn": "9780306406157"}
    )

    version = SimpleNamespace(
        medium="ebook", language="en", identifiers={"isbn13": ["9780306406157"]}
    )
    rule = {**RULE, "medium": "ebook"}
    assert not eligibility(
        value,
        WORK,
        rule,
        ReleasePreferences(),
        version=version,
        descriptor=descriptor(["Harbor.epub"]),
    )
    version.identifiers = {"isbn13": ["9780140328721"]}
    assert any(
        "ISBN" in r for r in eligibility(value, WORK, rule, ReleasePreferences(), version=version)
    )
    assert any(
        "blocked" in r
        for r in eligibility(
            value,
            WORK,
            rule,
            ReleasePreferences(blocked_formats=["pdf"]),
            descriptor=descriptor(["Harbor.epub", "Harbor.pdf"]),
        )
    )


@pytest.mark.parametrize("isbn", ["9780306406157", "0-306-40615-2", "ISBN-13: 978-0-306-40615-7"])
def test_exact_recording_with_explicit_isbn_and_complete_credits_can_be_selected(isbn):
    version = SimpleNamespace(
        medium="audio",
        language="en",
        narrators=["Reader"],
        abridged=False,
        identifiers={"isbn_13": ["9780306406157"]},
    )
    assert not eligibility(
        release(isbn=isbn, narrators=[" reader "], tags=["Unabridged"]),
        WORK,
        RULE,
        ReleasePreferences(),
        version=version,
        descriptor=descriptor(["Harbor.m4b"]),
    )


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"isbn": None}, "recording identity"),
        ({"isbn": "9780140328721"}, "recording identity"),
        ({"isbn": "9780306406158"}, "recording identity"),
        ({"narrators": []}, "complete narrator"),
        ({"narrators": ["Other Reader"]}, "complete narrator"),
        ({"narrators": ["Reader", "Other Reader"]}, "complete narrator"),
        ({"tags": ["Abridged"]}, "abridgment conflicts"),
        ({"tags": ["Abridged", "Unabridged"]}, "contradictory"),
    ],
)
def test_exact_recording_rejects_missing_or_conflicting_source_evidence(changes, expected):
    version = SimpleNamespace(
        medium="audio",
        language="en",
        narrators=["Reader"],
        abridged=False,
        identifiers={"isbn13": "9780306406157"},
    )
    value = release(isbn="9780306406157", narrators=["Reader"]).model_copy(update=changes)
    assert any(
        expected in reason
        for reason in eligibility(value, WORK, RULE, ReleasePreferences(), version=version)
    )


def test_same_invalid_isbn_never_establishes_edition_identity():
    version = SimpleNamespace(medium="ebook", language="en", identifiers={"isbn": "9780306406158"})
    value = release(isbn="9780306406158").model_copy(
        update={"medium": "ebook", "formats": ["epub"]}
    )
    assert any(
        "ISBN" in reason
        for reason in eligibility(
            value, WORK, {**RULE, "medium": "ebook"}, ReleasePreferences(), version=version
        )
    )


def test_large_transfers_ignore_legacy_profile_and_automatic_size_caps():
    manifest = descriptor(["Harbor.m4b"]).model_copy(update={"torrent_bytes": 100 * 1024**3})
    assert not eligibility(
        release().model_copy(update={"size_bytes": 100 * 1024**3}),
        WORK,
        RULE,
        ReleasePreferences(maximum_bytes=1),
        descriptor=manifest,
    )


def test_reviewed_preparation_can_retain_formats_that_cannot_yet_import_unattended():
    value = release().model_copy(update={"formats": ["flac"]})
    manifest = descriptor(["Harbor.flac"])
    assert not eligibility(value, WORK, RULE, ReleasePreferences(), descriptor=manifest)
    assert any(
        "reviewed importing" in reason
        for reason in eligibility(
            value, WORK, RULE, ReleasePreferences(), descriptor=manifest, unattended=True
        )
    )
