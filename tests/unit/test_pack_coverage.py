from copy import deepcopy

import pytest

from app.adapters.mam import SourceSeries
from app.domain import pack_coverage
from app.domain.automatic_eligibility import collection_candidate, eligibility
from app.domain.release_profiles import ReleasePreferences
from tests.unit.test_automatic_eligibility import RULE, WORK, descriptor, release

CATALOG = {
    "target_id": "harbor",
    "series": [
        {
            "id": "series",
            "generation": 1,
            "name": "Coast",
            "members": [
                {"id": "harbor", "title": "Harbor", "authors": ["Writer"]},
                {"id": "roads", "title": "Roads", "authors": ["Writer"]},
            ],
        }
    ],
}


def pack(**changes):
    return release().model_copy(
        update={"title": "Coast", "raw_title": "Coast Books 1-2", **changes}
    )


@pytest.mark.parametrize(
    "title",
    ["Angels and Demons", "Angels & Demons", "Angels ＆ Demons (Unabridged)"],
)
def test_single_series_book_title_variants_do_not_require_collection_proof(title):
    work = {"title": "Angels & Demons", "authors": ["Dan Brown"]}
    catalog = {
        "target_id": "angels",
        "series": [{"name": "Robert Langdon", "members": []}],
    }
    value = release().model_copy(
        update={
            "title": title,
            "raw_title": title,
            "authors": work["authors"],
            "series": [SourceSeries(source_id="langdon", name="Robert Langdon", position="1")],
        }
    )
    assert not collection_candidate(value, work, catalog)
    assert not eligibility(
        value,
        work,
        RULE,
        ReleasePreferences(prefer_series_packs=False),
        descriptor=descriptor(["Angels and Demons.m4b"]),
        catalog=catalog,
        unattended=True,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_title": "Angels and Demons collection"},
        {"tags": ["Books 1-2"]},
        {"series": [SourceSeries(source_id="langdon", name="Robert Langdon", position="1-2")]},
        {"details": {"collection_contents": {"items": [{"title": "A"}, {"title": "B"}]}}},
        {"title": "Angels and Demons: Volume 2"},
        {"title": "Robert Langdon"},
        {"title": "Angels and Demons and The Da Vinci Code"},
    ],
)
def test_title_equivalence_does_not_hide_collection_or_conflicting_title_evidence(changes):
    work = {"title": "Angels & Demons", "authors": ["Dan Brown"]}
    catalog = {"series": [{"name": "Robert Langdon", "members": []}]}
    value = release().model_copy(
        update={
            "title": "Angels and Demons",
            "raw_title": "Angels and Demons",
            "authors": work["authors"],
            "series": [SourceSeries(source_id="langdon", name="Robert Langdon", position="1")],
            **changes,
        }
    )
    assert collection_candidate(value, work, catalog)
    assert eligibility(
        value,
        work,
        RULE,
        ReleasePreferences(),
        descriptor=descriptor(["Angels and Demons.m4b"]),
        catalog=catalog,
    )


def test_published_catalog_and_manifest_corroborate_children_without_granting_ownership():
    value = pack(medium="ebook", formats=["epub"])
    manifest = descriptor(["01 - Harbor.epub", "02 - Roads.epub"])
    proof = pack_coverage.manifest(value, WORK, CATALOG, manifest, "ebook")
    assert proof["evidence"] == "catalog-and-manifest"
    assert {item["work"]["id"] for item in proof["members"]} == {"harbor", "roads"}
    assert not eligibility(
        value,
        WORK,
        {**RULE, "medium": "ebook"},
        ReleasePreferences(),
        descriptor=manifest,
        catalog=CATALOG,
        unattended=True,
    )
    assert eligibility(
        value,
        WORK,
        {**RULE, "medium": "ebook"},
        ReleasePreferences(prefer_series_packs=False),
        descriptor=manifest,
        catalog=CATALOG,
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["Harbor.epub"],
        ["Harbor.epub", "Unknown.epub"],
        ["Harbor.epub", "Roads.epub", "Roads.pdf"],
        ["Harbor.epub", "Roads/Harbor.epub"],
        ["01.epub", "02.epub"],
        ["Harbor and Roads.epub"],
        ["Harbor.epub", "Roads preview.epub"],
    ],
)
def test_unknown_alternative_partial_omnibus_or_ambiguous_children_need_review(paths):
    value = pack(medium="ebook", formats=["epub"])
    manifest = descriptor(paths)
    assert pack_coverage.manifest(value, WORK, CATALOG, manifest, "ebook") is None
    assert eligibility(
        value,
        WORK,
        {**RULE, "medium": "ebook"},
        ReleasePreferences(),
        descriptor=manifest,
        catalog=CATALOG,
    )


