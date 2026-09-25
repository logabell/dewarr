"""SMB operation boundaries that local POSIX filesystem journeys cannot exercise."""
# ruff: noqa: F811

import errno
import os

import pytest

from app.importing import publication
from tests.filesystem_fixtures import smb_open_children  # noqa: F401
from tests.unit.test_import_publication import specification  # noqa: F401
from tests.unit.test_setup_probe_files import roots  # noqa: F401


@pytest.mark.usefixtures("smb_open_children")
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_smb_folder_probe_closes_children_before_native_or_fallback_rename(
    roots, monkeypatch, protected, fallback
):
    if fallback:

        def unsupported(*args):
            raise OSError(errno.EOPNOTSUPP, "Native no-replace unavailable")

        monkeypatch.setattr(publication, "native_no_replace", unsupported)
    source, library, staging = roots
    journals = staging.parent / "journals" if protected else None
    if journals:
        journals.mkdir(mode=0o700)
        staging.chmod(0o777)
    report = publication.probe_download_folder(source, "", library, staging, journal_root=journals)
    assert report["copy"] and report["no_replace"]
    assert report["no_replace_mode"] == ("fallback" if fallback else "native")
    assert all(not list(root.iterdir()) for root in roots)
    if journals:
        assert not list(journals.iterdir())


@pytest.mark.usefixtures("smb_open_children")
@pytest.mark.parametrize("mode", ["copy", "hardlink"])
def test_real_import_closes_children_before_publication(specification, mode):
    spec = specification.model_copy(update={"mode": mode})
    before = (spec.source_root / "pack/book.epub").read_bytes()
    result = publication.publish_item(spec)
    assert result["state"] == "published"
    assert (spec.destination_root / spec.folder / "First Harbor.epub").read_bytes() == before
    assert (spec.source_root / "pack/book.epub").read_bytes() == before


def test_native_route_does_not_require_plain_rename_collision_semantics(roots, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "SMB refused directory replacement")

    monkeypatch.setattr(os, "rename", denied)
    report = publication.probe_download_folder(roots[0], "", roots[1], roots[2])
    assert report["no_replace"] and report["no_replace_mode"] == "native"
    assert all(not list(root.iterdir()) for root in roots)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EBUSY])
def test_library_access_failure_is_not_downgraded_to_fallback(roots, monkeypatch, code):
    native = publication.native_no_replace

    def denied(source_fd, source, destination_fd, destination):
        if source.startswith("probe-"):
            raise OSError(code, "Server refused publication")
        return native(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(publication, "native_no_replace", denied)
    with pytest.raises(OSError) as caught:
        publication.probe_download_folder(roots[0], "", roots[1], roots[2])
    assert caught.value.errno == code
    assert caught.value.probe_report["failure_step"] == "checking safe library publication"
    assert all(not list(root.iterdir()) for root in roots)


def test_unpinned_marker_changed_before_failed_rename_is_preserved(roots, monkeypatch):
    native = publication.native_no_replace
    source, library, staging = roots
    changed = []

    def replace(source_fd, name, destination_fd, destination):
        if name.startswith("probe-"):
            marker = staging / name / "marker"
            # Even a same-inode rewrite must not be deleted after releasing the pin.
            marker.write_bytes(b"foreign contents")
            changed.append(marker)
            raise PermissionError(errno.EACCES, "Server refused publication")
        return native(source_fd, name, destination_fd, destination)

    monkeypatch.setattr(publication, "native_no_replace", replace)
    with pytest.raises(PermissionError) as caught:
        publication.probe_download_folder(source, "", library, staging)
    assert caught.value.probe_report["cleanup_failures"]
    assert changed[0].read_bytes() == b"foreign contents"
    assert list(staging.iterdir()) == [changed[0].parent]
    assert not list(source.iterdir()) and not list(library.iterdir())
