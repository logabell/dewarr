from decimal import Decimal
from types import SimpleNamespace as Obj

import pytest

from app.domain.collection_signals import positions, range_claims, recording_options, tagged_claims
from app.importing.collection_recordings import conflict
from app.importing.match_evidence import MatchEvidence


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1-7", set(map(Decimal, range(1, 8)))),
        ("#1–4, 4.5", {Decimal(x) for x in [1, 2, 3, 4, 4.5]}),
        ("7 books", set()),
        ("7-1", set()),
        ("1-10000", set()),
        ("1-4 plus novella", set()),
    ],
)
def test_positions_require_explicit_bounded_membership(raw, expected):
    assert positions(raw) == expected


def test_ranges_and_named_tags_propose_only_matching_catalog_books():
    a = Obj(title="Twilight", series=[{"name": "The Twilight Saga", "position": "1"}])
    b = Obj(title="Quidditch Through the Ages", series=[])
    release = Obj(
        series=[Obj(name="Twilight", position="1-4")],
        tags=["7 series books plus Quidditch Through the Ages"],
    )
    assert [c for c, _ in range_claims(release, [(a, [a.title]), (b, [b.title])])] == [a]
    assert [c for c, _ in tagged_claims(release, [(a, [a.title]), (b, [b.title])])] == [b]


def test_alternate_recordings_keep_distinct_file_suggestions():
    paths = ["Pack/The Stand/Garrick Hagon/01.mp3", "Pack/The Stand/Bruce Huntey/01.mp3"]
    options = recording_options(
        [{"narrator_claim": "Garrick Hagon"}, {"narrator_claim": "Bruce Huntey"}], paths
    )
    assert options[0]["files"] == paths[:1]
    assert options[1]["files"] == paths[1:]


@pytest.mark.parametrize(
    "narrators,paths,expected",
    [
        ([["garrick hagon"]], ["The Stand/01.mp3"], None),
        ([["bruce huntey"]], ["The Stand/01.mp3"], "narrator"),
        ([], ["The Stand/01.mp3"], "narrator"),
        ([["garrick hagon"]], ["The Stand/01.mp3", "The Stand/02.mp3"], "grouping"),
        ([["garrick hagon"]], ["Another recording/01.mp3"], "not mapped"),
    ],
)
def test_import_requires_reviewed_recording_paths_and_embedded_narrator(narrators, paths, expected):
    members = [
        Obj(
            frozen={
                "descriptor": {"name": "Pack"},
                "collection_review": {
                    "paths": ["Pack/The Stand/01.mp3"],
                    "recording": {"narrator_claim": "Garrick Hagon"},
                },
            }
        )
    ]
    result = conflict(
        members, Obj(files=[Obj(path=p) for p in paths]), MatchEvidence(narrators=narrators)
    )
    assert result is None if expected is None else expected in result


@pytest.mark.parametrize(
    "folder_labels,expected",
    [
        (["Original - Reader", "Revised - Reader"], [[0], [1]]),
        (["Reader", "Reader"], [[], []]),
        (["Original Revised - Reader", "Reader"], [[], []]),
    ],
)
def test_same_narrator_editions_require_unambiguous_version_labels(folder_labels, expected):
    paths = [f"Pack/The Gunslinger/{folder}/{i}.mp3" for i, folder in enumerate(folder_labels)]
    options = recording_options(
        [
            {"narrator_claim": "Reader", "recording_notes": "original edition - read by Reader"},
            {"narrator_claim": "Reader", "recording_notes": "revised edition - read by Reader"},
        ],
        paths,
    )
    assert [r["files"] for r in options] == [[paths[i] for i in indexes] for indexes in expected]


@pytest.mark.parametrize(
    "claim,observed,held",
    [
        ("William Hurt and Stephen King", [["stephen king", "william hurt"]], False),
        ("William Hurt; Stephen King", [["stephen king and william hurt"]], False),
        ("William Hurt & Stephen King", [["stephen king", "william hurt"]], False),
        ("William Hurt and Stephen King", [["stephen king"]], True),
        ("William Hurt and Stephen King", [["stephen king", "william hurt", "other reader"]], True),
        ("King, Stephen", [["king, stephen"]], False),
        ("King, Stephen", [["king", "stephen"]], True),
    ],
)
def test_narrator_sets_allow_credit_order_without_dropping_people(claim, observed, held):
    member = Obj(
        frozen={
            "descriptor": {"name": "Pack"},
            "collection_review": {
                "paths": ["Pack/book.mp3"],
                "recording": {"narrator_claim": claim},
            },
        }
    )
    group = Obj(files=[Obj(path="book.mp3")])
    assert bool(conflict([member], group, MatchEvidence(narrators=observed))) is held
