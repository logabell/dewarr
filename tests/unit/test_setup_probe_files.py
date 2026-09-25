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
from tests.filesystem_fixtures import path_bound_directory_handles  # noqa: F401


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
    assert all(report["hardlink"] and report["no_replace"] and report["copy"] for report in reports)
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
    assert report["copy"] and report["no_replace"]
    assert all(not list(root.iterdir()) for root in roots)


def test_setup_probe_freezes_identity_after_the_writer_is_closed(roots, monkeypatch):
    """SMB can finalize mtime on reopening/closing a newly written file."""
    source, target, stage = roots
    real_open, real_close = os.open, os.close
    writers = {}

    def settle(path):
        if path.exists():
            observed = path.stat()
            os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))

    def opening(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).startswith(".book-search-route-"):
            full_path = source / path
            if flags & os.O_CREAT:
                writers[fd] = full_path
            elif full_path in writers.values():
                settle(full_path)
        return fd

    def closing(fd):
        path = writers.pop(fd, None)
        if path is not None:
            settle(path)
        return real_close(fd)

    monkeypatch.setattr(publication.os, "open", opening)
    monkeypatch.setattr(publication.os, "close", closing)
    report = publication.probe_download_folder(source, "", target, stage)
    assert report["copy"] and report["no_replace"]
    assert not writers
    assert all(not list(root.iterdir()) for root in roots)


def test_writer_close_error_does_not_retry_a_reused_descriptor(roots, monkeypatch):
    source, target, stage = roots
    real_open, real_close = os.open, os.close
    writer = None
    replacement = None

    def opening(path, flags, *args, **kwargs):
        nonlocal writer
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).startswith(".book-search-route-") and flags & os.O_CREAT:
            writer = fd
        return fd

    def closing(fd):
        nonlocal replacement
        if fd == writer and replacement is None:
            real_close(fd)
            replacement = real_open(os.devnull, os.O_RDONLY)
            assert replacement == fd
            raise OSError(errno.EIO, "synthetic close error after descriptor release")
        return real_close(fd)

    monkeypatch.setattr(publication.os, "open", opening)
    monkeypatch.setattr(publication.os, "close", closing)
    try:
        with pytest.raises(OSError, match="synthetic close error"):
            publication.probe_download_folder(source, "", target, stage)
        assert replacement is not None
        os.fstat(replacement)  # A second close must not consume this unrelated FD.
        assert all(not list(root.iterdir()) for root in roots)
    finally:
        if replacement is not None:
            try:
                real_close(replacement)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def test_failed_probe_removes_owned_source_file(roots, monkeypatch):
    def fail(*args, **kwargs):
        raise publication.PublicationError("synthetic probe failure")

    monkeypatch.setattr(publication, "probe_destination", fail)
    source, target, stage = roots
    with pytest.raises(publication.PublicationError, match="synthetic probe failure"):
        publication.probe_download_folder(source, "", target, stage)
    assert all(not list(root.iterdir()) for root in roots)


def test_unrecognized_source_replacement_is_preserved(roots, monkeypatch):
    def replace(source, relative, *args, **kwargs):
        path = source / relative
        path.unlink()
        path.write_bytes(b"replacement must survive")
        return {"hardlink": True}

    monkeypatch.setattr(publication, "probe_destination", replace)
    source, target, stage = roots
    with pytest.raises(publication.PublicationError, match="replacement preserved"):
        publication.probe_download_folder(source, "", target, stage)
    remaining = list(source.iterdir())
    assert len(remaining) == 1 and remaining[0].read_bytes() == b"replacement must survive"


def test_overlap_rejected_before_a_temporary_source_is_created(roots):
    source, _, stage = roots
    with pytest.raises(publication.PublicationError, match="overlap"):
        publication.probe_download_folder(source, "", source, stage)
    assert not list(source.iterdir())


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


@pytest.mark.usefixtures("path_bound_directory_handles")
def test_probe_copy_fallback_does_not_use_a_moved_directory_handle(roots, monkeypatch):
    def unsupported(*args, **kwargs):
        raise OSError(errno.EPERM, "Hardlinks not supported")

    monkeypatch.setattr(publication.os, "link", unsupported)
    source, target, stage = roots
    result = publication.probe_download_folder(source, "", target, stage)
    assert not result["hardlink"] and result["hardlink_error"] == "EPERM"
    assert result["copy"] and result["no_replace"]
    assert all(not list(root.iterdir()) for root in roots)


