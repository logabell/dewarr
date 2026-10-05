"""Download picker must never broaden the configured read-only root."""

from pathlib import Path

import pytest

from app.importing.browsing import browse_downloads


def test_download_browser_lists_files_and_folders_without_following_links(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "book").mkdir()
    (root / "book" / "chapter.m4b").write_bytes(b"audio")
    (root / "read.epub").write_bytes(b"epub")
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    result = browse_downloads(root)
    assert [entry["name"] for entry in result["entries"]] == ["book", "read.epub"]
    assert result["parent"] is None
    nested = browse_downloads(root, "book")
    assert nested["parent"] == ""
    assert nested["entries"][0] == {
        "name": "chapter.m4b",
        "path": "book/chapter.m4b",
        "kind": "file",
        "size": 5,
    }
    with pytest.raises((OSError, ValueError)):
        browse_downloads(root, "escape")


@pytest.mark.parametrize("path", ["../outside", "/etc", "book/../outside", "book//nested", "."])
def test_download_browser_rejects_invalid_relative_paths(tmp_path, path):
    with pytest.raises((OSError, ValueError)):
        browse_downloads(tmp_path, path)


def test_download_browser_limits_work_and_rejects_root_symlinks(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    for n in range(5):
        (root / str(n)).touch()
    result = browse_downloads(root, limit=2)
    assert len(result["entries"]) == 2
    assert result["truncated"]
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        browse_downloads(linked)
    with pytest.raises(ValueError):
        browse_downloads(Path("/"))
