import json
from pathlib import Path

import pytest

from app.adapters.catalog_types import BookData, EditionData, SearchPage
from app.domain.hardcover_matching import MatchEvidence, compatible, lookup


def book(**values):
    return BookData(
        provider="hardcover",
        external_id="42",
        title="The Giver of Stars",
        authors=["Jojo Moyes"],
        **values,
    )


@pytest.mark.parametrize(
    "title,authors,expected",
    [
        ("The Giver of Stars (Unabridged)", ["Jojo Moyes"], True),
        ("The Giver of Stars: A Novel", ["Jojo Moyes"], True),
        ("The Giver of Stars", ["Someone Else"], False),
        # A part or a dramatization is a recording of the book, not another book.
        ("The Giver of Stars (1 of 3)", ["Jojo Moyes"], True),
        ("The Giver of Stars [Dramatized Adaptation]", ["Jojo Moyes"], True),
        ("The Giver of Stars (Full-Cast Edition)", ["Full Cast", "Jojo Moyes"], True),
        ("The Giver of Stars 2", ["Jojo Moyes"], False),
        ("The Giver of Stars: A Study Guide [Dramatized Adaptation]", ["Jojo Moyes"], False),
        ("The BBC full-cast dramatisation of The Giver of Stars", ["Jojo Moyes"], False),
        ("The Giver of Stars (1 of 3)", ["Full Cast"], False),
    ],
)
def test_normalization_does_not_erase_identity(title, authors, expected):
    assert compatible(MatchEvidence(title=title, authors=authors), book()) is expected


@pytest.mark.parametrize("suffix", ["A Study Guide", "Dramatized Adaptation", "The Graphic Novel"])
def test_provider_derivative_is_not_matched_to_original(suffix):
    evidence = MatchEvidence(title="The Giver of Stars", authors=["Jojo Moyes"])
    candidate = book().model_copy(update={"title": f"The Giver of Stars: {suffix}"})
    assert not compatible(evidence, candidate)


def test_stacked_labels_preserve_narrator_credit():
    from app.domain.catalog_titles import display_title, title_narrators

    title = "The Giver of Stars (read by Julia Whelan) (Unabridged): A Novel"
    assert display_title(title) == "the giver of stars"
    assert title_narrators(title) == ["Julia Whelan"]


async def test_isbn_match_uses_equivalent_identifier_and_canonical_record():
    evidence = MatchEvidence(
        title="The Giver of Stars (Unabridged)",
        authors=["Jojo Moyes"],
        identifiers=[("isbn", "9780306406157")],
    )
    edition = EditionData(external_id="7", identifiers={"isbn_10": "0306406152"})
    candidate = book(editions=[edition], canonical_id="43")
    calls = []

    async def call(operation, *args):
        calls.append((operation, args))
        if operation == "identifier_search":
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        return book().model_copy(update={"external_id": "43"}), False, None

    result = await lookup(evidence, call)
    assert result.status == "matched" and result.basis == "identifier"
    assert result.book.external_id == "43"
    assert calls[1] == ("fetch", ("43",))


async def test_conflicting_identifiers_do_not_fall_back_to_title():
    evidence = MatchEvidence(
        title="The Giver of Stars",
        authors=["Jojo Moyes"],
        identifiers=[("asin", "B000000001"), ("asin", "B000000002")],
    )
    candidates = [
        book(
            editions=[EditionData(external_id=str(i), identifiers={"asin": f"B00000000{i}"})]
        ).model_copy(update={"external_id": str(i)})
        for i in (1, 2)
    ]

    calls = []

    async def call(operation, *args):
        calls.append(operation)
        assert operation != "search"
        if operation == "identifier_search":
            return (
                SearchPage(provider="hardcover", items=candidates, page=1, has_more=False),
                False,
                None,
            )
        assert operation == "fetch_many" and args == (["1", "2"],)
        return {b.external_id: b for b in candidates}, False, None

    assert (await lookup(evidence, call)).status == "unmatched"
    assert calls == ["identifier_search", "fetch_many"]


async def test_identifier_conflict_is_rejected_without_a_full_fetch():
    candidate = book(editions=[EditionData(external_id="7", identifiers={"asin": "B000000001"})])
    calls = []

    async def call(operation, *args):
        calls.append(operation)
        page = SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False)
        return page, False, None

    evidence = MatchEvidence(
        title="Another Book", authors=["Jojo Moyes"], identifiers=[("asin", "B000000001")]
    )
    assert (await lookup(evidence, call)).status == "unmatched"
    assert calls == ["identifier_search"]