@pytest.mark.parametrize(
    "paths,allowed",
    [
        (["Harbor/01.mp3", "Harbor/02.mp3", "Roads/01.mp3"], True),
        (["Harbor.m4b", "Roads.m4b"], True),
        (["Harbor/01.mp3", "Harbor/01.m4b", "Roads/01.mp3"], False),
        (["Harbor/01.mp3", "Harbor/Chapter 01.mp3", "Roads/01.mp3"], False),
        (["Harbor/Extra.mp3", "Harbor/01.mp3", "Roads/01.mp3"], False),
        (["Harbor/disc1/01.mp3", "Harbor/disc2/01.mp3", "Roads/01.mp3"], False),
    ],
)
def test_audio_groups_have_separate_book_boundaries(paths, allowed):
    # Actual track-number completeness is checked from downloaded tags before import.
    proof = pack_coverage.manifest(pack(), WORK, CATALOG, descriptor(paths), "audio")
    assert bool(proof) is allowed


@pytest.mark.parametrize("ambiguous", [False, True])
def test_collection_connector_variants_keep_ambiguous_catalog_members_behind_review(ambiguous):
    scope = deepcopy(CATALOG)
    scope["series"][0]["name"] = "Coast & Country"
    scope["series"][0]["members"][0]["title"] = "Harbor & Roads"
    value = pack(title="Coast and Country", raw_title="Coast and Country Books 1-2")
    if ambiguous:
        scope["series"][0]["members"].append(
            {"id": "duplicate", "title": "Harbor and Roads", "authors": ["Writer"]}
        )
    proof = pack_coverage.manifest(
        value,
        {**WORK, "title": "Harbor & Roads"},
        scope,
        descriptor(
            [
                "Harbor and Roads/Harbor and Roads 01.mp3",
                "Harbor and Roads/Harbor & Roads 02.mp3",
                "Roads/01.mp3",
            ]
        ),
        "audio",
    )
    assert bool(proof) is not ambiguous
    if proof:
        assert {member["work"]["id"] for member in proof["members"]} == {"harbor", "roads"}


def test_wrong_author_ambiguous_series_and_per_child_constraints_cannot_be_inferred():
    manifest = descriptor(["Harbor.m4b", "Roads.m4b"])
    assert pack_coverage.manifest(pack(authors=["Other"]), WORK, CATALOG, manifest, "audio") is None
    ambiguous = deepcopy(CATALOG)
    ambiguous["series"].append({**ambiguous["series"][0], "id": "another"})
    assert pack_coverage.manifest(pack(), WORK, ambiguous, manifest, "audio") is None
    assert any(
        "separate evidence" in reason
        for reason in eligibility(
            pack(narrators=["Reader"]),
            WORK,
            {**RULE, "required_narrators": ["Reader"]},
            ReleasePreferences(),
            descriptor=manifest,
            catalog=CATALOG,
        )
    )
    assert any(
        "separate evidence" in reason
        for reason in eligibility(
            pack(tags=["Unabridged"]),
            WORK,
            {**RULE, "abridged": False},
            ReleasePreferences(),
            descriptor=manifest,
            catalog=CATALOG,
        )
    )


def test_pack_limits_count_all_bytes_and_bound_additional_works():
    scope = deepcopy(CATALOG)
    for i in range(20):
        scope["series"][0]["members"].append(
            {"id": str(i), "title": f"Other {i}", "authors": ["Writer"]}
        )
    paths = [m["title"] + ".m4b" for m in scope["series"][0]["members"]]
    assert pack_coverage.manifest(pack(), WORK, scope, descriptor(paths), "audio") is None
    assert pack_coverage.manifest(pack(), WORK, scope, descriptor(paths[:-1]), "audio")
    oversized = descriptor(paths[:2]).model_copy(update={"torrent_bytes": 50 * 1024**3 + 1})
    assert not any(
        "transfer size" in reason
        for reason in eligibility(
            pack(), WORK, RULE, ReleasePreferences(), descriptor=oversized, catalog=scope
        )
    )


def test_target_format_preference_cannot_be_satisfied_by_a_siblings_encoding():
    value = pack(formats=["m4b", "mp3"])
    files = descriptor(["Harbor.mp3", "Roads.m4b"])
    proof = pack_coverage.manifest(value, WORK, CATALOG, files, "audio")
    assert pack_coverage.target_formats(proof) == ["mp3"]
    assert any(
        "preferred media format" in reason
        for reason in eligibility(
            value,
            WORK,
            RULE,
            ReleasePreferences(audio_formats=["m4b"]),
            descriptor=files,
            catalog=CATALOG,
        )
    )
    assert not eligibility(
        value,
        WORK,
        RULE,
        ReleasePreferences(audio_formats=["mp3"]),
        descriptor=files,
        catalog=CATALOG,
    )
