import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.collection_contents import title_key
from app.domain.series_projection import project

DATA = json.loads((Path(__file__).parents[1] / "fixtures/collections/series.json").read_text())

GOLDEN = {
    1048: (
        "Old Man's War|"
        "The Ghost Brigades|"
        "The Last Colony|"
        "Zoe's Tale|"
        "The Human Division|"
        "The End of All Things|"
        "The Shattering Peace"
    ),
    1084: (
        "Angels & Demons|The Da Vinci Code|The Lost Symbol|Inferno|Origin|The Secret of Secrets"
    ),
    1185: (
        "Harry Potter and the Philosopher's Stone|"
        "Harry Potter and the Chamber of Secrets|"
        "Harry Potter and the Prisoner of Azkaban|"
        "Harry Potter and the Goblet of Fire|"
        "Harry Potter and the Order of the Phoenix|"
        "Harry Potter and the Half-Blood Prince|"
        "Harry Potter and the Deathly Hallows"
    ),
    3413: (
        "The Gunslinger|"
        "The Drawing of the Three|"
        "The Waste Lands|"
        "Wizard and Glass|"
        "Wolves of the Calla|"
        "Song of Susannah|"
        "The Dark Tower"
    ),
    997: ("The Way of Kings|Words of Radiance|Oathbringer|Rhythm of War|Wind and Truth"),
    1053: (
        "All Systems Red|"
        "Artificial Condition|"
        "Rogue Protocol|"
        "Exit Strategy|"
        "Network Effect|"
        "Fugitive Telemetry|"
        "System Collapse|"
        "Platform Decay"
    ),
    1026: (
        "Leviathan Wakes|"
        "Caliban's War|"
        "Abaddon's Gate|"
        "Cibola Burn|"
        "Nemesis Games|"
        "Babylon's Ashes|"
        "Persepolis Rising|"
        "Tiamat's Wrath|"
        "Leviathan Falls"
    ),
    1018: (
        "The Colour of Magic|"
        "The Light Fantastic|"
        "Equal Rites|"
        "Mort|"
        "Sourcery|"
        "Wyrd Sisters|"
        "Pyramids|"
        "Guards! Guards!|"
        "Eric|"
        "Moving Pictures|"
        "Reaper Man|"
        "Witches Abroad|"
        "Small Gods|"
        "Lords and Ladies|"
        "Men at Arms|"
        "Soul Music|"
        "Interesting Times|"
        "Maskerade|"
        "Feet of Clay|"
        "Hogfather|"
        "Jingo|"
        "The Last Continent|"
        "Carpe Jugulum|"
        "The Fifth Elephant|"
        "The Truth|"
        "Thief of Time|"
        "The Last Hero|"
        "The Amazing Maurice and His Educated Rodents|"
        "Night Watch|"
        "The Wee Free Men|"
        "Monstrous Regiment|"
        "A Hat Full of Sky|"
        "Going Postal|"
        "Thud!|"
        "Wintersmith|"
        "Making Money|"
        "Unseen Academicals|"
        "I Shall Wear Midnight|"
        "Snuff|"
        "Raising Steam|"
        "The Shepherd's Crown"
    ),
}


@pytest.mark.parametrize("series_id", GOLDEN)
def test_live_series_main_reading_order_against_author_publisher_reference(series_id):
    rows = [
        (SimpleNamespace(**r), SimpleNamespace(id=r["snapshot"]["book"]["external_id"]))
        for r in DATA[str(series_id)]
    ]
    main, classes = project(rows)
    published = [
        e.snapshot["book"]["title"]
        for e, _ in main
        if e.snapshot["release_date"] and e.snapshot["release_date"] <= "2026-09-28"
    ]
    assert list(map(title_key, published)) == list(map(title_key, GOLDEN[series_id].split("|")))
    assert len(classes) == len(rows)
    if series_id == 997:
        assert len(main) == 10
    if series_id == 1048:
        assert main[5][0].snapshot["compilation"]
    if series_id == 3413:
        assert any(
            classes[e.id] == "supplement" and e.snapshot["position"] == "4.5" for e, _ in rows
        )


def test_incomplete_and_undated_placeholder_records_do_not_become_main_books():
    from app.domain.series_projection import category, full_book

    for snapshot in [
        {"book": {"title": "Untitled"}, "position": "6"},
        {
            "book": {"title": "Untitled"},
            "position": "4",
            "release_date": "2000-01-01",
            "metadata_issues": ["missing-title"],
        },
    ]:
        assert category(snapshot) == "other"
        assert not full_book(snapshot)
    assert (
        category({"book": {"title": "Untitled"}, "position": "6", "release_date": "2020-01-01"})
        == "main"
    )
