import errno
import json
import os
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from app.importing import publication
from app.importing.cancel_files import cancel_files
from app.importing.inspection import inspect_download
from app.importing.naming import fingerprint
from app.importing.publication import (
    PublicationBusy,
    PublicationError,
    PublicationSpec,
    PublishFile,
    probe_destination,
    publish_item,
    remember_rename_plan,
)
from app.importing.recovery import journal_census
from tests.filesystem_fixtures import path_bound_directory_handles  # noqa: F401
from tests.media_fixtures import epub


@pytest.fixture
def specification(tmp_path):
    base = tmp_path.resolve()
    source, library, staging = (base / name for name in ("downloads", "library", "staging"))
    epub(source / "pack/book.epub")
    library.mkdir()
    staging.mkdir(mode=0o700)
    snapshot = inspect_download(source, "pack")
    file = snapshot["files"][0]
    return PublicationSpec(
        entry_id=uuid4(),
        plan_revision=fingerprint({"test": True}),
        source_root=source,
        source_relative="pack",
        source_directory=snapshot["directory_identity"],
        destination_root=library,
        staging_root=staging,
        folder="Alex Morgan/First Harbor",
        files=[
            PublishFile(
                source="book.epub",
                name="First Harbor.epub",
                sha256=file["sha256"],
                identity=file["identity"],
            )
        ],
        sidecars={"metadata.opf": "<package/>"},
    )


@pytest.mark.parametrize("mode", ["hardlink", "copy"])
def test_publication_preserves_original_and_keeps_receipts_outside_library(specification, mode):
    spec = specification.model_copy(update={"mode": mode})
    original = spec.source_root / "pack/book.epub"
    before, before_stat = original.read_bytes(), original.stat()
    receipt = publish_item(spec)
    published = spec.destination_root / spec.folder / "First Harbor.epub"
    assert published.read_bytes() == before == original.read_bytes()
    assert (published.stat().st_ino == original.stat().st_ino) == (mode == "hardlink")
    assert original.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert receipt["state"] == "published"
    assert (spec.destination_root / spec.folder / "metadata.opf").read_text() == "<package/>"
    assert not list(spec.destination_root.rglob("*.json"))
    assert publish_item(spec) == receipt


