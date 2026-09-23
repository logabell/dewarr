"""Upcoming dates, monitored requests, and pre-release library enrichment."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.adapters.audiobookshelf import metadata_patch
from app.adapters.catalog_types import BookData, catalog_snapshot
from app.adapters.contracts import AdapterError
from app.adapters.librofm import parse_publication, parse_search
from app.domain.hardcover_matching import MatchEvidence
from app.domain.libro_enrichment import enrich
from app.domain.libro_library import library_token, lookup
from app.domain.quick_add import _idle_outcome
from app.domain.release_dates import (
    assign_release,
    audiobook_date_label,
    choose_release,
    due_action,
    filter_discover,
    merge_month,
    parse_iso_day,
    replaces_release,
    search_allowed,
)

TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)

PAGE = """
<html><head>
<script type="application/ld+json">
{"@type":"Audiobook","name":"Corvak's Challenge","publisher":"Tantor Media, Inc",
 "datePublished":"2026-10-27","description":"<p>Abandoned on the ice planet.</p>"}
</script>
</head><body>
<h1 class="audiobook-title">Corvak's Challenge</h1>
<div class="audiobook-title__series"><a href="/search">Ice Planet Clones</a></div>
<p><strong>Publication date: </strong> October 27, 2026 </p>
</body></html>
"""

YEAR_PAGE = """
<html><body>
<script type="application/ld+json">{"@type":"Audiobook","datePublished":"2026"}</script>
<h1 class="audiobook-title">Corvak's Challenge</h1>
</body></html>
"""

HIT = {
    "title": "Corvak's Challenge",
    "isbn": 9798318540547,
    "authors": ["Ruby Dixon"],
    "cover_url": "//covers.libro.fm/9798318540547_1120.jpg",
    "catalog_info": {"bookseller_pick": False, "new_release": False, "coming_soon": True},
    "audiobook_info": {"narrators": ["Hollie Jackson", "Mason Lloyd"]},
}


def search_payload(*books):
    return {"audiobook_collection": {"audiobooks": list(books), "sort_filter_content": {}}}


def test_audio_edition_date_wins_and_a_missing_audio_date_is_labeled_unknown():
    day, basis = choose_release(audio="2026-10-27", work="2026-09-01")
    assert day == date(2026, 10, 27) and basis == "audiobook"
    day, basis = choose_release(libro="2026-10-27", audio="2026-11-01", work="2026-09-01")
    assert day == date(2026, 10, 27) and basis == "audiobook"
    day, basis = choose_release(audio=None, work="2026-09-01")
    assert day == date(2026, 9, 1) and basis == "work"
    assert audiobook_date_label(basis) == "unknown"
    assert parse_iso_day("2026") is None
    assert parse_iso_day("2026-10") is None


def test_genre_filter_hides_discover_items_and_keeps_a_followed_book():
    personal = [
        {
            "work_id": "followed",
            "external_id": "9",
            "title": "Outside the filter",
            "genres": ["romance"],
            "release_date": date(2026, 10, 2),
        }
    ]
    discover = [
        {
            "external_id": "1",
            "title": "Fantasy",
            "genres": ["fantasy"],
            "release_date": date(2026, 10, 3),
        },
        {
            "external_id": "2",
            "title": "Romance",
            "genres": ["romance"],
            "release_date": date(2026, 10, 4),
        },
        {
            "external_id": "3",
            "title": "Untagged",
            "genres": [],
            "release_date": date(2026, 10, 5),
        },
        {
            "external_id": "9",
            "title": "Duplicate of the follow",
            "genres": ["romance"],
            "release_date": date(2026, 10, 2),
        },
    ]
    shown = merge_month(personal, discover, ["romance"])
    assert [item["title"] for item in shown] == ["Outside the filter", "Romance"]
    assert filter_discover(discover, []) == []


def test_an_audiobook_day_replaces_an_undated_follow_for_the_same_book():
    personal = [
        {
            "work_id": "followed",
            "title": "Waiting book",
            "followed": True,
            "release_date": None,
            "basis": "unknown",
            "state": "waiting",
        }
    ]
    discover = [
        {
            "work_id": "followed",
            "external_id": "42",
            "title": "Waiting book",
            "genres": ["fantasy"],
            "release_date": date(2026, 10, 27),
            "basis": "audiobook",
        }
    ]
    shown = merge_month(personal, discover, ["fantasy"])
    assert len(shown) == 1
    assert shown[0]["release_date"] == date(2026, 10, 27)
    assert shown[0]["basis"] == "audiobook"
    assert shown[0]["followed"] is True
    assert replaces_release(date(2026, 9, 1), "work", date(2026, 10, 27), "audiobook")
    assert not replaces_release(date(2026, 10, 27), "audiobook", date(2026, 11, 1), "work")
    assert not replaces_release(date(2026, 10, 27), "audiobook", date(2026, 10, 28), "audiobook")


def test_an_open_request_is_not_already_available():
    wanted = SimpleNamespace(state="wanted", message="Requested media is missing")
    status, message = _idle_outcome([wanted], [], [])
    assert status == "held"
    assert "Already available" not in message
    satisfied = SimpleNamespace(state="satisfied", message="In your library")
    status, message = _idle_outcome([satisfied], [], [])
    assert status == "completed" and message.startswith("Already available")


def test_search_waits_for_release_day_then_retries_without_downloading_a_mismatch():
    assert search_allowed(date(2026, 10, 27), TODAY) is False
    assert search_allowed(TODAY, TODAY) is True
    assert search_allowed(None, TODAY, coming_soon=True) is False
    assert (
        due_action(
            state="waiting",
            release_date=date(2026, 10, 27),
            today=TODAY,
            now=NOW,
            next_check_at=None,
            owned=False,
            in_flight=False,
            failed=False,
        )
        == "hold"
    )
    assert (
        due_action(
            state="waiting",
            release_date=TODAY,
            today=TODAY,
            now=NOW,
            next_check_at=NOW,
            owned=False,
            in_flight=False,
            failed=False,
        )
        == "resume"
    )
    assert (
        due_action(
            state="wanted",
            release_date=TODAY,
            today=TODAY,
            now=NOW,
            next_check_at=NOW,
            owned=False,
            in_flight=False,
            failed=True,
        )
        == "search"
    )


def test_publication_page_uses_a_full_day_and_leaves_a_year_undated():
    page = parse_publication(PAGE)
    assert page.date == date(2026, 10, 27)
    assert page.publisher == "Tantor Media, Inc"
    assert page.series == "Ice Planet Clones"
    assert "ice planet" in page.description.casefold()
    assert parse_publication(YEAR_PAGE).date is None
    hits = parse_search(search_payload(HIT))
    assert hits[0].isbn == "9798318540547"
    assert hits[0].coming_soon is True
    assert hits[0].cover_url == "https://covers.libro.fm/9798318540547_1120.jpg"
    with pytest.raises(AdapterError):
        parse_search({"audiobook_collection": {}})


def _work():
    return SimpleNamespace(
        title="Corvak's Challenge",
        authors=["Ruby Dixon"],
        description=None,
        cover_url=None,
        publication_year=None,
        metadata_fields={"origin": "audiobookshelf"},
    )


def test_a_followed_catalog_book_stores_a_snapshot_the_book_page_can_read():
    raw = catalog_snapshot("hardcover", "7", "Harbor", ["Writer"], "https://evil.example/cover.jpg")
    book = BookData.model_validate(raw)
    assert book.provider == "hardcover"
    assert book.external_id == "7"
    assert book.title == "Harbor"
    assert book.cover_url is None


def test_a_dated_release_clears_a_sticky_coming_soon_flag():
    fields = assign_release({}, None, "unknown", coming_soon=True, source="local")
    assert fields["release"]["coming_soon"] is True
    updated = assign_release(
        fields, date(2026, 10, 27), "audiobook", coming_soon=False, source="librofm"
    )
    assert updated["release"]["date"] == "2026-10-27"
    assert updated["release"]["coming_soon"] is False


def test_unreadable_library_credentials_do_not_escape_as_a_decrypt_error():
    with pytest.raises(AdapterError, match="could not be read"):
        library_token("not-a-fernet-token")


async def test_a_locked_title_stays_when_a_pre_release_match_is_applied():
    written = {}

    async def search(query, isbn=False):
        return parse_search(search_payload(HIT))

    async def publication(isbn):
        return parse_publication(PAGE)

    async def write(**payload):
        written.update(payload)

    work = _work()
    work.title = "My title"
    work.metadata_fields = {
        "origin": "audiobookshelf",
        "fields": {"title": {"locked": True, "value": "My title"}},
    }
    result = await enrich(
        work,
        medium="audio",
        evidence=MatchEvidence(
            title="Corvak's Challenge",
            authors=["Ruby Dixon"],
            identifiers=[("isbn", "9798318540547")],
        ),
        known_narrators=["Already known"],
        search=search,
        publication=publication,
        write=write,
    )
    assert result["status"] == "matched"
    assert work.title == "My title"
    assert written["title"] == "My title"
    assert work.metadata_fields["release"]["date"] == "2026-10-27"


async def test_unique_pre_release_hit_fills_the_work_and_the_library_item():
    written = {}

    async def search(query, isbn=False):
        assert isbn and query == "9798318540547"
        return parse_search(search_payload(HIT))

    async def publication(isbn):
        assert isbn == "9798318540547"
        return parse_publication(PAGE)

    async def write(**payload):
        written.update(payload)

    work = _work()
    result = await enrich(
        work,
        medium="audio",
        evidence=MatchEvidence(
            title="Corvak's Challenge",
            authors=["Ruby Dixon"],
            identifiers=[("isbn", "9798318540547")],
        ),
        known_narrators=["Already known"],
        search=search,
        publication=publication,
        write=write,
        cover=b"jpeg",
    )
    assert result["status"] == "matched"
    assert result["release_date"] == date(2026, 10, 27)
    assert work.title == "Corvak's Challenge"
    assert work.metadata_fields["release"]["date"] == "2026-10-27"
    assert work.metadata_fields["release"]["basis"] == "audiobook"
    assert written["narrators"] == ["Hollie Jackson", "Mason Lloyd"]
    assert written["cover"] == b"jpeg"
    assert metadata_patch(written["title"], written["authors"], written["narrators"])["metadata"][
        "narrators"
    ] == [{"name": "Hollie Jackson"}, {"name": "Mason Lloyd"}]


async def test_year_only_page_two_hits_and_ebooks_do_not_invent_a_day_or_call_the_provider():
    calls = {"search": 0}

    async def search(query, isbn=False):
        calls["search"] += 1
        return parse_search(search_payload(HIT, {**HIT, "isbn": 9780000000001, "title": "Other"}))

    async def publication(isbn):
        return parse_publication(YEAR_PAGE)

    async def write(**payload):
        raise AssertionError("unconfirmed hits are not written")

    work = _work()
    result = await enrich(
        work,
        medium="audio",
        evidence=MatchEvidence(title="Corvak's Challenge", authors=["Ruby Dixon"]),
        known_narrators=["Already known"],
        search=search,
        publication=publication,
        write=write,
    )
    assert result["status"] == "unconfirmed"
    assert "release" not in work.metadata_fields
    assert len(work.metadata_fields["libro"]["candidates"]) == 2

    async def one(query, isbn=False):
        return parse_search(search_payload({**HIT, "audiobook_info": {"narrators": []}}))

    async def year(isbn):
        return parse_publication(YEAR_PAGE)

    written = {}

    async def remember(**payload):
        written.update(payload)

    dated = _work()
    matched = await enrich(
        dated,
        medium="audio",
        evidence=MatchEvidence(
            title="Corvak's Challenge",
            authors=["Ruby Dixon"],
            identifiers=[("isbn", "9798318540547")],
        ),
        known_narrators=["Already known"],
        search=one,
        publication=year,
        write=remember,
    )
    assert matched["release_date"] is None
    assert dated.metadata_fields["release"]["date"] is None
    assert written["narrators"] is None
    assert "narrators" not in metadata_patch("Title", ["Ruby Dixon"], None)["metadata"]

    async def forbidden(query, isbn=False):
        raise AssertionError("ebooks do not call the pre-release provider")

    skipped = await lookup({"medium": "ebook"}, search=forbidden, publication=publication)
    assert skipped["status"] == "skipped" and skipped["called"] is False
    assert calls["search"] == 1


def test_series_write_back_sends_only_the_series():
    from app.adapters.audiobookshelf import series_patch

    assert series_patch("The Dresden Files", "1") == {
        "metadata": {"series": [{"name": "The Dresden Files", "sequence": "1"}]}
    }
    assert series_patch("Standalone Tales", None) == {
        "metadata": {"series": [{"name": "Standalone Tales"}]}
    }