async def test_canonical_cycle_is_not_accepted():
    candidate = book(canonical_id="43")

    async def call(operation, *args):
        if operation == "search":
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        return (
            candidate.model_copy(update={"external_id": "43", "canonical_id": "42"})
            if args[0] == "43"
            else candidate,
            False,
            None,
        )

    assert (
        await lookup(MatchEvidence(title=candidate.title, authors=candidate.authors), call)
    ).status == "unmatched"


async def test_malformed_search_falls_back_to_bounded_title_query():
    from app.adapters.contracts import AdapterError, FailureKind

    candidate = book()
    calls = []

    async def call(operation, *args):
        calls.append(operation)
        if operation == "search":
            raise AdapterError(FailureKind.PARSER, "Malformed unrelated search hit")
        if operation == "title_search":
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        return candidate, False, None

    result = await lookup(MatchEvidence(title=candidate.title, authors=candidate.authors), call)
    assert result.status == "matched"
    assert calls == ["search", "title_search", "fetch"]


async def test_bbc_dramatisation_does_not_make_original_novel_ambiguous():
    original = BookData(
        provider="hardcover", external_id="384057", title="'Salem's Lot", authors=["Stephen King"]
    )
    adaptation = original.model_copy(
        update={
            "external_id": "2318197",
            "title": (
                "Salem's Lot: The BBC full-cast dramatisation plus Secret Window, Secret Garden"
            ),
        }
    )

    async def call(operation, *args):
        if operation == "search":
            return (
                SearchPage(
                    provider="hardcover", items=[original, adaptation], page=1, has_more=False
                ),
                False,
                None,
            )
        assert args[0] == original.external_id
        return original, False, None

    match = await lookup(MatchEvidence(title=original.title, authors=original.authors), call)
    assert match.status == "matched"
    assert match.book.external_id == original.external_id


async def test_ambiguous_results_include_books_to_review():
    first = book()
    second = book().model_copy(update={"external_id": "43"})

    calls = []

    async def call(operation, *args):
        calls.append(operation)
        if operation == "search":
            return (
                SearchPage(provider="hardcover", items=[first, second], page=1, has_more=False),
                False,
                None,
            )
        return {"42": first, "43": second}, False, None

    match = await lookup(MatchEvidence(title=first.title, authors=first.authors), call)
    assert match.status == "unmatched"
    assert [b.external_id for b in match.candidates] == ["42", "43"]
    assert calls == ["search", "fetch_many"]


def test_missing_subtitle_separator_preserves_full_title_identity():
    candidate = BookData(
        provider="hardcover",
        external_id="1831181",
        title="Empire of AI: Dreams and Nightmares in Sam Altman's OpenAI",
        authors=["Karen Hao"],
    )
    assert compatible(
        MatchEvidence(
            title="Empire of AI Dreams and Nightmares in Sam Altman's OpenAI", authors=["Karen Hao"]
        ),
        candidate,
    )
    assert not compatible(
        MatchEvidence(title="Empire of AI Other Stories", authors=["Karen Hao"]), candidate
    )


@pytest.mark.parametrize(
    "suffix,expected",
    [
        (" (Cat and Mouse, #2)", True),
        (" (Cat and Mouse #2)", True),
        (" (Cat and Mouse, #2.5)", True),
        (" (Cat and Mouse, #1-2)", False),
        (" (Cat and Mouse, #1–#2)", False),
        (" (Cat and Mouse)", False),
        (" (Graphic Novel, #2)", False),
    ],
)
def test_goodreads_series_membership_is_not_the_title(suffix, expected):
    candidate = BookData(
        provider="hardcover",
        external_id="565267",
        title="Hunting Adeline",
        authors=["H. D. Carlton"],
    )
    evidence = MatchEvidence(title="Hunting Adeline" + suffix, authors=["H.D. Carlton"])
    assert compatible(evidence, candidate) is expected
    assert not compatible(evidence, candidate.model_copy(update={"title": "Haunting Adeline"}))
    assert not compatible(evidence, candidate.model_copy(update={"authors": ["Other Writer"]}))


