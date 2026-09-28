import errno
import os
import stat
import struct
import sys

import pytest

from app.importing.destinations import prepare_staging
from app.importing.publication import PublicationError, prepare_journals, private_staging


@pytest.mark.parametrize("existing", [False, True])
def test_managed_legacy_staging_repairs_inherited_permissions(tmp_path, monkeypatch, existing):
    stage = tmp_path.resolve() / ".book-search-staging"
    real_mkdir = os.mkdir

    def inherited_mode(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        os.chmod(path, 0o777, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", inherited_mode)
    if existing:
        stage.mkdir()
    parent_mode = stat.S_IMODE(stage.parent.stat().st_mode)
    prepare_staging(stage)
    with private_staging(stage):
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    prepare_staging(stage)
    assert stat.S_IMODE(stage.parent.stat().st_mode) == parent_mode


@pytest.mark.parametrize("failure", ["ignored", "denied"])
def test_managed_staging_fails_closed_when_nas_cannot_set_permissions(
    tmp_path, monkeypatch, failure
):
    stage = tmp_path.resolve() / ".book-search-staging"
    stage.mkdir()
    stage.chmod(0o777)

    def chmod(fd, mode):
        if failure == "denied":
            raise PermissionError(errno.EPERM, "ACL denied")

    monkeypatch.setattr(os, "fchmod", chmod)
    with pytest.raises(PublicationError, match="0700"):
        prepare_staging(stage)
    assert stat.S_IMODE(stage.stat().st_mode) == 0o777


@pytest.mark.parametrize("obstacle", ["symlink", "nonempty", "foreign-owner", "explicit"])
def test_permission_repair_never_changes_untrusted_or_operator_folders(
    tmp_path, monkeypatch, obstacle
):
    root = tmp_path.resolve()
    stage = root / ("operator-stage" if obstacle == "explicit" else ".book-search-staging")
    target = root / "unrelated"
    if obstacle == "symlink":
        target.mkdir()
        target.chmod(0o777)
        stage.symlink_to(target, target_is_directory=True)
    else:
        stage.mkdir()
        stage.chmod(0o777)
    if obstacle == "nonempty":
        (stage / "receipt.json").write_text("untrusted receipt")
    if obstacle == "foreign-owner":
        monkeypatch.setattr(os, "geteuid", lambda: stage.stat().st_uid + 1)
    if obstacle == "explicit":
        prepare_staging(stage)
    else:
        with pytest.raises((OSError, PublicationError)):
            prepare_staging(stage)
    assert stat.S_IMODE(stage.stat().st_mode) == 0o777
    if obstacle == "nonempty":
        assert (stage / "receipt.json").read_text() == "untrusted receipt"


def test_new_journals_constrain_inherited_permissions_but_existing_journals_stay_strict(
    tmp_path, monkeypatch
):
    journals = tmp_path.resolve() / "journals"
    real_mkdir = os.mkdir

    def inherited_mode(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        os.chmod(path, 0o777, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", inherited_mode)
    prepare_journals(journals)
    assert stat.S_IMODE(journals.stat().st_mode) == 0o700
    journals.chmod(0o777)
    with pytest.raises(PublicationError, match="0700"):
        prepare_journals(journals)


def test_media_only_staging_keeps_shared_permissions(tmp_path):
    stage = tmp_path.resolve() / ".book-search-staging"
    stage.mkdir()
    stage.chmod(0o777)
    journals = tmp_path.resolve() / "journals"
    prepare_staging(stage, journals)
    prepare_journals(journals)
    with private_staging(stage, journals):
        assert stat.S_IMODE(stage.stat().st_mode) == 0o777


def test_private_legacy_directory_with_setgid_keeps_recovery_files(tmp_path):
    stage = tmp_path.resolve() / ".book-search-staging"
    stage.mkdir()
    stage.chmod(0o2700)
    receipt = stage / "receipt.json"
    receipt.write_text("existing private receipt")
    prepare_staging(stage)
    with private_staging(stage):
        assert stat.S_IMODE(stage.stat().st_mode) == 0o2700
        assert receipt.read_text() == "existing private receipt"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX ACL xattrs")
def test_linux_inherited_acl_is_masked_without_changing_library_acl(tmp_path):
    root = tmp_path.resolve()
    # Linux POSIX ACL xattr format: version, then tag/permissions/qualifier entries.
    entries = [
        (1, 7, 0xFFFFFFFF),
        (2, 7, 65534),
        (4, 7, 0xFFFFFFFF),
        (16, 7, 0xFFFFFFFF),
        (32, 7, 0xFFFFFFFF),
    ]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(root, "system.posix_acl_default", acl)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("Test filesystem does not support POSIX ACLs")
        raise
    stage = root / ".book-search-staging"
    stage.mkdir(mode=0o777)
    assert stat.S_IMODE(stage.stat().st_mode) == 0o777
    prepare_staging(stage)
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    access = os.getxattr(stage, "system.posix_acl_access")
    assert next(perms for tag, perms, _ in struct.iter_unpack("<HHI", access[4:]) if tag == 16) == 0
    assert os.getxattr(root, "system.posix_acl_default") == acl
