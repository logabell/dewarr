from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.adapters.mam import release
from app.adapters.torrent_descriptor import inspect_torrent
from app.domain.release_profiles import (
    PreferenceOverrides,
    ProfileSnapshot,
    ReleasePreferences,
    assess_release,
    enforce_inspected_profile,
    enforce_profile,
    overlay_profile,
    ranking_key,
    resolve_preferences,
    source_popularity,
)
from tests.mam_fixture import release_row
from tests.torrent_fixture import torrent_bytes

WORK = {"title": "Harbor", "authors": ["Writer"]}


@pytest.mark.parametrize(
    ("title", "authors", "expected"),
    [
        ("[M4B] Andy Weir-Project Hail Mary", [], "corroborated"),
        ("Andy Weir - Project Hail Mary", [], "corroborated"),
        ("Project.Hail.Mary.by.Andy.Weir", [], "corroborated"),
        ("Project Hail Mary - Andy Weir.m4b", [], "corroborated"),
        ("Project Hail Mary", [], "possible"),
        ("Andy Weir - Project Hail Mary.par2", [], "possible"),
        ("Andy Weir - Project Hail Mary.part01.rar", [], "possible"),
        ("Andy Weir - Project Hail Mary sample", [], "possible"),
        ("Andy Weir - Project Hail Mary and The Martian", [], "possible"),
        ("[M4B] Andy Weir-Project Hail Mary", ["Other Writer"], "unmatched"),
    ],
)
def test_indexer_exact_author_title_pair_can_replace_missing_structured_fields(
    title, authors, expected
):
    from app.adapters.prowlarr import ProwlarrRelease

    candidate = ProwlarrRelease(
        source_id="fixture",
        title=title,
        raw_title=title,
        authors=authors,
        medium="audio",
        protocol="nzb",
        indexer_name="Fixture",
        categories=[3030],
        observed_at=datetime.now(UTC),
        acquisition_supported=True,
    )
    assessment = assess_release(
        candidate, {"title": "Project Hail Mary", "authors": ["Andy Weir"]}, ReleasePreferences()
    )
    assert assessment.identity == expected


def test_builtin_ebook_format_preference_order():
    assert ReleasePreferences().ebook_formats == [
        "epub",
        "azw3",
        "mobi",
        "pdf",
        "azw",
        "cbz",
        "cbr",
    ]


def test_series_scope_inheritance_and_legacy_boolean_compatibility():
    defaults = ReleasePreferences()
    assert "series_scope" not in defaults.model_dump(mode="json")
    assert defaults.effective_series_scope == "prefer_packs"
    preferences, origins = resolve_preferences(
        [
            ("Installation default", {"series_scope": "complete_series"}),
            ("Personal default", {"prefer_series_packs": False}),
        ]
    )
    assert preferences.effective_series_scope == "just_book"
    assert not preferences.allows_series_packs and "series_scope" not in origins
    result = overlay_profile(
        ProfileSnapshot(preferences=preferences, origins=origins),
        list_overrides={"series_scope": "complete_series"},
    )
    assert result.preferences.effective_series_scope == "complete_series"
    assert result.origins["series_scope"] == "List override"
    result = overlay_profile(result, request_overrides={"series_scope": "just_book"})
    assert not result.preferences.allows_series_packs
    assert result.origins["series_scope"] == "Request override"
    cleared = overlay_profile(result, request_overrides={"series_scope": None})
    assert cleared.preferences.effective_series_scope == "just_book"
    assert cleared.request_overrides.model_dump(mode="json") == {"series_scope": None}


