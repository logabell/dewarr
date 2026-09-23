from types import SimpleNamespace

from app.domain.source_queries import default_query


def test_default_query_is_the_base_title_and_the_first_author_surname():
    def work(title, authors):
        return SimpleNamespace(title=title, authors=authors)

    assert default_query(work("Dark Age (1 of 3) [Dramatized Adaptation]", ["Pierce Brown"])) == (
        "Dark Age Brown"
    )
    assert default_query(work("Storm Front", ["Full Cast", "Jim Butcher"])) == "Storm Front Butcher"
    assert default_query(work("Harry Potter", ["J.K. Rowling"])) == "Harry Potter Rowling"
    assert default_query(work("Brown Girl Dreaming", ["Jacqueline Woodson"])) == (
        "Brown Girl Dreaming Woodson"
    )
    assert default_query(work("The Tolkien Reader", ["J.R.R. Tolkien"])) == "The Tolkien Reader"
    assert default_query(work("Anonymous Tales", [])) == "Anonymous Tales"