async def test_hunting_adeline_saved_book_resolves_full_canonical_details():
    evidence = MatchEvidence(title="Hunting Adeline (Cat and Mouse, #2)", authors=["H.D. Carlton"])
    candidate = BookData(
        provider="hardcover",
        external_id="565267",
        title="Hunting Adeline",
        authors=["H. D. Carlton"],
    )
    calls = []

    async def call(operation, *args):
        calls.append((operation, args))
        if operation == "search":
            assert args[0] == "hunting adeline H.D. Carlton"
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        assert operation == "fetch" and args == ("565267",)
        return candidate, False, None

    result = await lookup(evidence, call)
    assert result.status == "matched"
    assert result.book.external_id == "565267"
    assert len(calls) == 2


def test_extra_listed_contributor_matches_the_catalog_author():
    evidence = MatchEvidence(
        title="Harry Potter and the Prisoner of Azkaban",
        authors=["J.K. Rowling", "Mary GrandPré"],
    )
    novel = BookData(
        provider="hardcover",
        external_id="1",
        title="Harry Potter and the Prisoner of Azkaban",
        authors=["J.K. Rowling"],
        cover_url="https://assets.hardcover.app/azkaban.jpg",
    )
    assert compatible(evidence, novel)
    assert not compatible(
        MatchEvidence(title=novel.title, authors=["J.K. Rowling"]),
        novel.model_copy(update={"authors": ["J.K. Rowling", "Mary GrandPré"]}),
    )


async def test_storygraph_illustrator_credit_resolves_one_hardcover_book():
    evidence = MatchEvidence(
        title="Harry Potter and the Prisoner of Azkaban",
        authors=["J.K. Rowling", "Mary GrandPré"],
    )
    novel = BookData(
        provider="hardcover",
        external_id="1",
        title=evidence.title,
        authors=["J.K. Rowling"],
        description="Harry's third year at Hogwarts.",
        cover_url="https://assets.hardcover.app/azkaban.jpg",
    )
    others = [
        novel.model_copy(
            update={
                "external_id": "2",
                "title": "Harry Potter and the Prisoner of Azkaban by J.K. Rowling",
                "authors": ["Bright Summaries"],
            }
        ),
        novel.model_copy(
            update={
                "external_id": "3",
                "title": (
                    "Harry Potter and the Prisoner of Azkaban / Harry Potter and the Goblet of Fire"
                ),
            }
        ),
        novel.model_copy(update={"external_id": "4", "title": "Harry Potter Series: 1-3"}),
    ]

    async def call(operation, *args):
        if operation == "search":
            return (
                SearchPage(
                    provider="hardcover",
                    items=[others[0], novel, *others[1:]],
                    page=1,
                    has_more=False,
                ),
                False,
                None,
            )
        assert operation == "fetch" and args == ("1",)
        return novel, False, None

    result = await lookup(evidence, call)
    assert result.status == "matched"
    assert result.basis == "title-author"
    assert result.book.cover_url == novel.cover_url
    assert result.book.authors == ["J.K. Rowling"]


BOOKS = json.loads((Path(__file__).parents[1] / "fixtures" / "hardcover-books.json").read_text())


def hc(name):
    return BookData.model_validate(BOOKS[name])


def catalog(*books, searches=None):
    """A Hardcover stand-in that records each call."""
    calls = []
    by_id = {book.external_id: book for book in books}

    async def call(operation, *args):
        calls.append((operation, args))
        if operation == "fetch":
            return by_id[args[0]], False, None
        if operation == "fetch_many":
            return {key: by_id[key] for key in args[0]}, False, None
        if operation == "identifier_search":
            wanted = {value for _, value in args[0]}
            items = [
                book
                for book in books
                if any(set(edition.identifiers.values()) & wanted for edition in book.editions)
            ]
        else:
            items = list(books) if searches is None else searches(operation, args)
        return SearchPage(provider="hardcover", items=items, page=1, has_more=False), False, None

    return call, calls


async def test_dramatized_adaptation_matches_the_novel_not_the_adaptation_entry():
    call, _ = catalog(hc("storm_front"), hc("storm_front_adaptation"))
    evidence = MatchEvidence(title="Storm Front [Dramatized Adaptation]", authors=["Jim Butcher"])
    result = await lookup(evidence, call)
    assert result.status == "matched" and result.book.external_id == "1001"


async def test_asin_of_the_adaptation_entry_falls_back_to_the_novel():
    adaptation = hc("storm_front_adaptation").model_copy(
        update={"editions": [EditionData(external_id="9100", identifiers={"asin": "B0CONLYADP"})]}
    )
    call, calls = catalog(hc("storm_front"), adaptation)
    evidence = MatchEvidence(
        title="Storm Front [Dramatized Adaptation]",
        authors=["Jim Butcher"],
        identifiers=[("asin", "B0CONLYADP")],
    )
    result = await lookup(evidence, call)
    assert result.status == "matched" and result.book.external_id == "1001"
    # The adaptation hit is rejected from the search record, before any full fetch.
    assert [operation for operation, _ in calls] == ["identifier_search", "search", "fetch"]


