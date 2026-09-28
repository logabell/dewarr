# ruff: noqa: F811
import errno
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.importing import destinations, publication
from app.importing.destinations import (
    STAGING_NAME,
    check_library_route,
    choose_staging,
    holds_journals,
)
from app.importing.filesystem import InspectionError, describe_os_error
from tests.filesystem_fixtures import (
    path_bound_directory_handles,  # noqa: F401
    read_only_downloads,  # noqa: F401
)


@pytest.fixture
def roots(tmp_path):
    values = [tmp_path.resolve() / name for name in ("downloads", "library", "staging")]
    for root in values:
        root.mkdir(mode=0o700)
    return values


def test_concurrent_empty_root_probes_clean_only_their_own_objects(roots):
    source, target, stage = roots
    (source / "keep.bin").write_bytes(b"existing torrent file")
    before = (source / "keep.bin").stat()
    with ThreadPoolExecutor(max_workers=3) as pool:
        reports = list(
            pool.map(
                lambda _: publication.probe_download_folder(source, "", target, stage), range(3)
            )
        )
    assert all(report["hardlink"] and report["copy"] for report in reports)
    assert list(source.iterdir()) == [source / "keep.bin"]
    assert (source / "keep.bin").stat() == before
    assert not list(target.iterdir()) and not list(stage.iterdir())


@pytest.mark.parametrize("protected", [False, True])
def test_failed_link_reports_copy_capability_without_claiming_a_hardlink(
    roots, monkeypatch, protected
):
    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")

    monkeypatch.setattr(publication.os, "link", cross_device)
    source, target, stage = roots
    journals = stage.parent / "control" if protected else None
    if journals:
        journals.mkdir(mode=0o700)
        stage.chmod(0o777)
    report = publication.probe_download_folder(source, "", target, stage, journal_root=journals)
    if journals:
        assert not list(journals.iterdir())
    assert not report["hardlink"] and report["hardlink_error"] == "EXDEV"
    assert report["copy"]
    assert all(not list(root.iterdir()) for root in roots)


def test_failed_probe_removes_owned_source_file(roots, monkeypatch):
    def fail(*args, **kwargs):
        raise publication.PublicationError("synthetic probe failure")

    monkeypatch.setattr(publication, "probe_destination", fail)
    source, target, stage = roots
    with pytest.raises(publication.PublicationError, match="synthetic probe failure"):
        publication.probe_download_folder(source, "", target, stage)
    assert all(not list(root.iterdir()) for root in roots)


def test_overlap_rejected_before_a_temporary_source_is_created(roots):
    source, _, stage = roots
    with pytest.raises(publication.PublicationError, match="overlap"):
        publication.probe_download_folder(source, "", source, stage)
    assert not list(source.iterdir())


def test_access_check_does_not_exercise_recovery_or_collision_protocols(roots, monkeypatch):
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "No locking or rename support")

    monkeypatch.setattr(publication.fcntl, "flock", unsupported)
    monkeypatch.setattr(publication, "native_no_replace", unsupported)
    monkeypatch.setattr(os, "rename", unsupported)
    monkeypatch.setattr(os, "fsync", unsupported)
    source, library, staging = roots
    staging.chmod(0o770)
    journals = staging.parent / "journals-not-created-by-connection-check"
    report = publication.probe_download_folder(source, "", library, staging, journal_root=journals)
    assert report["copy"] and report["hardlink"]
    assert not journals.exists()
    assert all(not list(root.iterdir()) for root in roots)


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("relative", ["", "completed"])
def test_readable_only_download_folder_qualifies_copy_without_touching_files(
    roots, read_only_downloads, code, relative
):
    source, library, staging = roots
    folder = source / relative
    if relative:
        folder.mkdir()
    original = folder / "existing.epub"
    original.write_bytes(b"existing download")
    before = original.stat()
    read_only_downloads(folder, code)
    report = publication.probe_download_folder(source, relative, library, staging)
    assert report["source_readable"] and not report["source_writable"]
    assert report["source_write_error"] == errno.errorcode[code]
    assert report["copy"] and not report["hardlink"]
    assert report["hardlink_error"] == "UNTESTED_READ_ONLY_SOURCE"
    assert original.stat() == before and original.read_bytes() == b"existing download"
    assert list(folder.iterdir()) == [original]
    assert not list(library.iterdir()) and not list(staging.iterdir())


