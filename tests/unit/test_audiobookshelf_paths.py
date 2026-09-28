from pathlib import Path, PurePosixPath

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.audiobookshelf import Audiobookshelf, backend_path
from app.adapters.contracts import AdapterError
from app.api.destinations import DestinationInput
from app.api.library_folders import FolderInput
from app.importing.filesystem import InspectionError, directory


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/books", "/books"),
        ("D:/Books/Audiobooks", "D:/Books/Audiobooks"),
        ("D:\\Books\\Audiobooks", "D:/Books/Audiobooks"),
        ("D:/", "D:/"),
        ("D:\\", "D:/"),
        ("//media/share/Books", "\\\\media/share/Books"),
        ("\\\\media\\share\\Books", "\\\\media/share/Books"),
        ("\\\\media/share/Books", "\\\\media/share/Books"),
    ],
)
def test_backend_path_accepts_posix_drive_and_unc_roots(value, expected):
    assert backend_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "relative",
        "/",
        "/books/../private",
        "/books//bad",
        "D:",
        "D:/Books/../Windows",
        "D:/Books//Audiobooks",
        "//media",
        "//media/share/../secret",
        "\\\\media",
    ],
)
def test_backend_path_rejects_unconfined_roots(value):
    with pytest.raises(ValueError, match="confined absolute path"):
        backend_path(value)


def test_stored_windows_roots_join_like_audiobookshelf():
    drive = backend_path("D:\\Books\\Audiobooks")
    unc = backend_path("\\\\media\\share\\Books")
    assert str(PurePosixPath(drive) / "Author/Title") == "D:/Books/Audiobooks/Author/Title"
    assert str(PurePosixPath("D:/") / "Author/Title") == "D:/Author/Title"
    assert str(PurePosixPath(unc) / "Author/Title") == "\\\\media/share/Books/Author/Title"


async def test_windows_full_path_is_listed_as_a_library_folder():
    payload = {
        "id": "synthetic",
        "mediaType": "book",
        "folders": [{"fullPath": "D:\\Books\\Audiobooks"}],
        "settings": {
            "audiobooksOnly": False,
            "disableWatcher": False,
            "metadataPrecedence": ["opfFile"],
        },
    }

    def respond(_request):
        return httpx.Response(200, json=payload)

    async with Audiobookshelf("http://fixture", transport=httpx.MockTransport(respond)) as adapter:
        configuration = await adapter.import_configuration("synthetic")
    assert configuration.folders == ["D:/Books/Audiobooks"]


@pytest.mark.parametrize(
    "settings",
    [
        {"coverAspectRatio": 1},
        {"audiobooksOnly": None, "disableWatcher": None, "metadataPrecedence": None},
        None,
    ],
)
async def test_library_settings_missing_from_older_abs_use_abs_defaults(settings):
    payload = {
        "id": "synthetic",
        "mediaType": "book",
        "folders": [{"id": "folder", "fullPath": "/audiobooks"}],
        "settings": settings,
    }

    def respond(_request):
        return httpx.Response(200, json=payload)

    async with Audiobookshelf("http://fixture", transport=httpx.MockTransport(respond)) as adapter:
        configuration = await adapter.import_configuration("synthetic")
    assert configuration.folders == ["/audiobooks"]
    assert configuration.audiobooks_only is False
    assert configuration.watcher_enabled is True
    assert configuration.metadata_precedence == [
        "folderStructure",
        "audioMetatags",
        "nfoFile",
        "txtFiles",
        "opfFile",
        "absMetadata",
    ]


async def test_unusable_folder_path_names_the_folder():
    payload = {
        "id": "synthetic",
        "mediaType": "book",
        "folders": [{"fullPath": "/audiobooks//nested"}],
        "settings": {},
    }

    def respond(_request):
        return httpx.Response(200, json=payload)

    async with Audiobookshelf("http://fixture", transport=httpx.MockTransport(respond)) as adapter:
        with pytest.raises(AdapterError, match="'/audiobooks//nested'"):
            await adapter.import_configuration("synthetic")


def test_folder_choice_keeps_the_windows_root_and_a_posix_mount():
    library_id = "11111111-1111-1111-1111-111111111111"
    chosen = FolderInput(
        library_id=library_id,
        backend_path="D:\\Books\\Audiobooks",
        local_path="  /data/audiobooks  ",
    )
    assert chosen.backend_path == "D:/Books/Audiobooks"
    assert chosen.local_path == "/data/audiobooks"
    with pytest.raises(ValidationError):
        FolderInput(
            library_id=library_id,
            backend_path="D:/Books/Audiobooks",
            local_path="D:/Books/Audiobooks",
        )
    with pytest.raises(ValidationError):
        FolderInput(
            library_id=library_id,
            backend_path="\\\\media/share/Books",
            local_path="//media/share/Books",
        )


def test_unc_shaped_root_is_not_opened_as_a_local_folder():
    with pytest.raises(InspectionError, match="absolute download root"):
        with directory(Path("//media/share/Books")):
            pass


def test_destination_input_accepts_the_windows_root_audiobookshelf_reports():
    saved = DestinationInput(
        library_id="11111111-1111-1111-1111-111111111111",
        medium="audio",
        backend_path="D:/Books/Audiobooks",
    )
    assert saved.backend_path == "D:/Books/Audiobooks"
    with pytest.raises(ValidationError):
        DestinationInput(
            library_id=saved.library_id,
            medium="audio",
            backend_path="/books/../private",
        )