async def test_asin_of_a_dramatized_edition_matches_despite_the_label():
    call, calls = catalog(hc("storm_front"))
    evidence = MatchEvidence(
        title="Storm Front [Dramatized Adaptation]",
        authors=["Jim Butcher"],
        identifiers=[("asin", "B0CGRAPHIC")],
    )
    result = await lookup(evidence, call)
    assert result.status == "matched" and result.basis == "identifier"
    assert "search" not in [operation for operation, _ in calls]


async def test_one_part_matches_the_whole_book_and_series_breaks_a_tie():
    other = BookData.model_validate(
        {
            **BOOKS["dark_age"],
            "external_id": "1109",
            "series": [{"external_id": "9", "name": "Other", "position": "1"}],
        }
    )
    call, _ = catalog(hc("dark_age"), other)
    part = MatchEvidence(
        title="Dark Age (1 of 3) [Dramatized Adaptation]", authors=["Pierce Brown"]
    )
    assert (await lookup(part, call)).status == "unmatched"
    part = part.model_copy(update={"series": [("Red Rising Saga", "5")]})
    result = await lookup(part, call)
    assert result.status == "matched" and result.book.external_id == "1101"


def test_the_same_series_at_another_position_is_another_book():
    evidence = MatchEvidence(
        title="Red Rising", authors=["Pierce Brown"], series=[("Red Rising", "5")]
    )
    assert not compatible(evidence, hc("red_rising"))
    assert compatible(
        evidence.model_copy(update={"series": [("Red Rising", "1")]}), hc("red_rising")
    )


async def test_publisher_credit_matches_through_the_series_in_the_title():
    call, calls = catalog(hc("well_of_ascension"))
    evidence = MatchEvidence(
        title="Mistborn 2 - The Well of Ascension 1 of 3", authors=["GraphicAudio"]
    )
    result = await lookup(evidence, call)
    assert result.status == "matched" and result.basis == "title-series"
    assert calls[0] == ("title_search", ("the well of ascension",))
    wrong_position = MatchEvidence(
        title="Mistborn 5 - The Well of Ascension", authors=["GraphicAudio"]
    )
    assert (await lookup(wrong_position, catalog(hc("well_of_ascension"))[0])).status == "unmatched"
    without_series = MatchEvidence(title="The Well of Ascension", authors=["GraphicAudio"])
    assert (await lookup(without_series, call)).status == "unmatched"


async def test_book_number_suffix_is_checked_against_the_catalog_position():
    novel = hc("prisoner_of_azkaban")

    def searches(operation, args):
        # The numbered title finds nothing; the plain title finds the novel.
        return [] if "book 1" in args[0].lower() else [novel]

    call, _ = catalog(novel, searches=searches)
    third = MatchEvidence(
        title="Harry Potter and the Prisoner of Azkaban, Book 3 (Unabridged)",
        authors=["J.K. Rowling"],
    )
    assert (await lookup(third, call)).status == "matched"
    first = third.model_copy(update={"title": "Harry Potter and the Prisoner of Azkaban, Book 1"})
    assert (await lookup(first, call)).status == "unmatched"


async def test_full_cast_credit_and_series_subtitle_match_the_novel():
    call, calls = catalog(hc("prisoner_of_azkaban"))
    evidence = MatchEvidence(
        title="Harry Potter and the Prisoner of Azkaban: Harry Potter, Year 3",
        authors=["Full Cast", "J.K. Rowling"],
    )
    result = await lookup(evidence, call)
    assert result.status == "matched"
    assert calls[0] == (
        "search",
        ("harry potter and the prisoner of azkaban J.K. Rowling", 1, None),
    )


async def test_initials_written_without_periods_are_the_same_author():
    call, _ = catalog(hc("generation_ai"))
    result = await lookup(MatchEvidence(title="Generation AI", authors=["WR Hulkenberg"]), call)
    assert result.status == "matched"


def test_conflicting_explicit_series_numbers_stay_distinct():
    evidence = MatchEvidence(title="Same Title (Series, #1)", authors=["Writer"])
    candidate = BookData(
        provider="hardcover", external_id="1", title="Same Title (Series, #2)", authors=["Writer"]
    )
    assert not compatible(evidence, candidate)