def test_empty_read_only_folder_can_qualify_for_future_downloads(roots, read_only_downloads):
    source, library, staging = roots
    read_only_downloads(source)
    report = publication.probe_download_folder(source, "", library, staging)
    assert report["copy"] and not report["source_writable"]
    assert all(not list(root.iterdir()) for root in roots)


def test_read_only_fallback_does_not_accept_unreadable_source(
    roots, read_only_downloads, monkeypatch
):
    source, library, staging = roots
    read_only_downloads(source)
    original = os.scandir
    source_info = source.stat()

    def denied(path):
        if isinstance(path, int):
            info = os.fstat(path)
            if (info.st_dev, info.st_ino) == (source_info.st_dev, source_info.st_ino):
                raise PermissionError(errno.EACCES, "Cannot list download folder")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(PermissionError) as caught:
        publication.probe_download_folder(source, "", library, staging)
    assert caught.value.probe_report["failure_step"] == "reading the download folder"
    assert caught.value.probe_report["path"] == str(source)
    assert all(not list(root.iterdir()) for root in roots)


def test_source_mutating_route_still_requires_write_access(roots, read_only_downloads):
    source, library, staging = roots
    read_only_downloads(source)
    with pytest.raises(OSError) as caught:
        publication.probe_download_folder(source, "", library, staging, allow_read_only=False)
    assert caught.value.errno == errno.EROFS
    assert caught.value.probe_report["failure_step"] == "creating a temporary file"
    assert all(not list(root.iterdir()) for root in roots)


@pytest.fixture
def media(tmp_path):
    library = tmp_path.resolve() / "media" / "audiobooks"
    library.mkdir(parents=True)
    return library


def separate_filesystem(monkeypatch, *paths):
    real = destinations._device
    monkeypatch.setattr(destinations, "_device", lambda path: -1 if path in paths else real(path))


def test_valid_library_choice_creates_private_sibling_staging(media):
    staging = media.parent / STAGING_NAME
    check_library_route(media, staging, [])
    assert staging.is_dir() and staging.stat().st_mode & 0o777 == 0o700


def test_library_folder_mounted_on_its_own_asks_for_the_parent(media, monkeypatch):
    separate_filesystem(monkeypatch, media)
    with pytest.raises(InspectionError, match="different filesystem"):
        check_library_route(media, media.parent / STAGING_NAME, [])
    assert not (media.parent / STAGING_NAME).exists()


def test_missing_library_folder_names_the_container_path(media):
    missing = media.parent / "missing"
    with pytest.raises(InspectionError, match=re.escape(f"cannot find {missing} inside")):
        check_library_route(missing, media.parent / STAGING_NAME, [])


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_staging_parent_names_the_worker_uid(media):
    media.parent.chmod(0o555)
    try:
        with pytest.raises(InspectionError, match=rf"uid {os.geteuid()}\).*\(EACCES\)"):
            check_library_route(media, media.parent / STAGING_NAME, [])
    finally:
        media.parent.chmod(0o755)


def test_staging_on_another_filesystem_is_refused(media, monkeypatch):
    staging = media.parent / "stage"
    staging.mkdir(mode=0o700)
    separate_filesystem(monkeypatch, staging)
    with pytest.raises(InspectionError, match="different filesystem from"):
        check_library_route(media, staging, [])


def test_other_libraries_can_use_independent_filesystems(media, monkeypatch):
    ebooks = media.parent / "ebooks"
    ebooks.mkdir()
    check_library_route(media, media.parent / STAGING_NAME, [ebooks, media.parent / "gone"])
    separate_filesystem(monkeypatch, ebooks)
    check_library_route(media, media.parent / STAGING_NAME, [ebooks])


def test_staging_follows_the_library_unless_configured(media, monkeypatch):
    sibling = media.parent / STAGING_NAME
    assert choose_staging(media, None, None) == sibling
    assert choose_staging(media, Path("/configured"), sibling) == Path("/configured")
    # A saved path from an earlier, failed choice no longer pins staging.
    assert choose_staging(media, None, Path("/missing/.book-search-staging")) == sibling
    working = media.parent / "working"
    working.mkdir()
    assert choose_staging(media, None, working) == working
    separate_filesystem(monkeypatch, working)
    assert choose_staging(media, None, working) == sibling


