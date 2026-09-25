"""Filesystem behavior simulations for route qualification and publication."""

import errno
import os
import stat

import pytest

from app.importing import publication


@pytest.fixture
def read_only_downloads(monkeypatch):
    def configure(path, code=errno.EROFS):
        original = os.open
        identity = path.stat()

        def opening(name, flags, *args, **kwargs):
            parent = kwargs.get("dir_fd")
            if parent is not None and flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR):
                info = os.fstat(parent)
                if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                    raise OSError(code, "Download mount is read-only to Dewarr")
            return original(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", opening)

    return configure


@pytest.fixture
def smb_open_children(monkeypatch):
    """Windows SMB denies directory renames with open child files (NOR-67)."""
    real_open, real_close, real_dup = os.open, os.close, os.dup
    real_native, real_rename = publication.native_no_replace, os.rename
    opened = {}

    def track(fd):
        info = os.fstat(fd)
        opened[fd] = (info.st_dev, info.st_ino, stat.S_ISREG(info.st_mode))
        return fd

    def opening(*args, **kwargs):
        return track(real_open(*args, **kwargs))

    def duplicate(fd):
        return track(real_dup(fd))

    def closing(fd):
        opened.pop(fd, None)
        return real_close(fd)

    def check(source, source_fd):
        info = os.stat(source, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            return
        folder = real_open(source, os.O_RDONLY | os.O_DIRECTORY, dir_fd=source_fd)
        try:
            for name in os.listdir(folder):
                child = os.stat(name, dir_fd=folder, follow_symlinks=False)
                if (child.st_dev, child.st_ino, True) in opened.values():
                    raise PermissionError(errno.EACCES, "SMB directory has an open child file")
        finally:
            real_close(folder)

    def native(source_fd, source, destination_fd, destination):
        check(source, source_fd)
        return real_native(source_fd, source, destination_fd, destination)

    def rename(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        check(source, src_dir_fd)
        return real_rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "close", closing)
    monkeypatch.setattr(os, "dup", duplicate)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(publication, "native_no_replace", native)
    yield
    assert not opened


@pytest.fixture
def path_bound_directory_handles(monkeypatch):
    """A moved directory's old handle can no longer resolve child names (NOR-56)."""
    real_open, real_close, real_dup = os.open, os.close, os.dup
    real_move = publication.no_replace
    handles, stale = {}, set()

    def open_path(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") in stale:
            raise FileNotFoundError(errno.ENOENT, "Directory handle refers to its old path", path)
        fd = real_open(path, flags, *args, **kwargs)
        stale.discard(fd)
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            handles[fd] = (info.st_dev, info.st_ino)
        return fd

    def close(fd):
        handles.pop(fd, None)
        stale.discard(fd)
        return real_close(fd)

    def duplicate(fd):
        copied = real_dup(fd)
        if fd in handles:
            handles[copied] = handles[fd]
        if fd in stale:
            stale.add(copied)
        return copied

    def move(source_fd, source_name, destination_fd, destination_name):
        info = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
        result = real_move(source_fd, source_name, destination_fd, destination_name)
        if stat.S_ISDIR(info.st_mode):
            identity = (info.st_dev, info.st_ino)
            stale.update(fd for fd, item in handles.items() if item == identity)
        return result

    monkeypatch.setattr(publication.os, "open", open_path)
    monkeypatch.setattr(publication.os, "close", close)
    monkeypatch.setattr(publication.os, "dup", duplicate)
    monkeypatch.setattr(publication, "no_replace", move)