def test_route_layers_preserve_explicit_clearing_and_legacy_unset_snapshots():
    fields = {
        "downloader_id",
        "torrent_downloader_id",
        "usenet_downloader_id",
        "ebook_destination_id",
        "audio_destination_id",
    }
    assert not fields & ReleasePreferences().model_dump(mode="json").keys()
    torrent, usenet = str(uuid4()), str(uuid4())
    chosen, _origins = resolve_preferences(
        [
            (
                "Installation default",
                {"downloader_id": torrent, "usenet_downloader_id": usenet},
            )
        ]
    )
    assert str(chosen.downloader_id) == torrent
    assert chosen.torrent_downloader_id is None
    assert str(chosen.usenet_downloader_id) == usenet
    cleared_client, _origins = resolve_preferences(
        [
            (
                "Installation default",
                {"downloader_id": torrent, "usenet_downloader_id": usenet},
            ),
            ("Personal default", {"usenet_downloader_id": None}),
        ]
    )
    assert cleared_client.usenet_downloader_id is None
    assert str(cleared_client.downloader_id) == torrent
    installation, personal, saved, listed, requested = [str(uuid4()) for _ in range(5)]
    preferences, origins = resolve_preferences(
        [
            (
                "Installation default",
                {"ebook_destination_id": installation, "audio_destination_id": installation},
            ),
            ("Personal default", {"ebook_destination_id": personal}),
            ("Profile", {"ebook_destination_id": saved}),
        ]
    )
    base = ProfileSnapshot(preferences=preferences, origins=origins)
    result = overlay_profile(
        base,
        list_overrides={"ebook_destination_id": listed},
        request_overrides={"ebook_destination_id": requested},
    )
    assert str(result.preferences.ebook_destination_id) == requested
    assert result.origins["ebook_destination_id"] == "Request override"
    assert str(result.preferences.audio_destination_id) == installation
    cleared = overlay_profile(base, request_overrides={"ebook_destination_id": None})
    assert cleared.preferences.ebook_destination_id is None
    assert cleared.request_overrides.model_dump(mode="json") == {"ebook_destination_id": None}
    assert PreferenceOverrides.model_validate(
        cleared.request_overrides.model_dump()
    ).model_fields_set == {"ebook_destination_id"}


def candidate(**changes):
    return release(
        release_row(**{"title": "Harbor", "author_info": '{"1":"Writer"}', **changes}),
        datetime.now(UTC),
    )


def ordered(candidates, preferences):
    return sorted(
        candidates, key=lambda r: ranking_key(r, assess_release(r, WORK, preferences), preferences)
    )


def test_identity_and_blocked_formats_precede_seeds():
    right = candidate(id=1, seeders=0, filetype="M4B")
    wrong = candidate(id=2, seeders=100000).model_copy(update={"title": "Different book"})
    blocked = candidate(id=3, seeders=90000, filetype="MP3")
    preferences = ReleasePreferences(
        criteria=["seeders", "format", "source"], blocked_formats=["mp3"]
    )
    assert ordered([wrong, blocked, right], preferences)[0].source_id == "1"
    assert assess_release(blocked, WORK, preferences).blocked
    assert assess_release(wrong, WORK, preferences).identity == "unmatched"


def test_format_priority_source_priority_and_known_zero_seeds():
    m4b = candidate(id=1, seeders=None, filetype="M4B")
    mp3 = candidate(id=2, seeders=100, filetype="MP3")
    assert ordered([mp3, m4b], ReleasePreferences())[0].source_id == "1"
    assert (
        ordered([mp3, m4b], ReleasePreferences(criteria=["seeders", "format", "source"]))[
            0
        ].source_id
        == "2"
    )
    known = candidate(id=3, seeders=0, filetype="M4B")
    assert ordered([m4b, known], ReleasePreferences())[0].source_id == "3"
    remote = known.model_copy(update={"source": "prowlarr", "indexer_id": "7"})
    preferences = ReleasePreferences(
        source_order=["prowlarr:7", "mam", "prowlarr"], criteria=["source", "format", "seeders"]
    )
    assert ordered([known, remote], preferences)[0].source == "prowlarr"


