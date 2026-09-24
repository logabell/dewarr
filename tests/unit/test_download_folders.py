from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.domain import download_folders


def test_volume_discovery_excludes_system_and_config_mounts(tmp_path):
    mounts = tmp_path / "mountinfo"
    mounts.write_text(
        "1 0 0:1 / / rw - overlay overlay rw\n"
        "2 1 0:2 / /etc/hosts rw - ext4 disk rw\n"
        "3 1 0:2 / /config rw - ext4 disk rw\n"
        "4 1 0:2 / /downloads\\040books rw - ext4 disk rw\n"
        "5 1 0:2 / /mnt/torrents rw - ext4 disk rw\n"
    )
    assert download_folders.volume_roots(mounts) == {
        Path("/data"),
        Path("/downloads books"),
        Path("/mnt/torrents"),
    }


def test_browse_confines_paths_hides_files_and_symlinks(monkeypatch, tmp_path):
    volume = tmp_path / "volume"
    downloads = volume / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "book.epub").touch()
    (downloads / ".hidden").mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (downloads / "escape").symlink_to(outside, target_is_directory=True)
    (volume / "alias").symlink_to(downloads, target_is_directory=True)
    monkeypatch.setattr(download_folders, "volume_roots", lambda: {volume})
    settings = SimpleNamespace(import_sources={"books": downloads})
    assert download_folders.browse_folders(settings)["directories"] == [str(volume)]
    assert download_folders.browse_folders(settings, str(volume))["parent"] is None
    assert download_folders.browse_folders(settings, str(downloads)) == {
        "path": str(downloads),
        "parent": str(volume),
        "directories": [],
        "truncated": False,
    }
    for path in [
        outside,
        volume / "alias",
        downloads / "escape",
        volume / "../private",
        volume / "missing",
        downloads / "book.epub",
    ]:
        with pytest.raises(HTTPException) as error:
            download_folders.browse_folders(settings, str(path))
        assert error.value.status_code == 422


def test_directory_listing_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(download_folders, "volume_roots", lambda: {tmp_path})
    monkeypatch.setattr(download_folders, "MAX_ENTRIES", 2)
    for index in range(4):
        (tmp_path / str(index)).mkdir()
    result = download_folders.browse_folders(SimpleNamespace(import_sources={}), str(tmp_path))
    assert result["truncated"] and len(result["directories"]) == 2


def test_library_browser_includes_configured_library_roots(monkeypatch, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(download_folders, "volume_roots", lambda: set())
    settings = SimpleNamespace(import_sources={}, import_destinations={"ebooks": library})
    assert download_folders.browse_folders(settings)["directories"] == []
    assert download_folders.browse_folders(settings, include_libraries=True)["directories"] == [
        str(library)
    ]