def test_new_library_staging_does_not_require_its_parent(media):
    assert choose_staging(media, None, None, inside_library=True) == media / STAGING_NAME
    # Existing and explicitly configured storage retain their recovery protocol.
    sibling = media.parent / STAGING_NAME
    sibling.mkdir()
    assert choose_staging(media, None, sibling, inside_library=True) == sibling
    assert choose_staging(media, sibling, None, inside_library=True) == sibling


def test_staging_with_receipts_is_recognized(media):
    staging = media.parent / STAGING_NAME
    assert not holds_journals(staging)
    staging.mkdir(mode=0o700)
    (staging / "lock-probe").write_bytes(b"")
    assert not holds_journals(staging)
    (staging / "entry.json").write_text("{}")
    assert holds_journals(staging)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (errno.EACCES, f"Dewarr (uid {os.geteuid()}) was denied access to a folder"),
        (errno.ENOENT, "cannot find a folder inside its container"),
        (errno.EXDEV, "different filesystems"),
        (errno.EROFS, "Remove :ro"),
        (errno.ELOOP, "symbolic link"),
        (errno.EIO, "Destination probe failed; check paths"),
    ],
)
def test_os_errors_are_explained_with_their_code(code, expected):
    message = describe_os_error(OSError(code, os.strerror(code)))
    assert expected in message and message.endswith(f"({errno.errorcode[code]})")


@pytest.mark.parametrize("path", ["/library", "/audiobooks", "/shelf-42", "/Reading Room"])
def test_top_level_library_uses_private_child(path):
    library = Path(path)
    assert choose_staging(library, None, None) == library / STAGING_NAME


@pytest.mark.parametrize("boundary", ["device", "bind", "unwritable-parent"])
def test_library_only_mount_chooses_child_and_probes_real_files(media, monkeypatch, boundary):
    if boundary == "device":
        separate_filesystem(monkeypatch, media.parent)
    elif boundary == "bind":
        monkeypatch.setattr(destinations, "filesystem_mounts", lambda: [(media, "ext4", set())])
    else:
        monkeypatch.setattr(destinations.os, "access", lambda *args: False)
    staging = choose_staging(media, None, None)
    assert staging == media / STAGING_NAME
    check_library_route(media, staging, [])
    source = media.parent / "downloads"
    source.mkdir()
    report = publication.probe_download_folder(source, "", media, staging)
    assert report["hardlink"] and report["copy"]
    assert list(media.iterdir()) == [staging]
    assert not list(staging.iterdir()) and not list(source.iterdir())
    assert staging.stat().st_mode & 0o777 == 0o700
    assert choose_staging(media, None, staging) == staging


@pytest.mark.parametrize("obstacle", ["symlink", "file", "public-with-receipt"])
def test_existing_child_staging_is_never_replaced_or_chmodded(media, obstacle):
    staging = media / STAGING_NAME
    sentinel = media.parent / "keep"
    sentinel.write_text("keep")
    if obstacle == "symlink":
        staging.symlink_to(media.parent, target_is_directory=True)
    elif obstacle == "file":
        staging.write_text("keep")
    else:
        staging.mkdir(mode=0o755)
        # Empty, Dewarr-owned managed folders may now be secured automatically.
        # Existing public receipts must never become trusted through chmod alone.
        (staging / "receipt.json").write_text("keep")
    before = staging.lstat()
    with pytest.raises(InspectionError):
        check_library_route(media, staging, [])
    assert staging.lstat() == before
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("other_library", [False, True])
def test_same_device_different_bind_mount_is_rejected(media, monkeypatch, other_library):
    stage = media / STAGING_NAME
    other = media.parent / "other"
    other.mkdir()
    monkeypatch.setattr(destinations, "filesystem_mounts", lambda: [(media, "ext4", set())])
    if other_library:
        check_library_route(media, stage, [other])
    else:
        with pytest.raises(InspectionError, match="different mounts"):
            check_library_route(media, other / STAGING_NAME, [])
        assert not (other / STAGING_NAME).exists()


def test_repick_does_not_preserve_staging_that_became_part_of_library(media):
    previous = media / "old-incoming"
    previous.mkdir()
    assert choose_staging(media, None, previous) == media.parent / STAGING_NAME
    # An operator override is explicit, and is rejected with its path by layout validation.
    assert choose_staging(media, previous, None) == previous