def test_a_dramatized_release_is_an_edition_but_a_part_is_not_the_whole_book():
    preferences = ReleasePreferences()
    dramatized = candidate(title="Harbor [Dramatized Adaptation] [M4B]")
    assessment = assess_release(dramatized, WORK, preferences)
    assert assessment.identity == "corroborated" and not assessment.blocked
    assert "Dramatized adaptation: an audio edition of this book" in assessment.explanation
    part = assess_release(candidate(title="Harbor (1 of 3) - GraphicAudio"), WORK, preferences)
    assert part.identity == "possible"
    assert part.review == ["This release is part 1 of 3 of the book, not the whole book"]
    narrated = ReleasePreferences(recording_style="narrated")
    assert assess_release(dramatized, WORK, narrated).blocked == [
        "The profile accepts narrated recordings only"
    ]
    assert not assess_release(candidate(), WORK, narrated).blocked
    assert assess_release(
        candidate(), WORK, ReleasePreferences(recording_style="dramatized")
    ).blocked


def test_a_matching_isbn_or_asin_corroborates_a_release_named_differently():
    renamed = candidate(title="Harbor: A Novel of the Coast", isbn="ASIN: B0ABCDEFGH")
    assert assess_release(renamed, WORK, ReleasePreferences()).identity == "possible"
    work = {**WORK, "identifiers": ["B0ABCDEFGH"]}
    assessment = assess_release(renamed, work, ReleasePreferences())
    assert assessment.identity == "corroborated"
    assert "The source's ISBN or ASIN matches an edition of this book" in assessment.explanation
    stranger = renamed.model_copy(update={"authors": ["Someone Else"]})
    assert assess_release(stranger, work, ReleasePreferences()).identity == "unmatched"


def test_recording_style_overrides_can_restore_any_style():
    assert PreferenceOverrides(recording_style="any").model_dump() == {"recording_style": "any"}
    assert "recording_style" not in ReleasePreferences().model_dump()


def test_unknowns_are_not_ownership_or_automatic_eligibility():
    assessment = assess_release(
        candidate(filetype=None, size=None, seeders=None),
        WORK,
        ReleasePreferences(maximum_bytes=1000),
    )
    assert assessment.formats == [] and len(assessment.review) == 2
    assert "Seed count unknown" in assessment.explanation
    assert not hasattr(assessment, "owned")


def test_source_local_popularity_can_precede_seeds_but_not_eligibility():
    preferences = ReleasePreferences(criteria=["format", "source", "popularity", "seeders"])
    popular = candidate(id=1, times_completed=80, seeders=2, filetype="M4B")
    seeded = candidate(id=2, times_completed=5, seeders=100, filetype="M4B")
    unknown = candidate(id=3, times_completed=None, seeders=500, filetype="M4B")
    zero = candidate(id=4, times_completed=0, seeders=1, filetype="M4B")
    wrong = candidate(id=5, times_completed=999999, seeders=999999, filetype="M4B").model_copy(
        update={"title": "Wrong title"}
    )
    blocked = candidate(id=6, times_completed=999999, seeders=999999, filetype="MP3")
    preferences.blocked_formats = ["mp3"]
    assert [
        r.source_id for r in ordered([unknown, seeded, wrong, zero, blocked, popular], preferences)
    ] == ["1", "2", "4", "3", "5", "6"]
    assert ordered([popular, seeded], ReleasePreferences())[0].source_id == "2"
    explanation = assess_release(popular, WORK, preferences).explanation
    assert "MAM reports 80 completed downloads; compared only within MAM" in explanation


