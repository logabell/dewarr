import asyncio
import copy

import httpx
import pytest

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError
from app.importing import backend
from app.importing.backend import verify_backend
from app.importing.publication import PublicationError
from tests.abs_import_fixture import ImportBackendFixture


async def test_actual_folder_mapping_challenge_with_watcher_only_token(tmp_path):
    fixture = ImportBackendFixture(tmp_path.resolve())
    fixture.user_type = "user"
    async with fixture.client() as adapter:
        result = await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert result["root_mapping"] and not result["scan_capable"] and result["watcher_enabled"]
    assert fixture.path_checks == [False, True, False]
    assert not list(fixture.root.iterdir())


async def test_mapping_waits_for_cached_creation_and_removal(tmp_path, monkeypatch):
    fixture = ImportBackendFixture(tmp_path.resolve())
    monkeypatch.setattr(backend, "MAPPING_POLL_INTERVAL", 0, raising=False)
    observed = []
    async with fixture.client() as adapter:
        real_exists = adapter.path_exists
        previous = False

        async def cached_exists(root, name):
            nonlocal previous
            current = await real_exists(root, name)
            result, previous = previous, current
            observed.append(result)
            return result

        monkeypatch.setattr(adapter, "path_exists", cached_exists)
        result = await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert result["root_mapping"]
    assert observed == [False, False, True, True, False]
    assert not list(fixture.root.iterdir())


@pytest.mark.parametrize("stale_phase", ["creation", "removal"])
async def test_mapping_cache_deadline_fails_closed(tmp_path, monkeypatch, stale_phase):
    fixture = ImportBackendFixture(tmp_path.resolve())
    monkeypatch.setattr(backend, "MAPPING_VISIBILITY_TIMEOUT", 0.02)
    monkeypatch.setattr(backend, "MAPPING_POLL_INTERVAL", 0.001)
    calls = []
    async with fixture.client() as adapter:
        real_exists = adapter.path_exists

        async def stale_exists(root, name):
            current = await real_exists(root, name)
            calls.append(current)
            return False if stale_phase == "creation" else len(calls) > 1

        monkeypatch.setattr(adapter, "path_exists", stale_exists)
        message = "same library folder" if stale_phase == "creation" else "removed challenge"
        with pytest.raises(PublicationError, match=message):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert len(calls) > 2
    assert not list(fixture.root.iterdir())


async def test_existing_mapping_challenge_is_never_polled_or_modified(tmp_path, monkeypatch):
    fixture = ImportBackendFixture(tmp_path.resolve())
    calls = []
    async with fixture.client() as adapter:

        async def exists(root, name):
            calls.append(name)
            return True

        monkeypatch.setattr(adapter, "path_exists", exists)
        with pytest.raises(PublicationError, match="Unexpected existing"):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert len(calls) == 1
    assert not list(fixture.root.iterdir())


async def test_cancelled_mapping_poll_cleans_its_marker(tmp_path, monkeypatch):
    fixture = ImportBackendFixture(tmp_path.resolve())
    async with fixture.client() as adapter:
        real_exists = adapter.path_exists

        async def cancel_when_created(root, name):
            if await real_exists(root, name):
                raise asyncio.CancelledError
            return False

        monkeypatch.setattr(adapter, "path_exists", cancel_when_created)
        with pytest.raises(asyncio.CancelledError):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert not list(fixture.root.iterdir())


@pytest.mark.parametrize("condition", ["audio-only", "precedence", "no-detection", "wrong-root"])
async def test_incompatible_backend_settings_fail_before_creating_marker(tmp_path, condition):
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
    assert not fixture.path_checks and not list(fixture.root.iterdir())


@pytest.mark.parametrize("version", ["2.19.1", "2.36.1", "2.41.0"])
async def test_other_audiobookshelf_releases_still_verify_the_folder(tmp_path, version):
    fixture = ImportBackendFixture(tmp_path.resolve())
    fixture.version = version
    async with fixture.client() as adapter:
        result = await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert result["version"] == version and result["root_mapping"]
    assert not list(fixture.root.iterdir())


async def test_different_worker_backend_mounts_are_not_verified(tmp_path):
    worker, backend = tmp_path.resolve() / "worker", tmp_path.resolve() / "backend"
    worker.mkdir()
    backend.mkdir()
    fixture = ImportBackendFixture(backend)
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError, match="same library folder"):
            await verify_backend(adapter, "synthetic", "/books", worker, "ebook")
    assert not list(worker.iterdir()) and not list(backend.iterdir())


async def test_remote_failure_cleans_only_own_empty_marker(tmp_path):
    fixture = ImportBackendFixture(tmp_path.resolve())

    async def fail_on_visible(name):
        if (fixture.root / name).exists():
            raise httpx.ConnectError("Synthetic network outage")

    fixture.before_exists = fail_on_visible
    async with fixture.client() as adapter:
        with pytest.raises(AdapterError):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert not list(fixture.root.iterdir())


async def test_replaced_marker_is_preserved(tmp_path):
    fixture = ImportBackendFixture(tmp_path.resolve())
    replaced = []

    async def replace(name):
        path = fixture.root / name
        if path.exists():
            path.rename(fixture.root / "moved-original")
            path.mkdir()
            replaced.append(path)

    fixture.before_exists = replace
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError, match="replacement preserved"):
            await verify_backend(adapter, "synthetic", "/books", fixture.root, "ebook")
    assert replaced[0].is_dir() and (fixture.root / "moved-original").is_dir()


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