def test_probe_error_preserves_failed_operation_and_cleans_up(roots, monkeypatch):
    real_move = publication.no_replace

    def fail_library_move(source_fd, source_name, destination_fd, destination_name):
        if source_name.startswith("probe-"):
            raise OSError(errno.ENOENT, "Synthetic rename failure")
        return real_move(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(publication, "no_replace", fail_library_move)
    source, target, stage = roots
    with pytest.raises(OSError) as caught:
        publication.probe_download_folder(source, "", target, stage)
    assert caught.value.errno == errno.ENOENT
    assert caught.value.probe_report["failure_step"] == "checking safe library publication"
    assert caught.value.probe_report["error_code"] == "ENOENT"
    assert caught.value.probe_report["copy"]
    assert all(not list(root.iterdir()) for root in roots)


@pytest.mark.usefixtures("deferred_unlink")
def test_incomplete_probe_marker_keeps_original_error_and_cleans_owned_files(roots, monkeypatch):
    real_write = publication.write_all

    def fail_marker(fd, content):
        if content.startswith(b"book-search destination probe "):
            os.write(fd, content[:4])
            raise OSError(errno.ENOSPC, "Synthetic full disk")
        return real_write(fd, content)

    monkeypatch.setattr(publication, "write_all", fail_marker)
    source, target, stage = roots
    with pytest.raises(OSError) as caught:
        publication.probe_download_folder(source, "", target, stage)
    assert caught.value.errno == errno.ENOSPC
    assert caught.value.probe_report["error_code"] == "ENOSPC"
    assert all(not list(root.iterdir()) for root in roots)


@pytest.fixture
def deferred_unlink(monkeypatch):
    """Model NFS silly-rename: an unlinked open file remains until its last close."""
    real_open, real_close, real_dup = os.open, os.close, os.dup
    real_unlink, real_rename = os.unlink, os.rename
    opened, pending = {}, {}

    def track(fd):
        info = os.fstat(fd)
        opened[fd] = (info.st_dev, info.st_ino)
        return fd

    def opening(*args, **kwargs):
        return track(real_open(*args, **kwargs))

    def duplicate(fd):
        return track(real_dup(fd))

    def closing(fd):
        key = opened.pop(fd, None)
        real_close(fd)
        if key in pending and key not in opened.values():
            parent, name = pending.pop(key)
            real_unlink(name, dir_fd=parent)
            real_close(parent)

    def unlinking(path, *, dir_fd=None):
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        key = (info.st_dev, info.st_ino)
        if key in opened.values() and info.st_nlink == 1:
            name = f".nfs-test-{info.st_ino}"
            real_rename(path, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            pending[key] = (real_dup(dir_fd), name)
        else:
            real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(publication.os, "open", opening)
    monkeypatch.setattr(publication.os, "dup", duplicate)
    monkeypatch.setattr(publication.os, "close", closing)
    monkeypatch.setattr(publication.os, "unlink", unlinking)
    yield
    assert not pending
    assert not opened


@pytest.mark.usefixtures("deferred_unlink")
@pytest.mark.parametrize("fallback", [False, True])
def test_probe_closes_test_files_before_removing_network_folders(roots, monkeypatch, fallback):
    if fallback:

        def unsupported(*args):
            raise OSError(errno.EOPNOTSUPP, "No native no-replace rename")

        monkeypatch.setattr(publication, "native_no_replace", unsupported)
    source, target, stage = roots
    report = publication.probe_download_folder(source, "", target, stage)
    assert report["copy"] and report["no_replace"]
    assert all(not list(root.iterdir()) for root in roots)


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOTEMPTY])
def test_probe_cleanup_failure_names_the_operation_and_preserves_files(roots, monkeypatch, code):
    source, target, stage = roots
    real_rmdir = os.rmdir
    preserved = []

    def cannot_remove(path, *, dir_fd=None):
        if str(path).startswith(".book-search-probe-"):
            folder = target / path
            if code == errno.ENOTEMPTY:
                extra = folder / "another-app.txt"
                extra.write_text("preserve this")
                preserved.append(extra)
            raise OSError(code, os.strerror(code))
        return real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(publication.os, "rmdir", cannot_remove)
    with pytest.raises(InspectionError) as caught:
        publication.probe_download_folder(source, "", target, stage)
    report = caught.value.probe_report
    assert report["failure_step"] == "cleaning up temporary probe files"
    assert report["error_code"] == errno.errorcode[code]
    assert str(target) in str(caught.value)
    assert "object changed" not in str(caught.value)
    assert all(path.read_text() == "preserve this" for path in preserved)
    assert not list(source.iterdir()) and not list(stage.iterdir())
    assert len(list(target.iterdir())) == 1


def test_probe_cleanup_does_not_mask_an_earlier_failure(roots, monkeypatch):
    source, target, stage = roots
    real_unlink = os.unlink

    def failed_move(*args):
        raise OSError(errno.ENOSPC, "Disk full during publication")

    def failed_cleanup(path, *, dir_fd=None):
        if str(path).startswith("write-"):
            raise OSError(errno.EACCES, "Cleanup permission denied")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(publication, "no_replace", failed_move)
    monkeypatch.setattr(publication.os, "unlink", failed_cleanup)
    with pytest.raises(OSError) as caught:
        publication.probe_download_folder(source, "", target, stage)
    assert caught.value.errno == errno.ENOSPC
    assert caught.value.probe_report["failure_step"] == "checking safe journal creation"
    assert caught.value.probe_report["error_code"] == "ENOSPC"
    assert caught.value.probe_report["cleanup_failures"][0]["error_code"] == "EACCES"
    assert len(list(stage.iterdir())) == 1
    assert not list(source.iterdir()) and not list(target.iterdir())


@pytest.mark.parametrize("path", ["/library", "/audiobooks"])
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
    assert report["no_replace"] and report["hardlink"] and report["copy"]
    assert list(media.iterdir()) == [staging]
    assert not list(staging.iterdir()) and not list(source.iterdir())
    assert staging.stat().st_mode & 0o777 == 0o700
    assert choose_staging(media, None, staging) == staging


@pytest.mark.parametrize("obstacle", ["symlink", "file", "public"])
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
