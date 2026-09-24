from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.release_download_status import for_releases, identity, selection_feedback


def test_release_identity_is_source_and_indexer_scoped():
    release = {"source": "prowlarr", "source_id": "same-id", "indexer_id": 1}
    assert identity(release) != identity({**release, "indexer_id": 2})
    assert identity(release) != identity({**release, "source": "mam"})
    assert identity(release) == identity({**release, "seeders": 100})


def test_pinned_failure_explains_only_the_clicked_release():
    operation = SimpleNamespace(
        status="held",
        message="No eligible release",
        payload={
            "command": {"result_id": "clicked"},
            "decisions": [
                {"result_id": "other", "reasons": ["Unrelated reason"]},
                {"result_id": "clicked", "reasons": ["No ready torrent download route"]},
            ],
        },
    )
    message, reasons = selection_feedback(operation)
    assert reasons == ["No ready torrent download route"]
    assert "No ready torrent download route" in message
    assert "Unrelated" not in message


@pytest.mark.parametrize("imported", [False, True])
async def test_only_confirmed_import_evidence_labels_the_specific_release_imported(imported):
    release = {"source": "mam", "source_id": "501"}
    selection = SimpleNamespace(intent_id=uuid4(), frozen={"release": release})
    attempt = SimpleNamespace(id=uuid4(), state="complete", message="Complete", observation={})
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: []),
                SimpleNamespace(all=lambda: [(selection, attempt, uuid4() if imported else None)]),
            ]
        )
    )
    states = await for_releases(db, uuid4(), uuid4(), [release])
    assert states[identity(release)].state == ("imported" if imported else "downloaded")
    assert states[identity(release)].prevent_download
    assert states[identity(release)].request_id == selection.intent_id
