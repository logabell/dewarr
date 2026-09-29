from pathlib import Path

from app.domain.collection_contents import extract

ROOT = Path(__file__).parents[1] / "fixtures/collections"


def test_live_collection_lists_preserve_recording_variants_and_partial_author_scope():
    assert len(extract((ROOT / "mam-130074.txt").read_text())["items"]) == 55
    brown = extract((ROOT / "mam-60963.txt").read_text())
    assert len(brown["items"]) == 5
    angels = next(x for x in brown["items"] if x["title"] == "Angels and Demons")
    assert len(angels["evidence"]) == 2
    assert angels["recordings"][0]["abridgment_claim"] == "abridged"
    king = extract((ROOT / "mam-61157.txt").read_text())
    assert len(king["items"]) == 63
    assert sum(len(x["recordings"]) for x in king["items"]) == 82
    assert len(next(x for x in king["items"] if x["title"] == "The Stand")["recordings"]) == 3


def test_html_contents_and_anthology_do_not_invent_novels():
    assert len(extract((ROOT / "mam-314448.txt").read_text())["items"]) == 88
    stories = extract((ROOT / "mam-330111.txt").read_text())
    assert len(stories["items"]) == 16
    assert all(
        e["content_kind_hint"] == "short_story" for x in stories["items"] for e in x["evidence"]
    )
    assert extract((ROOT / "mam-20171.txt").read_text())["items"] == []


def test_negatives_and_limits_are_explicit():
    result = extract(
        "Includes:\nCarrie\nThe Shining\nNot included:\n1986 - It\n"
        "Other books by the author:\nDuma Key (2008)"
    )
    assert {x["title"] for x in result["items"]} == {"Carrie", "The Shining"}
    assert {x["title"] for x in result["excluded"]} == {"It", "Duma Key"}
    assert extract("Includes:\n" + "\n".join(f"Book {i}" for i in range(102)))["truncated"]


def test_mam_adapter_preserves_structural_list_boundaries_before_plain_text_display():
    from datetime import UTC, datetime

    from app.adapters.mam import release
    from tests.mam_fixture import release_row

    for identifier, expected in ((314448, 88), (330111, 16)):
        value = release(
            release_row(description=(ROOT / f"mam-{identifier}.txt").read_text()), datetime.now(UTC)
        )
        assert len(value.details["collection_contents"]["items"]) == expected
