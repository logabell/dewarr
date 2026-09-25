from types import SimpleNamespace

from app.api.requests import _chip, _matches_card, projection_page


def _card(**target):
    return SimpleNamespace(
        approval_status="approved",
        reasons=[SimpleNamespace(active=True)],
        targets=[
            SimpleNamespace(
                message=target.pop("message", ""),
                attempt_state=target.pop("attempt_state", None),
                next_action=target.pop("next_action", "none"),
                **target,
            )
        ],
    )


def test_downloading_filter_drops_a_copy_the_chip_already_calls_in_library():
    card = _card(state="satisfied", attempt_state="complete")
    assert _chip(card, card.targets[0]) == "in-library"
    assert not _matches_card(card, "downloading")
    assert _matches_card(card, "library")


def test_filter_page_stops_at_the_page_or_the_scan_budget():
    matches = [True] * 30
    kept, nxt = projection_page(matches, cursor=0, limit=10, budget=40)
    assert kept == list(range(10))
    assert nxt == 10
    kept, nxt = projection_page(matches, cursor=10, limit=10, budget=40)
    assert kept == list(range(10, 20))
    assert nxt == 20
    kept, nxt = projection_page([True] * 4, cursor=0, limit=10, budget=40)
    assert kept == [0, 1, 2, 3]
    assert nxt is None
    sparse = [False] * 50 + [True]
    kept, nxt = projection_page(sparse, cursor=0, limit=10, budget=40)
    assert kept == []
    assert nxt == 40
    kept, nxt = projection_page(sparse, cursor=40, limit=10, budget=40)
    assert kept == [50]
    assert nxt is None


def test_committed_release_without_a_transfer_matches_downloading():
    card = _card(state="wanted", next_action="downloads")
    assert _chip(card, card.targets[0]) == "downloading"
    assert _matches_card(card, "downloading")


def test_downloading_filter_keeps_an_import_and_skips_an_inventory_check():
    importing = _card(state="wanted", attempt_state="complete")
    checking = _card(state="awaiting-inventory", attempt_state="complete")
    assert _matches_card(importing, "downloading")
    assert not _matches_card(checking, "downloading")
    assert _chip(checking, checking.targets[0]) == "check-inventory"


def test_held_import_is_review_instead_of_forever_importing():
    card = _card(state="wanted", attempt_state="complete", needs_review=True)
    assert _chip(card, card.targets[0]) == "review"
    assert _matches_card(card, "review")
    assert not _matches_card(card, "downloading")
