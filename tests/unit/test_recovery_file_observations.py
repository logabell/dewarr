# ruff: noqa: F811
import json
import shutil
from uuid import uuid4

import pytest

from app.adapters.audiobookshelf import ABSItem
from app.importing.publication import publish_item
from app.importing.recovery import (
    grimmory_publication_ids,
    journal_census,
    observe_entry,
    publication_state,
    read_publication,
)
from tests.unit.test_import_publication import specification  # noqa: F401


def inputs(spec):
    return (
        {"id": spec.entry_id, "state": "publishing", "specification": spec.model_dump(mode="json")},
        {
            "import_sources": {"books": str(spec.source_root)},
            "import_destinations": {"ebooks": str(spec.destination_root)},
            "import_staging_root": str(spec.staging_root) if spec.journal_root is None else None,
            "import_storage_routes": {
                "ebooks": {
                    "staging_root": str(spec.staging_root),
                    "journal_root": str(spec.journal_root),
                }
            }
            if spec.journal_root
            else {},
        },
    )


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize(
    "point", ["stage-created", "file-staged", "prepared", "published-before-receipt", "complete"]
)
def test_recovery_reads_partial_and_published_files_without_changing_any_receipt(
    specification, point, protected
):
    spec = specification
    if protected:
        from app.importing.publication import PublicationSpec

        journals = spec.staging_root.parent / "control"
        journals.mkdir(mode=0o700)
        spec.staging_root.chmod(0o777)
        spec = PublicationSpec.model_validate({**spec.model_dump(), "journal_root": journals})

    def crash(phase):
        if phase == point:
            raise RuntimeError("simulated interruption")

    if point == "complete":
        publish_item(spec)
    else:
        with pytest.raises(RuntimeError):
            publish_item(spec, checkpoint=crash)
    if point == "published-before-receipt":
        receipt_path = (spec.journal_root or spec.staging_root) / f"{spec.entry_id}.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["stage_identity"]["inode"] += 1
        receipt_path.write_text(json.dumps(receipt))
    before = {
        str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in spec.source_root.parent.rglob("*")
        if p.is_file()
    }
    state, _, evidence = observe_entry(*inputs(spec))
    assert state == ("published" if point in {"complete", "published-before-receipt"} else "staged")
    assert evidence["source"] == "matches-frozen-files"
    after = {
        str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in spec.source_root.parent.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_deleted_publication_and_untracked_journal_are_reported(specification):
    spec = specification
    publish_item(spec)
    for p in (spec.destination_root / spec.folder).iterdir():
        p.unlink()
    (spec.destination_root / spec.folder).rmdir()
    state, _, evidence = observe_entry(*inputs(spec))
    assert state == "relocated"
    assert evidence["relocated"] is True
    assert "media_identities" not in evidence
    key = uuid4()
    (spec.staging_root / f"{key}.json").write_text(
        json.dumps({"entry_id": str(key), "state": "prepared"})
    )
    assert {r["entry_id"] for r in journal_census(spec.staging_root)} == {
        str(spec.entry_id),
        str(key),
    }


def test_only_a_grimmory_destination_keeps_a_moved_publication_reviewable(specification):
    spec = specification
    publish_item(spec)
    for path in (spec.destination_root / spec.folder).iterdir():
        path.unlink()
    (spec.destination_root / spec.folder).rmdir()
    entry, roots = inputs(spec)
    assert publication_state(entry, roots, grimmory=True)[0] == "relocated"
    assert publication_state(entry, roots, grimmory=False)[0] == "missing"
    destination_id, library_id, integration_id = uuid4(), uuid4(), uuid4()
    entry["destination_id"] = destination_id
    assert (
        grimmory_publication_ids(
            {
                "import_entries": [entry],
                "import_destinations": [{"id": destination_id, "library_id": library_id}],
                "libraries": [{"id": library_id, "integration_id": integration_id}],
                "integrations": [{"id": integration_id, "kind": "audiobookshelf"}],
            }
        )
        == set()
    )
    assert grimmory_publication_ids(
        {
            "import_entries": [entry],
            "import_destinations": [{"id": destination_id, "library_id": library_id}],
            "libraries": [{"id": library_id, "integration_id": integration_id}],
            "integrations": [{"id": integration_id, "kind": "grimmory"}],
        }
    ) == {str(entry["id"])}


async def test_recovery_library_read_allows_the_sync_limit_and_stops_above_it():
    from app.adapters.contracts import AdapterError
    from app.domain.recovery_observers import abs_library

    async def pulse():
        return None

    class Client:
        def __init__(self, total):
            self.total = total

        async def page(self, library_id, page):
            assert page == 0
            return [{"id": str(number)} for number in range(self.total)], self.total

        async def expanded(self, ids):
            return [
                ABSItem(
                    id=item_id,
                    library_id="7",
                    title="Harbor",
                    authors=[],
                    narrators=[],
                )
                for item_id in ids
            ]

    fingerprint, items = await abs_library(Client(10_001), "7", pulse, verify=False)
    assert len(fingerprint) == len(items) == 10_001
    with pytest.raises(AdapterError, match="100,000"):
        await abs_library(Client(100_001), "7", pulse, verify=False)


def test_missing_sidecar_stays_unpublished_while_the_book_file_remains(specification):
    spec = specification
    publish_item(spec)
    (spec.destination_root / spec.folder / "metadata.opf").unlink()
    assert observe_entry(*inputs(spec))[0] == "missing"


def test_symlinked_destination_and_changed_root_are_not_adopted(specification, tmp_path):
    spec = specification
    publish_item(spec)
    target = spec.destination_root / spec.folder
    real = tmp_path / "relocated"
    target.rename(real)
    target.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        observe_entry(*inputs(spec))
    entry, roots = inputs(spec)
    roots["import_destinations"] = {"another": str(tmp_path / "another")}
    with pytest.raises(RuntimeError, match="mounted roots"):
        observe_entry(entry, roots)


def test_journal_census_stops_at_total_byte_and_time_budgets(specification, monkeypatch):
    from app.importing import recovery

    spec = specification
    publish_item(spec)
    receipts = {p: p.read_bytes() for p in spec.staging_root.glob("*.json")}
    monkeypatch.setattr(recovery, "MAX_JOURNAL_BYTES", 1)
    with pytest.raises(RuntimeError, match="256 MiB"):
        journal_census(spec.staging_root)
    monkeypatch.setattr(recovery, "MAX_JOURNAL_BYTES", 256 * 1024 * 1024)
    clock = iter([0, 61])
    monkeypatch.setattr(recovery.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="deadline"):
        journal_census(spec.staging_root)
    assert receipts == {p: p.read_bytes() for p in receipts}


def test_recovery_returns_original_receipt_and_current_media_identity(specification):
    spec = specification
    publish_item(spec)
    state, _, evidence, receipt = read_publication(*inputs(spec))
    assert state == "published"
    assert receipt == json.loads((spec.staging_root / f"{spec.entry_id}.json").read_bytes())
    for file in spec.files:
        current = (spec.destination_root / spec.folder / file.name).stat()
        assert evidence["media_identities"][file.name]["inode"] == current.st_ino
        assert evidence["media_identities"][file.name]["mtime_ns"] == current.st_mtime_ns


def test_media_replacement_during_hash_verification_is_rejected(specification, monkeypatch):
    from app.importing import recovery

    spec = specification
    publish_item(spec)
    original = recovery.verify_item

    def replace_after_hash(*args):
        original(*args)
        target = spec.destination_root / spec.folder / spec.files[0].name
        contents = target.read_bytes()
        target.unlink()
        target.write_bytes(contents)

    monkeypatch.setattr(recovery, "verify_item", replace_after_hash)
    with pytest.raises(RuntimeError, match="media or metadata changed"):
        observe_entry(*inputs(spec))


def test_ancestor_replacement_cannot_leave_proof_for_an_unreachable_folder(
    specification, monkeypatch, tmp_path
):
    from app.importing import recovery

    spec = specification
    publish_item(spec)
    original = recovery.verify_item
    old_parent = tmp_path / "relocated-author"
    parent = (spec.destination_root / spec.folder).parent

    def replace_ancestor(*args):
        original(*args)
        # The open book directory itself is unchanged when its parent moves.
        parent.rename(old_parent)
        shutil.copytree(old_parent, parent)

    monkeypatch.setattr(recovery, "verify_item", replace_ancestor)
    with pytest.raises(RuntimeError, match="Published path changed"):
        observe_entry(*inputs(spec))


def test_sidecar_edit_after_hashing_is_not_accepted(specification, monkeypatch):
    from app.importing import recovery

    spec = specification
    publish_item(spec)
    original = recovery.verify_item

    def edit_after_hash(*args):
        original(*args)
        (spec.destination_root / spec.folder / "metadata.opf").write_text("<changed/>")

    monkeypatch.setattr(recovery, "verify_item", edit_after_hash)
    with pytest.raises(RuntimeError, match="media or metadata changed"):
        observe_entry(*inputs(spec))
