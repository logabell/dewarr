import copy

import httpx
import pytest

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError, FailureKind
from app.importing.backend import verify_backend
from app.importing.publication import PublicationError
from tests.abs_import_fixture import ImportBackendFixture


async def test_library_validation_needs_no_upload_permission_or_remote_filesystem_probe(tmp_path):
    fixture = ImportBackendFixture(tmp_path.resolve(), backend_path="/data/media/Audiobooks")
    fixture.user_type = "user"

    async def respond(request):
        if request.url.path == "/api/filesystem/pathexists":
            return httpx.Response(403)
        return await fixture.handle(request)

    async with Audiobookshelf(
        "http://fixture", "private-import-token", transport=httpx.MockTransport(respond)
    ) as adapter:
        result = await verify_backend(
            adapter, "synthetic", fixture.backend_path, fixture.root, "audio"
        )
    assert result["configuration_validated"]
    assert not result["scan_capable"] and result["watcher_enabled"]
    assert "root_mapping" not in result  # Backend settings do not prove shared filesystem identity.
    assert not list(fixture.root.iterdir())


@pytest.mark.parametrize(
    "status, kind",
    [
        (401, FailureKind.AUTHENTICATION),
        (403, FailureKind.PERMISSION),
        (503, FailureKind.UNAVAILABLE),
    ],
)
async def test_library_access_failures_still_block_validation(tmp_path, status, kind):
    fixture = ImportBackendFixture(tmp_path.resolve())

    async def respond(request):
        if request.url.path == "/api/libraries/synthetic":
            return httpx.Response(status)
        return await fixture.handle(request)

    async with Audiobookshelf(
        "http://fixture", "private-import-token", transport=httpx.MockTransport(respond)
    ) as adapter:
        with pytest.raises(AdapterError) as error:
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert error.value.kind == kind
    assert not list(fixture.root.iterdir())


async def test_library_configuration_change_during_validation_is_rejected(tmp_path):
    fixture = ImportBackendFixture(tmp_path.resolve())
    reads = 0

    async def change():
        nonlocal reads
        reads += 1
        if reads == 2:
            fixture.settings["disableWatcher"] = True

    fixture.before_library = change
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError, match="settings changed"):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")


@pytest.mark.parametrize("condition", ["audio-only", "precedence", "no-detection", "wrong-root"])
async def test_incompatible_backend_settings_are_rejected(tmp_path, condition):
    fixture = ImportBackendFixture(tmp_path.resolve())
    root = "/books"
    if condition == "audio-only":
        fixture.settings["audiobooksOnly"] = True
    elif condition == "precedence":
        fixture.settings["metadataPrecedence"].reverse()
    elif condition == "no-detection":
        fixture.user_type = "user"
        fixture.settings["disableWatcher"] = True
    else:
        root = "/different"
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError):
            await verify_backend(adapter, "synthetic", root, fixture.root, "ebook")
    assert not list(fixture.root.iterdir())


@pytest.mark.parametrize("version", ["2.19.1", "2.36.1", "2.41.0"])
async def test_other_audiobookshelf_releases_still_validate_settings(tmp_path, version):
    fixture = ImportBackendFixture(tmp_path.resolve())
    fixture.version = version
    async with fixture.client() as adapter:
        result = await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert result["version"] == version and result["configuration_validated"]
    assert not list(fixture.root.iterdir())


async def test_malformed_import_settings_do_not_get_default_capabilities():
    valid = {
        "id": "synthetic",
        "mediaType": "book",
        "folders": [{"fullPath": "/books"}],
        "settings": {
            "audiobooksOnly": False,
            "disableWatcher": False,
            "metadataPrecedence": ["opfFile"],
        },
    }
    for key, value in [
        ("settings", {"disableWatcher": "false"}),
        ("settings", {"audiobooksOnly": 0}),
        ("settings", {"metadataPrecedence": "opfFile"}),
        ("settings", ["opfFile"]),
        ("folders", [{"fullPath": "/books/../private"}]),
        ("id", "different"),
    ]:
        data = copy.deepcopy(valid)
        data[key] = value
        async with Audiobookshelf(
            "http://fixture",
            transport=httpx.MockTransport(lambda _, data=data: httpx.Response(200, json=data)),
        ) as adapter:
            with pytest.raises(AdapterError):
                await adapter.import_configuration("synthetic")


async def test_opf_must_not_overwrite_native_abs_edits(tmp_path):
    fixture = ImportBackendFixture(tmp_path.resolve())
    fixture.settings["metadataPrecedence"] = [
        "folderStructure",
        "audioMetatags",
        "absMetadata",
        "opfFile",
    ]
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError, match="edits made in ABS survive"):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert not list(fixture.root.iterdir())
