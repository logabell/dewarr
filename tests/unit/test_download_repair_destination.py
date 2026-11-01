from app.domain.download_repairs import same_destination

BEFORE = {"root_key": "audio", "mode": "hardlink", "backend": {"generation": 1, "url": "abs"}}


def test_switching_to_copy_keeps_the_destination():
    assert same_destination(BEFORE, {**BEFORE, "mode": "copy"})
    assert same_destination(BEFORE, {**BEFORE, "backend": {"generation": 2, "url": "abs"}})


def test_other_changes_still_need_a_new_selection():
    assert not same_destination({**BEFORE, "mode": "copy"}, BEFORE)
    assert not same_destination(BEFORE, {**BEFORE, "mode": "copy", "root_key": "other"})
    assert not same_destination(BEFORE, {**BEFORE, "backend": {"generation": 1, "url": "other"}})