def test_rename_publication_keeps_the_seeding_inode_and_adds_sidecars(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    original = spec.source_root / "pack/book.epub"
    leaf = spec.destination_root / spec.folder
    leaf.mkdir(parents=True)
    published = leaf / "First Harbor.epub"
    os.rename(original, published)
    (leaf / ".torrent").mkdir()
    (leaf / ".torrent" / "cover.jpg").write_bytes(b"cover")
    inode = published.stat().st_ino
    receipt = publish_item(spec)
    assert published.stat().st_ino == inode
    assert (leaf / "metadata.opf").read_text() == "<package/>"
    assert receipt["state"] == "published"
    assert not original.exists()
    assert publish_item(spec)["state"] == "published"


def test_rename_publication_refuses_a_different_file_on_the_same_device(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    original = spec.source_root / "pack/book.epub"
    leaf = spec.destination_root / spec.folder
    leaf.mkdir(parents=True)
    published = leaf / "First Harbor.epub"
    shutil.copy2(original, published)
    with pytest.raises(PublicationError, match="seeding copy"):
        publish_item(spec)
    assert original.exists() and published.exists()


def test_cancelling_a_seeding_rename_keeps_files_already_in_the_library(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    original = spec.source_root / "pack/book.epub"
    leaf = spec.destination_root / spec.folder
    leaf.mkdir(parents=True)
    published = leaf / "First Harbor.epub"
    os.rename(original, published)
    (leaf / ".torrent").mkdir()
    remember_rename_plan(spec, {"location": "/library", "renames": [], "targets": []})
    receipt = cancel_files(spec)
    assert receipt["state"] == "published"
    assert published.read_bytes()
    assert (leaf / ".torrent").is_dir()


def test_cancelling_an_empty_library_folder_does_not_keep_a_missing_book(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    leaf = spec.destination_root / spec.folder
    leaf.mkdir(parents=True)
    (leaf / ".torrent").mkdir()
    remember_rename_plan(spec, {"location": "/library", "renames": [], "targets": []})
    receipt = cancel_files(spec)
    assert receipt["state"] == "cancelled"
    assert leaf.is_dir()


def test_cancelling_a_partial_library_folder_stays_held(specification):
    extra = specification.files[0].model_copy(
        update={"name": "Second.epub", "source": "second.epub"}
    )
    spec = specification.model_copy(
        update={"files": [*specification.files, extra], "mode": "rename"}
    )
    leaf = spec.destination_root / spec.folder
    leaf.mkdir(parents=True)
    published = leaf / "First Harbor.epub"
    os.rename(spec.source_root / "pack/book.epub", published)
    remember_rename_plan(spec, {"location": "/library", "renames": [], "targets": []})
    with pytest.raises(PublicationError, match="does not match"):
        cancel_files(spec)
    assert published.is_file()


def test_cancelling_before_a_seeding_rename_leaves_the_download(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    original = spec.source_root / "pack/book.epub"
    before = original.read_bytes()
    remember_rename_plan(spec, {"location": "/library", "renames": [], "targets": []})
    receipt = cancel_files(spec)
    assert receipt["state"] == "cancelled"
    assert original.read_bytes() == before


@pytest.mark.parametrize(
    "mode,point",
    [
        ("hardlink", "stage-created"),
        ("hardlink", "file-staged"),
        ("hardlink", "prepared"),
        ("hardlink", "published-before-receipt"),
        ("copy", "copy-created"),
        ("copy", "file-staged"),
        ("copy", "prepared"),
        ("copy", "published-before-receipt"),
    ],
)
def test_interrupted_publication_resumes_without_duplicate_or_partial_item(
    specification, mode, point
):
    spec = specification.model_copy(update={"mode": mode})

    def crash(phase):
        if phase == point:
            raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        publish_item(spec, checkpoint=crash)
    folder = spec.destination_root / spec.folder
    assert folder.exists() == (point == "published-before-receipt")
    receipt = publish_item(spec)
    assert receipt["state"] == "published"
    assert sorted(path.name for path in folder.iterdir()) == ["First Harbor.epub", "metadata.opf"]
    assert len(list(spec.destination_root.rglob("*.epub"))) == 1


def test_retry_recognizes_published_item_even_if_download_was_later_removed(specification):
    spec = specification
    publish_item(spec)
    (spec.source_root / "pack/book.epub").unlink()
    assert publish_item(spec)["state"] == "published"


@pytest.mark.parametrize("mode", ["hardlink", "copy"])
@pytest.mark.usefixtures("path_bound_directory_handles")
def test_post_rename_inode_change_recovers_by_publication_marker(specification, mode):
    spec = specification.model_copy(update={"mode": mode})

    def crash(phase):
        if phase == "published-before-receipt":
            raise RuntimeError("simulated mergerfs rename")

    with pytest.raises(RuntimeError, match="mergerfs"):
        publish_item(spec, checkpoint=crash)
    receipt_path = spec.staging_root / f"{spec.entry_id}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["stage_identity"]["inode"] += 1
    receipt_path.write_text(json.dumps(receipt))
    published = spec.destination_root / spec.folder
    assert (published / publication.PUBLICATION_MARKER).is_file()
    assert publication.remaining_import_bytes(spec) == 0
    recovered = publish_item(spec)
    assert recovered["state"] == "published"
    assert recovered["stage_identity"]["inode"] == published.stat().st_ino
    assert not (published / publication.PUBLICATION_MARKER).exists()
    assert publish_item(spec)["state"] == "published"


def test_publication_marker_does_not_adopt_an_identical_replacement(specification):
    spec = specification

    def crash(phase):
        if phase == "published-before-receipt":
            raise RuntimeError("stop after rename")

    with pytest.raises(RuntimeError, match="after rename"):
        publish_item(spec, checkpoint=crash)
    published = spec.destination_root / spec.folder
    moved = published.with_name("Moved original")
    published.rename(moved)
    published.mkdir()
    for source in moved.iterdir():
        if source.name != publication.PUBLICATION_MARKER:
            shutil.copy2(source, published / source.name)
    with pytest.raises(PublicationError, match="another item"):
        publish_item(spec)
    assert sorted(path.name for path in published.iterdir()) == [
        "First Harbor.epub",
        "metadata.opf",
    ]
    assert (moved / publication.PUBLICATION_MARKER).is_file()


def test_same_filesystem_rename_reserves_sidecar_bytes_only(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    assert publication.remaining_import_bytes(spec) == len(b"<package/>")


def test_cross_filesystem_rename_reserves_the_download_folder(specification):
    spec = specification.model_copy(update={"mode": "rename"})
    identity = {**spec.files[0].identity, "device": spec.files[0].identity["device"] + 1}
    spec = spec.model_copy(
        update={"files": [spec.files[0].model_copy(update={"identity": identity})]}
    )
    booklet = spec.source_root / "pack" / "booklet.pdf"
    booklet.write_bytes(b"%PDF")
    payload = sum(
        path.stat().st_size for path in (spec.source_root / "pack").iterdir() if path.is_file()
    )
    assert publication.remaining_import_bytes(spec) == payload + len(b"<package/>")


@pytest.mark.parametrize("point", ["prepared", "published-before-receipt"])
def test_completed_copy_recovery_needs_no_additional_media_bytes(specification, point):
    spec = specification.model_copy(update={"mode": "copy"})
    assert publication.remaining_import_bytes(spec) > spec.files[0].identity["size"]

    def crash(phase):
        if phase == point:
            raise RuntimeError("Interrupted copy")

    with pytest.raises(RuntimeError, match="Interrupted copy"):
        publish_item(spec, checkpoint=crash)
    assert publication.remaining_import_bytes(spec) == 0
    assert publish_item(spec)["state"] == "published"


@pytest.mark.parametrize("contents", [None, "unrelated"])
def test_existing_destination_never_replaced_or_adopted(specification, contents):
    spec = specification
    destination = spec.destination_root / spec.folder
    destination.mkdir(parents=True)
    if contents:
        (destination / "existing.txt").write_text(contents)
    before = destination.stat().st_ino
    with pytest.raises(PublicationError, match="another item"):
        publish_item(spec)
    assert destination.stat().st_ino == before
    if contents:
        assert (destination / "existing.txt").read_text() == contents


def test_source_change_cannot_publish_a_stale_manifest(specification):
    spec = specification
    (spec.source_root / "pack/book.epub").write_bytes(b"changed")
    with pytest.raises(PublicationError, match="changed"):
        publish_item(spec)
    assert not (spec.destination_root / spec.folder).exists()


def test_casefold_collision_and_parent_media_are_held(specification):
    spec = specification
    (spec.destination_root / "alex morgan").mkdir()
    with pytest.raises(PublicationError, match="case variant"):
        publish_item(spec)
    (spec.destination_root / "alex morgan").rename(spec.destination_root / "Alex Morgan")
    (spec.destination_root / "Alex Morgan/old.mp3").write_bytes(b"existing")
    with pytest.raises(PublicationError, match="already contains files"):
        publish_item(spec)
    assert (spec.destination_root / "Alex Morgan/old.mp3").read_bytes() == b"existing"


def test_symlink_destination_parent_cannot_escape(specification, tmp_path):
    spec = specification
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (spec.destination_root / "Alex Morgan").symlink_to(outside)
    with pytest.raises(OSError):
        publish_item(spec)
    assert not list(outside.iterdir())


def test_destination_parent_move_before_rename_is_detected(specification):
    spec = specification

    def move(phase):
        if phase == "before-publish":
            (spec.destination_root / "Alex Morgan").rename(spec.destination_root / "Moved")
            (spec.destination_root / "Alex Morgan").mkdir()

    with pytest.raises(PublicationError, match="parent moved"):
        publish_item(spec, checkpoint=move)
    assert not list(spec.destination_root.rglob("*.epub"))


def test_unknown_staging_file_is_not_deleted_to_make_manifest_fit(specification):
    spec = specification

    def stop(phase):
        if phase == "stage-created":
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        publish_item(spec, checkpoint=stop)
    journal = json.loads((spec.staging_root / f"{spec.entry_id}.json").read_text())
    extra = spec.staging_root / journal["stage_name"] / "unrelated.txt"
    extra.write_text("preserve me")
    with pytest.raises(PublicationError, match="unplanned files"):
        publish_item(spec)
    assert extra.read_text() == "preserve me"


def test_actual_link_and_rename_probe_cleans_only_its_artifacts(specification):
    spec = specification
    source = spec.source_root / "pack/book.epub"
    before = source.read_bytes()
    result = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert result["hardlink"] and result["no_replace"]
    assert source.read_bytes() == before
    assert not list(spec.staging_root.iterdir()) and not list(spec.destination_root.iterdir())


def test_probe_cross_device_link_does_not_silently_choose_copy(specification, monkeypatch):
    spec = specification

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "test mount boundary")

    monkeypatch.setattr(os, "link", cross_device)
    result = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert not result["hardlink"] and result["hardlink_error"] == "EXDEV"
    assert result["no_replace"]


def test_probe_collision_does_not_remove_preexisting_folder(specification, monkeypatch):
    spec = specification

    @dataclass
    class Fixed:
        hex: str = "collision"

    monkeypatch.setattr(publication, "uuid4", Fixed)
    existing = spec.destination_root / ".book-search-probe-collision"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        probe_destination(
            spec.source_root,
            spec.source_relative,
            spec.files[0],
            spec.destination_root,
            spec.staging_root,
        )
    assert existing.is_dir()
    assert not list(spec.staging_root.iterdir())


def test_private_staging_and_root_separation_required(specification):
    spec = specification
    spec.staging_root.chmod(0o755)
    with pytest.raises(PublicationError, match="private"):
        publish_item(spec)
    with pytest.raises(ValueError, match="overlap"):
        PublicationSpec.model_validate(
            {**spec.model_dump(), "staging_root": spec.destination_root / "stage"}
        )


@pytest.fixture
def mapped_storage_owner(monkeypatch):
    """Model an NFS export that reports the worker's files as nobody:users.

    Keep every real filesystem operation and timestamp intact: only the server's
    reported uid/gid differ from the container identity.
    """
    real_fstat = os.fstat

    class MappedStat:
        st_uid = 99
        st_gid = 100

        def __init__(self, info):
            self.info = info

        def __getattr__(self, name):
            return getattr(self.info, name)

    monkeypatch.setattr(os, "fstat", lambda fd: MappedStat(real_fstat(fd)))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)


@pytest.mark.usefixtures("mapped_storage_owner")
@pytest.mark.parametrize("mode", ["hardlink", "copy"])
def test_mapped_owner_supports_probe_publication_recovery_and_retry(
    specification, no_rename_flag, mode
):
    spec = specification.model_copy(update={"mode": mode})
    report = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert report["copy"] and report["hardlink"] and report["no_replace"]
    assert report["no_replace_mode"] == report["receipt_mode"] == "fallback"
    assert not list(spec.staging_root.iterdir())
    original = (spec.source_root / "pack/book.epub").read_bytes()
    receipt = publish_item(spec)
    assert receipt["state"] == "published"
    before = spec.staging_root.stat().st_mtime_ns
    census = journal_census(spec.staging_root)
    assert len(census) == 1 and census[0]["state"] == "published"
    assert spec.staging_root.stat().st_mtime_ns == before  # Recovery remains read-only.
    assert publish_item(spec) == receipt  # Reopen existing locks and receipt.
    assert (spec.destination_root / spec.folder / "First Harbor.epub").read_bytes() == original
    assert (spec.source_root / "pack/book.epub").read_bytes() == original


@pytest.mark.usefixtures("mapped_storage_owner")
def test_mapped_owner_share_without_hardlinks_can_still_copy(
    specification, no_rename_flag, no_hardlinks
):
    spec = specification.model_copy(update={"mode": "copy"})
    report = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert report["copy"] and report["no_replace"] and not report["hardlink"]
    assert publish_item(spec)["state"] == "published"
    assert (spec.destination_root / spec.folder / "First Harbor.epub").read_bytes() == (
        spec.source_root / "pack/book.epub"
    ).read_bytes()


@pytest.mark.usefixtures("mapped_storage_owner")
def test_mapped_owner_does_not_bypass_a_server_write_denial(specification, monkeypatch):
    real_open = os.open

    def deny_writes(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise PermissionError(errno.EACCES, "NFS server denied write")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_writes)
    with pytest.raises(PermissionError, match="NFS server denied write"):
        publish_item(specification)
    assert not list(specification.staging_root.iterdir())
    assert not list(specification.destination_root.iterdir())
    assert (specification.source_root / "pack/book.epub").is_file()


@pytest.mark.usefixtures("mapped_storage_owner")
def test_mapped_owner_supports_cancelling_an_interrupted_import(specification):
    def interrupt(phase):
        if phase == "prepared":
            raise RuntimeError("Interrupted import")

    with pytest.raises(RuntimeError, match="Interrupted import"):
        publish_item(specification, checkpoint=interrupt)
    assert cancel_files(specification)["state"] == "cancelled"
    assert cancel_files(specification)["state"] == "cancelled"
    assert not list(specification.staging_root.glob("item-*"))
    assert not (specification.destination_root / specification.folder).exists()
    assert (specification.source_root / "pack/book.epub").is_file()


@pytest.mark.usefixtures("mapped_storage_owner")
@pytest.mark.parametrize("kind", ["library", "entry"])
def test_mapped_owner_locks_still_exclude_another_worker(specification, kind):
    lock = publication.publication_lock if kind == "library" else publication.entry_lock
    with publication.private_staging(specification.staging_root) as staging:
        with lock(staging, "same-key"):
            with pytest.raises(PublicationBusy):
                with lock(staging, "same-key"):
                    pytest.fail("A second worker acquired the same lock")
        with lock(staging, "same-key"):
            pass


@pytest.mark.parametrize("invalid", ["owner", "hardlink", "symlink", "directory"])
def test_lock_files_still_reject_untrusted_objects(specification, monkeypatch, invalid):
    path = specification.staging_root / "injected-lock"
    if invalid == "directory":
        path.mkdir()
    elif invalid == "symlink":
        path.symlink_to(specification.source_root / "pack/book.epub")
    else:
        path.touch(mode=0o600)
        if invalid == "hardlink":
            os.link(path, specification.staging_root / "second-link")
    real_lock_file = publication._lock_file
    monkeypatch.setattr(
        publication, "_lock_file", lambda staging, name: real_lock_file(staging, path.name)
    )
    if invalid == "owner":
        real_fstat = os.fstat

        def different_owner(fd):
            info = real_fstat(fd)
            if info.st_ino == path.stat().st_ino:
                values = list(info)
                values[4] = info.st_uid + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(os, "fstat", different_owner)
    with publication.private_staging(specification.staging_root) as staging:
        with pytest.raises((PublicationError, OSError)):
            with publication.publication_lock(staging, "key"):
                pytest.fail("An invalid lock was accepted")
    assert path.exists()  # Validation never removes another object's lock.


@pytest.mark.usefixtures("mapped_storage_owner")
def test_mapped_owner_still_requires_private_staging(specification):
    specification.staging_root.chmod(0o775)
    with pytest.raises(PublicationError, match="current permissions are 0775"):
        publish_item(specification)
    assert not list(specification.staging_root.iterdir())


def test_probe_preserves_replaced_destination_during_cleanup(specification, monkeypatch):
    spec = specification
    original = publication.no_replace
    replacements = []

    def replace_after_publish(source_fd, source_name, destination_fd, destination_name):
        mode = original(source_fd, source_name, destination_fd, destination_name)
        if not destination_name.startswith(".book-search-probe-"):
            return mode
        destination = spec.destination_root / destination_name
        destination.rename(spec.destination_root / "moved-original-probe")
        destination.mkdir()
        replacements.append(destination)

    monkeypatch.setattr(publication, "no_replace", replace_after_publish)
    with pytest.raises(PublicationError, match="unrecognized replacement preserved"):
        probe_destination(
            spec.source_root,
            spec.source_relative,
            spec.files[0],
            spec.destination_root,
            spec.staging_root,
        )
    assert replacements and replacements[0].is_dir()
    assert (spec.destination_root / "moved-original-probe").is_dir()
    assert not list(spec.staging_root.iterdir())


@pytest.mark.usefixtures("path_bound_directory_handles")
def test_probe_accepts_a_post_rename_directory_inode_change(specification, monkeypatch):
    spec = specification
    real_stat = os.stat

    def changed_inode(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path).startswith(".book-search-probe-") and kwargs.get("dir_fd") is not None:
            values = list(info)
            values[1] += 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(publication.os, "stat", changed_inode)
    result = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert result["no_replace"]
    assert not list(spec.staging_root.iterdir()) and not list(spec.destination_root.iterdir())


def test_concurrent_publishers_cannot_create_two_items(specification):
    def run():
        try:
            return publish_item(specification)["state"]
        except PublicationBusy:
            return "busy"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert "published" in results
    assert set(results) <= {"published", "busy"}
    assert len(list(specification.destination_root.rglob("*.epub"))) == 1


def test_deleted_or_changed_published_item_is_not_replaced(specification):
    spec = specification
    publish_item(spec)
    published = spec.destination_root / spec.folder / "First Harbor.epub"
    published.unlink()
    with pytest.raises(PublicationError, match="unplanned files"):
        publish_item(spec)
    assert not published.exists()


@pytest.fixture(params=["book.epub", "nested/book.epub"])
def file_spec(specification, request):
    spec = specification
    relative = request.param
    selected = spec.source_root / relative
    selected.parent.mkdir(exist_ok=True)
    (spec.source_root / "pack/book.epub").rename(selected)
    epub(spec.source_root / "neighbor.epub", title="Unrelated download")
    snapshot = inspect_download(spec.source_root, relative)
    file = snapshot["files"][0]
    return PublicationSpec.model_validate(
        {
            **spec.model_dump(),
            "source_kind": "file",
            "source_relative": relative,
            "source_directory": snapshot["directory_identity"],
            "files": [
                {
                    "source": file["path"],
                    "name": "First Harbor.epub",
                    "identity": file["identity"],
                    "sha256": file["sha256"],
                }
            ],
        }
    )


@pytest.mark.parametrize("mode", ["hardlink", "copy"])
def test_file_scoped_publication_is_source_preserving_and_resumable(file_spec, mode):
    spec = file_spec.model_copy(update={"mode": mode})
    selected = spec.source_root / spec.source_relative
    before = selected.read_bytes()
    neighbor = (spec.source_root / "neighbor.epub").read_bytes()

    def crash(phase):
        if phase == "file-staged":
            raise RuntimeError("Synthetic interrupted file import")

    with pytest.raises(RuntimeError, match="interrupted"):
        publish_item(spec, checkpoint=crash)
    epub(selected.parent / "new-download.epub", title="Concurrent download")
    assert publish_item(spec)["state"] == "published"
    published = spec.destination_root / spec.folder / "First Harbor.epub"
    assert before == selected.read_bytes() == published.read_bytes()
    assert (selected.stat().st_ino == published.stat().st_ino) == (mode == "hardlink")
    assert (spec.source_root / "neighbor.epub").read_bytes() == neighbor
    assert len(list(spec.destination_root.rglob("*.epub"))) == 1
    selected.unlink()
    assert publish_item(spec)["state"] == "published"


def test_file_scope_cannot_expand_to_a_different_or_additional_file(file_spec):
    original = file_spec.model_dump()
    for files in (
        [{**original["files"][0], "source": "neighbor.epub"}],
        [
            *original["files"],
            {**original["files"][0], "source": "neighbor.epub", "name": "Extra.epub"},
        ],
    ):
        with pytest.raises(ValueError, match="only its inspected file"):
            PublicationSpec.model_validate({**original, "files": files})


def test_file_scope_cannot_publish_a_replaced_source(file_spec):
    selected = file_spec.source_root / file_spec.source_relative
    selected.rename(selected.with_suffix(".old"))
    epub(selected)
    with pytest.raises(PublicationError, match="changed"):
        publish_item(file_spec)
    assert not (file_spec.destination_root / file_spec.folder).exists()


def test_directory_receipt_fingerprint_remains_backward_compatible(specification):
    old_document = specification.model_dump(mode="json")
    old_document.pop("source_kind")
    old_document.pop("binary_sidecars")
    old_document.pop("conversion")
    assert publication.specification_fingerprint(specification) == fingerprint(old_document)


@pytest.fixture(params=[errno.EINVAL, errno.ENOTSUP, errno.ENOSYS])
def no_rename_flag(monkeypatch, request):
    """NFS, SMB and 9p reject renameat2(RENAME_NOREPLACE) and renameatx_np(RENAME_EXCL)."""

    def unsupported(*args):
        raise OSError(request.param, os.strerror(request.param))

    monkeypatch.setattr(publication, "native_no_replace", unsupported)


def refuse_hardlinks(monkeypatch, code=errno.EPERM):
    def refuse(*args, **kwargs):
        raise OSError(code, "hardlinks unsupported on this share")

    monkeypatch.setattr(os, "link", refuse)


@pytest.fixture
def no_hardlinks(monkeypatch):
    refuse_hardlinks(monkeypatch)


@pytest.fixture
def folders(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    handles = [os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in (source, target)]
    yield source, target, *handles
    for fd in handles:
        os.close(fd)


@pytest.mark.parametrize("links", [True, False])
def test_file_fallback_moves_and_never_replaces(folders, no_rename_flag, monkeypatch, links):
    if not links:
        refuse_hardlinks(monkeypatch, errno.EOPNOTSUPP)
    source, target, source_fd, target_fd = folders
    (source / "new").write_bytes(b"new")
    assert publication.no_replace(source_fd, "new", target_fd, "journal") == "fallback"
    assert (target / "journal").read_bytes() == b"new" and not (source / "new").exists()
    (source / "other").write_bytes(b"other")
    with pytest.raises(FileExistsError):
        publication.no_replace(source_fd, "other", target_fd, "journal")
    assert (target / "journal").read_bytes() == b"new"
    assert (source / "other").read_bytes() == b"other"
    assert sorted(path.name for path in target.iterdir()) == ["journal"]


def test_directory_fallback_moves_and_never_replaces(folders, no_rename_flag):
    source, target, source_fd, target_fd = folders
    (source / "item").mkdir()
    (source / "item/book.epub").write_bytes(b"book")
    assert publication.no_replace(source_fd, "item", target_fd, "Book") == "fallback"
    assert (target / "Book/book.epub").read_bytes() == b"book"
    (target / "file").write_bytes(b"keep")
    (target / "empty").mkdir()
    for existing in ("Book", "file", "empty"):
        (source / "next").mkdir(exist_ok=True)
        with pytest.raises(OSError) as raised:
            publication.no_replace(source_fd, "next", target_fd, existing)
        assert raised.value.errno == errno.EEXIST
    assert (target / "file").read_bytes() == b"keep" and (target / "empty").is_dir()
    assert (target / "Book/book.epub").read_bytes() == b"book"


def test_fallback_accepts_nfs_replies_for_its_own_completed_operation(
    folders, no_rename_flag, monkeypatch
):
    source, target, source_fd, target_fd = folders
    real_link, real_rename = os.link, os.rename

    def replayed_link(*args, **kwargs):
        real_link(*args, **kwargs)
        raise FileExistsError(errno.EEXIST, "replayed")

    def replayed_rename(*args, **kwargs):
        real_rename(*args, **kwargs)
        raise FileNotFoundError(errno.ENOENT, "replayed")

    monkeypatch.setattr(os, "link", replayed_link)
    monkeypatch.setattr(os, "rename", replayed_rename)
    (source / "journal").write_bytes(b"state")
    (source / "item").mkdir()
    publication.no_replace(source_fd, "journal", target_fd, "journal")
    publication.no_replace(source_fd, "item", target_fd, "item")
    assert (target / "journal").read_bytes() == b"state" and (target / "item").is_dir()
    assert not list(source.iterdir())


@pytest.mark.parametrize("mode", ["hardlink", "copy"])
def test_publication_succeeds_without_a_no_replace_rename_flag(specification, no_rename_flag, mode):
    spec = specification.model_copy(update={"mode": mode})
    assert publish_item(spec)["state"] == "published"
    published = spec.destination_root / spec.folder / "First Harbor.epub"
    assert published.read_bytes() == (spec.source_root / "pack/book.epub").read_bytes()
    assert publish_item(spec)["state"] == "published"


def test_copy_publication_without_rename_flag_or_hardlinks(
    specification, no_rename_flag, no_hardlinks
):
    spec = specification.model_copy(update={"mode": "copy"})
    assert publish_item(spec)["state"] == "published"
    journals = list(spec.staging_root.glob("*.json"))
    assert len(journals) == 1 and journals[0].stat().st_size


@pytest.mark.parametrize("links", [True, False])
def test_probe_verifies_fallback_and_cleans_up(specification, no_rename_flag, monkeypatch, links):
    if not links:
        refuse_hardlinks(monkeypatch)
    spec = specification
    result = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert result["no_replace"] and result["hardlink"] == links
    assert result["no_replace_mode"] == result["receipt_mode"] == "fallback"
    assert not list(spec.staging_root.iterdir()) and not list(spec.destination_root.iterdir())


def test_native_probe_reports_its_mode(specification):
    spec = specification
    result = probe_destination(
        spec.source_root,
        spec.source_relative,
        spec.files[0],
        spec.destination_root,
        spec.staging_root,
    )
    assert result["no_replace_mode"] == result["receipt_mode"] == "native"
    assert result["warnings"] == []


def test_interrupted_journal_claim_does_not_block_publication(specification):
    spec = specification
    (spec.staging_root / f"{spec.entry_id}.json").touch()
    with publication.private_staging(spec.staging_root) as staging:
        assert publication.read_receipt(staging, f"{spec.entry_id}.json") is None
    assert publish_item(spec)["state"] == "published"
    journal = json.loads((spec.staging_root / f"{spec.entry_id}.json").read_text())
    assert journal["state"] == "published"


def test_interrupted_journal_claim_is_not_a_held_census(specification):
    (specification.staging_root / f"{uuid4()}.json").touch()
    assert journal_census(specification.staging_root) == []


def test_directory_fsync_is_optional_on_smb(specification, monkeypatch):
    real_fsync = os.fsync

    def smb_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", smb_fsync)
    assert publish_item(specification)["state"] == "published"


def test_probe_explains_missing_file_locks(specification, monkeypatch):
    def no_locks(*args):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(publication.fcntl, "flock", no_locks)
    spec = specification
    with pytest.raises(PublicationError, match="nobrl"):
        probe_destination(
            spec.source_root,
            spec.source_relative,
            spec.files[0],
            spec.destination_root,
            spec.staging_root,
        )
    assert not list(spec.staging_root.iterdir()) and not list(spec.destination_root.iterdir())


def test_smb_noserverino_mounts_are_reported(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 0:21 / / rw,relatime - overlay overlay rw\n"
        "35 22 0:40 / /data/My\\040Media rw,relatime - cifs //nas/media "
        "rw,vers=3.1.1,noserverino,cache=strict\n"
        "36 22 0:41 / /data/nfs rw,relatime shared:5 - nfs4 nas:/export rw,vers=4.2\n"
    )
    warnings = publication.mount_warnings(
        Path("/data/My Media/Audiobooks"),
        Path("/data/My Media/.book-search-staging"),
        Path("/data/nfs/library"),
        mountinfo=mountinfo,
    )
    assert len(warnings) == 1 and "/data/My Media" in warnings[0] and "serverino" in warnings[0]
    assert publication.mount_warnings(Path("/data/x"), mountinfo=tmp_path / "missing") == []


@pytest.mark.usefixtures("path_bound_directory_handles")
def test_copy_publication_with_path_bound_handles_preserves_download(specification, no_hardlinks):
    spec = specification.model_copy(update={"mode": "copy"})
    original = spec.source_root / "pack/book.epub"
    before = original.read_bytes()
    assert publish_item(spec)["state"] == "published"
    assert (spec.destination_root / spec.folder / "First Harbor.epub").read_bytes() == before
    assert original.read_bytes() == before
    assert publish_item(spec)["state"] == "published"
