"""Filesystem behavior simulations for route qualification and publication."""

import errno
import os
import stat

import pytest

from app.importing import publication


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