def test_popularity_requires_source_groups_and_does_not_invent_other_tracker_counts():
    with pytest.raises(ValidationError, match="Source preference must precede"):
        ReleasePreferences(criteria=["popularity", "source", "format", "seeders"])
    preferences = ReleasePreferences(
        criteria=["source", "popularity", "format", "seeders"], source_order=["prowlarr", "mam"]
    )
    mam = candidate(id=1, times_completed=100000)
    foreign = mam.model_copy(
        update={"source": "prowlarr", "indexer_id": "7", "snatches": 999999999}
    )
    assert source_popularity(foreign) is None
    assert ordered([mam, foreign], preferences)[0] is foreign
    other = foreign.model_copy(update={"source_id": "2", "indexer_id": "8", "seeders": 999999})
    assert ordered([other, foreign], preferences) == [foreign, other]
    for invalid in [None, -1, True, "123"]:
        assert source_popularity(mam.model_copy(update={"snatches": invalid})) is None
    assert source_popularity(mam.model_copy(update={"snatches": 0})) == 0


def test_popularity_does_not_change_legacy_snapshots_or_deterministic_ties():
    first = candidate(id=1, times_completed=7, seeders=3)
    second = candidate(id=2, times_completed=7, seeders=3)
    legacy = ReleasePreferences()
    assert legacy.criteria == ["format", "source", "seeders"]
    assert ranking_key(first, assess_release(first, WORK, legacy), legacy) == (
        False,
        0,
        (0,),
        (0,),
        (False, -3),
        (0,),
        "mam",
        "1",
    )
    preferences = ReleasePreferences(criteria=["source", "popularity", "seeders", "format"])
    assert ordered([second, first], preferences) == [first, second]
    resolved, origins = resolve_preferences([("Profile", {"criteria": preferences.criteria})])
    frozen = ProfileSnapshot(preferences=resolved, origins=origins).model_dump(mode="json")
    assert ProfileSnapshot.model_validate(frozen).model_dump(mode="json") == frozen
    cleared = overlay_profile(
        ProfileSnapshot.model_validate(frozen), request_overrides={"criteria": legacy.criteria}
    )
    assert cleared.preferences.criteria == legacy.criteria


@pytest.mark.parametrize(
    "options",
    [
        {"criteria": ["format"]},
        {"criteria": ["format", "format", "source"]},
        {"ebook_formats": []},
        {"ebook_formats": ["mp3"]},
        {"audio_formats": ["epub"]},
        {"blocked_formats": ["exe"]},
        {"source_order": ["http://unsafe"]},
        {"source_order": ["mam", "mam"]},
        {"maximum_bytes": 0},
    ],
)
def test_invalid_profile_preferences(options):
    with pytest.raises(ValidationError):
        ReleasePreferences(**options)


async def test_actual_manifest_formats_and_size_override_incomplete_source_claims():
    descriptor = await inspect_torrent(torrent_bytes())
    source = candidate(filetype=None, size=None)
    extensions = {f.path.rsplit(".", 1)[-1].lower() for f in descriptor.files}
    blocked = next(iter(extensions & {"m4b", "mp3", "epub", "pdf"}))
    with pytest.raises(HTTPException, match="blocked format"):
        enforce_profile(
            source,
            descriptor,
            ProfileSnapshot(preferences=ReleasePreferences(blocked_formats=[blocked])),
        )
    with pytest.raises(HTTPException, match="size limit"):
        enforce_profile(
            source, descriptor, ProfileSnapshot(preferences=ReleasePreferences(maximum_bytes=1))
        )


def test_observed_companion_formats_and_total_transfer_size_are_enforced():
    files = [
        {"extension": "m4b", "identity": {"size": 100}},
        {"extension": "pdf", "identity": {"size": 10}},
    ]
    with pytest.raises(HTTPException, match="blocked format: pdf"):
        enforce_inspected_profile(
            files, ProfileSnapshot(preferences=ReleasePreferences(blocked_formats=["pdf"]))
        )
    with pytest.raises(HTTPException, match="size limit"):
        enforce_inspected_profile(
            files, ProfileSnapshot(preferences=ReleasePreferences(maximum_bytes=105))
        )
    enforce_inspected_profile(
        files, ProfileSnapshot(preferences=ReleasePreferences(maximum_bytes=110))
    )
