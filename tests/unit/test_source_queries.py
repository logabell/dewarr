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


def test_slskd_queries_add_numbered_title_fallbacks():
    from app.domain.source_queries import slskd_queries

    work = {
        "title": "#12 The Saboteurs",
        "authors": ["Clive Cussler Jack Du Brul"],
    }
    assert slskd_queries(work, "#12 The Saboteurs") == [
        "#12 The Saboteurs",
        "#12 The Saboteurs Brul",
        "The Saboteurs",
    ]


def test_slskd_queries_preserve_custom_search_terms():
    from app.domain.source_queries import slskd_queries

    work = {"title": "#12 The Saboteurs", "authors": ["Clive Cussler Jack Du Brul"]}
    assert slskd_queries(work, "Saboteurs epub") == ["Saboteurs epub"]


def test_slskd_relevance_ignores_unrelated_music_folders():
    from types import SimpleNamespace
    from app.domain.source_queries import slskd_relevant

    work = {
        "title": "#12 The Saboteurs",
        "authors": ["Clive Cussler Jack Du Brul"],
    }
    music = [
        SimpleNamespace(
            directory="Micah P. Hinson and the Pioneer Saboteurs",
            files=[],
        ),
        SimpleNamespace(
            directory="Imperial Wax - 2019 Gastwerk Saboteurs",
            files=[],
        ),
    ]
    assert not slskd_relevant(music, work)


def test_slskd_relevance_accepts_numberless_book_folder_without_rewriting_it():
    from types import SimpleNamespace
    from app.domain.source_queries import slskd_relevant

    work = {
        "title": "#12 The Saboteurs",
        "authors": ["Clive Cussler Jack Du Brul"],
    }
    release = SimpleNamespace(
        directory="The Saboteurs",
        files=[],
        title="The Saboteurs",
    )
    assert slskd_relevant([release], work)
    assert release.title == "The Saboteurs"

def test_slskd_relevance_requires_author_evidence_for_audio_folders():
    from types import SimpleNamespace
    from app.domain.source_queries import slskd_relevant

    work = {
        "title": "#12 The Saboteurs",
        "authors": ["Clive Cussler Jack Du Brul"],
    }

    audio_alias = SimpleNamespace(
        medium="audio",
        directory="Raconteurs, The (in Australia known as The Saboteurs)",
        files=[],
    )
    audiobook = SimpleNamespace(
        medium="audio",
        directory="The Saboteurs - Clive Cussler Jack Du Brul",
        files=[],
    )
    ebook = SimpleNamespace(
        medium="ebook",
        directory="The Saboteurs",
        files=[],
    )
    unknown = SimpleNamespace(
        medium=None,
        directory="The Saboteurs",
        files=[],
    )

    assert not slskd_relevant([audio_alias], work)
    assert slskd_relevant([audiobook], work)
    assert slskd_relevant([ebook], work)
    assert slskd_relevant([unknown], work)


def test_slskd_relevance_respects_requested_search_medium():
    from types import SimpleNamespace
    from app.domain.source_queries import slskd_relevant

    work = {
        "title": "#12 The Saboteurs",
        "authors": ["Clive Cussler Jack Du Brul"],
    }
    audio = SimpleNamespace(
        medium="audio",
        directory="The Saboteurs - Clive Cussler Jack Du Brul",
        files=[],
    )
    ebook = SimpleNamespace(
        medium="ebook",
        directory="The Saboteurs",
        files=[],
    )

    assert not slskd_relevant([audio], work, medium="ebook")
    assert not slskd_relevant([ebook], work, medium="audio")
    assert slskd_relevant([audio], work, medium="audio")
