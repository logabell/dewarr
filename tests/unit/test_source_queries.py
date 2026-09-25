from types import SimpleNamespace

from app.domain.source_queries import default_query


def test_default_query_discovers_by_short_title_without_author_or_recording_labels():
    def work(title, authors):
        return SimpleNamespace(title=title, authors=authors)

    assert default_query(work("Dark Age (1 of 3) [Dramatized Adaptation]", ["Pierce Brown"])) == (
        "Dark Age"
    )
    assert default_query(work("Storm Front", ["Full Cast", "Jim Butcher"])) == "Storm Front"
    assert default_query(work("Harry Potter", ["J.K. Rowling"])) == "Harry Potter"
    assert default_query(work("Brown Girl Dreaming", ["Jacqueline Woodson"])) == (
        "Brown Girl Dreaming"
    )
    assert default_query(work("The Tolkien Reader", ["J.R.R. Tolkien"])) == "The Tolkien Reader"
    assert default_query(work("Anonymous Tales", [])) == "Anonymous Tales"
    assert default_query(work("Atmosphere: A Love Story", ["Taylor Jenkins Reid"])) == "Atmosphere"
    assert (
        default_query(
            work(
                "Enshittification: Why Everything Suddenly Got Worse and What to Do About It",
                ["Cory Doctorow"],
            )
        )
        == "Enshittification"
    )
    assert default_query(work("Harbor: Volume Two", ["Writer"])) == "Harbor: Volume Two"


def test_legacy_search_checkpoints_relax_but_new_defaults_and_custom_terms_stay_unchanged():
    from app.domain.source_queries import book_queries

    work = {"title": "Atmosphere: A Love Story", "authors": ["Taylor Jenkins Reid"]}
    assert book_queries(work, "Atmosphere") == ["Atmosphere"]
    assert book_queries(work, "Atmosphere: A Love Story Reid") == [
        "Atmosphere: A Love Story Reid",
        "Atmosphere Reid",
        "Atmosphere",
    ]
    assert book_queries(work, "atmosphere m4b") == ["atmosphere m4b"]
    assert book_queries({"title": "Harbor", "authors": ["Writer"]}, "Harbor Writer") == [
        "Harbor Writer",
        "Harbor",
    ]
    assert book_queries({"title": "Harbor", "authors": []}, "Harbor") == ["Harbor"]
    assert book_queries(
        {"title": "Harbor: Volume Two", "authors": ["Writer"]}, "Harbor: Volume Two Writer"
    ) == ["Harbor: Volume Two Writer", "Harbor: Volume Two"]
