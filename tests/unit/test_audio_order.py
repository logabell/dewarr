from copy import deepcopy
from types import SimpleNamespace

from app.importing import grouping


async def test_old_untagged_inspection_gains_order_and_a_new_review_revision(monkeypatch):
    snapshot = {
        "revision": "old-inspection",
        "files": [],
        "groups": [
            {
                "key": "audio",
                "medium": "audio",
                "title": None,
                "authors": [],
                "narrators": [],
                "identity": "unresolved",
                "full_content": "unverified",
                "files": [{"path": "02.mp3", "track": None}, {"path": "01.mp3", "track": None}],
            }
        ],
    }
    original = deepcopy(snapshot)

    async def none(*args):
        return None

    monkeypatch.setattr(grouping, "latest_grouping", none)
    inspection = SimpleNamespace(id="inspection", snapshot=snapshot)
    revision, content = await grouping.current_grouping(None, inspection)
    assert revision != snapshot["revision"]
    assert [file.track for file in content.groups[0].files] == [2, 1]
    assert snapshot == original  # Keep the original byte inspection unchanged.
    assert (await grouping.current_grouping(None, inspection))[0] == revision

    # An explicit grouping review remains authoritative.
    saved = content.model_dump()
    saved["groups"][0]["files"][0]["track"] = 1
    saved["groups"][0]["files"][1]["track"] = 2

    async def reviewed(*args):
        return SimpleNamespace(revision="reviewed", content=saved)

    monkeypatch.setattr(grouping, "latest_grouping", reviewed)
    revision, content = await grouping.current_grouping(None, inspection)
    assert revision == "reviewed"
    assert [file.track for file in content.groups[0].files] == [1, 2]
