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


def test_failed_link_reports_copy_capability_without_claiming_a_hardlink(roots, monkeypatch):
    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")

    monkeypatch.setattr(publication.os, "link", cross_device)
    source, target, stage = roots
    report = publication.probe_download_folder(source, "", target, stage)
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
    with pytest.raises(InspectionError, match="Mount the parent folder instead"):
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


def test_libraries_on_different_filesystems_are_refused(media, monkeypatch):
    ebooks = media.parent / "ebooks"
    ebooks.mkdir()
    check_library_route(media, media.parent / STAGING_NAME, [ebooks, media.parent / "gone"])
    separate_filesystem(monkeypatch, ebooks)
    with pytest.raises(InspectionError, match="one staging folder for every library"):
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
