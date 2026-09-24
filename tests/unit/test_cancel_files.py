import json

import pytest

from app.importing.cancel_files import cancel_files
from app.importing.publication import PublicationError, publish_item
from tests.unit.test_import_publication import specification  # noqa: F401


def interrupt_at(point):
    def checkpoint(phase):
        if phase == point:
            raise RuntimeError("Fixture interruption")

    return checkpoint


@pytest.mark.parametrize(
    "mode,point",
    [
        ("hardlink", None),
        ("hardlink", "stage-created"),
        ("hardlink", "file-staged"),
        ("hardlink", "prepared"),
        ("copy", "copy-created"),
        ("copy", "prepared"),
    ],
)
def test_cancel_removes_only_owned_staging_and_blocks_delayed_publication(
    specification,  # noqa: F811
    mode,
    point,
):
    spec = specification.model_copy(update={"mode": mode})
    original = spec.source_root / "pack/book.epub"
    before = original.read_bytes(), original.stat().st_ino, original.stat().st_mtime_ns
    if point:
        with pytest.raises(RuntimeError):
            publish_item(spec, checkpoint=interrupt_at(point))
    receipt = cancel_files(spec)
    assert receipt["state"] == "cancelled"
    assert not list(spec.staging_root.glob("item-*"))
    assert not (spec.destination_root / spec.folder).exists()
    assert (original.read_bytes(), original.stat().st_ino, original.stat().st_mtime_ns) == before
    assert original.stat().st_nlink == 1
    assert cancel_files(spec) == receipt
    with pytest.raises(PublicationError, match="cancelled"):
        publish_item(spec)


@pytest.mark.parametrize(
    "point", ["cancel-file-removed", "cancel-stage-removed", "cancel-receipt-written"]
)
def test_cancel_resumes_its_own_partial_cleanup(specification, point):  # noqa: F811
    spec = specification
    with pytest.raises(RuntimeError):
        publish_item(spec, checkpoint=interrupt_at("prepared"))
    with pytest.raises(RuntimeError):
        cancel_files(spec, checkpoint=interrupt_at(point))
    assert cancel_files(spec)["state"] == "cancelled"
    assert not list(spec.staging_root.glob("item-*"))
    assert (spec.source_root / "pack/book.epub").is_file()


def test_cancel_preserves_published_item_and_external_sidecar_edits(specification):  # noqa: F811
    spec = specification
    with pytest.raises(RuntimeError):
        publish_item(spec, checkpoint=interrupt_at("published-before-receipt"))
    folder = spec.destination_root / spec.folder
    receipt_path = spec.staging_root / f"{spec.entry_id}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["stage_identity"]["inode"] += 1
    receipt_path.write_text(json.dumps(receipt))
    (folder / "metadata.opf").write_text("Later library edit")
    inode = (folder / "First Harbor.epub").stat().st_ino
    assert cancel_files(spec)["state"] == "published"
    assert (folder / "metadata.opf").read_text() == "Later library edit"
    assert (folder / "First Harbor.epub").stat().st_ino == inode
    assert not (folder / ".book-search-publication").exists()


@pytest.mark.parametrize("change", ["extra", "replacement", "symlink"])
def test_cancel_holds_unrecognized_staged_content_without_partial_removal(specification, change):  # noqa: F811
    spec = specification
    with pytest.raises(RuntimeError):
        publish_item(spec, checkpoint=interrupt_at("prepared"))
    receipt = json.loads((spec.staging_root / f"{spec.entry_id}.json").read_text())
    stage = spec.staging_root / receipt["stage_name"]
    if change == "extra":
        (stage / "unrelated.txt").write_text("Keep this file")
    else:
        sidecar = stage / "metadata.opf"
        sidecar.rename(stage / "original.opf")  # Keep original inode allocated.
        if change == "replacement":
            sidecar.write_text("Unknown replacement")
        else:
            sidecar.symlink_to(spec.source_root / "pack/book.epub")
        (stage / "original.opf").rename(spec.staging_root / "preserved-original.opf")
    with pytest.raises((PublicationError, ValueError, OSError)):
        cancel_files(spec)
    assert (stage / "First Harbor.epub").is_file()
    assert (spec.source_root / "pack/book.epub").is_file()


def test_cancel_missing_published_folder_does_not_release_its_history(specification):  # noqa: F811
    spec = specification
    publish_item(spec)
    folder = spec.destination_root / spec.folder
    folder.rename(folder.with_name("Externally moved"))
    with pytest.raises(PublicationError, match="moved"):
        cancel_files(spec)


def test_cancel_keeps_an_unrelated_library_collision(specification):  # noqa: F811
    spec = specification
    folder = spec.destination_root / spec.folder
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("Keep this library item")
    with pytest.raises(PublicationError):
        publish_item(spec)
    assert cancel_files(spec)["state"] == "cancelled"
    assert (folder / "keep.txt").read_text() == "Keep this library item"


def test_cancel_holds_unknown_publication_move_before_receipt_acknowledgement(specification):  # noqa: F811
    spec = specification
    with pytest.raises(RuntimeError):
        publish_item(spec, checkpoint=interrupt_at("published-before-receipt"))
    folder = spec.destination_root / spec.folder
    folder.rename(folder.with_name("Externally moved"))
    with pytest.raises(PublicationError, match="publication may have completed elsewhere"):
        cancel_files(spec)


def test_missing_journal_does_not_prove_existing_destination_was_never_published(specification):  # noqa: F811
    spec = specification
    publish_item(spec)
    (spec.staging_root / f"{spec.entry_id}.json").unlink()
    with pytest.raises(PublicationError, match="without a publication journal"):
        cancel_files(spec)
    assert (spec.destination_root / spec.folder / "First Harbor.epub").is_file()
