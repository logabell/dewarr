from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.prowlarr import MagnetRedirect, ProwlarrArtifact
from app.domain import prowlarr_resolution as resolver


def release():
    return SimpleNamespace(source_id="prowlarr:release-1", protocol="torrent")


@pytest.mark.asyncio
async def test_direct_artifact_is_persisted_without_metadata_resolution(monkeypatch):
    item = release()
    artifact = ProwlarrArtifact(item, b"torrent")
    calls = {}

    async def prowlarr_call(*args, **kwargs):
        calls["prowlarr"] = (args, kwargs)
        return artifact, 7

    async def persist_artifact(*args, **kwargs):
        calls["persist"] = (args, kwargs)
        return uuid4()

    async def resolve_metadata(*args, **kwargs):
        pytest.fail("Direct artifact must not invoke qBittorrent metadata resolution")

    monkeypatch.setattr(resolver, "prowlarr_call", prowlarr_call)
    monkeypatch.setattr(resolver, "persist_artifact", persist_artifact)
    monkeypatch.setattr(resolver, "resolve_metadata", resolve_metadata)

    identifier, returned = await resolver.resolve_prowlarr(
        uuid4(), item, "indexer-reference", expected_generation=7
    )

    assert returned is item
    assert calls["prowlarr"][1]["expected_generation"] == 7
    assert calls["persist"][1]["expected_downloader"] is None
    assert identifier is not None


@pytest.mark.asyncio
async def test_magnet_uses_only_selected_qbittorrent_and_fences_persistence(monkeypatch):
    item = release()
    connection_id = uuid4()
    magnet = "magnet:?xt=urn:btih:" + "a" * 40
    calls = {}

    async def prowlarr_call(*args, **kwargs):
        return MagnetRedirect(magnet), 7

    async def resolve_metadata(*args, **kwargs):
        calls["metadata"] = (args, kwargs)
        return b"verified-torrent", 12

    async def persist_artifact(*args, **kwargs):
        calls["persist"] = (args, kwargs)
        return uuid4()

    monkeypatch.setattr(resolver, "prowlarr_call", prowlarr_call)
    monkeypatch.setattr(resolver, "resolve_metadata", resolve_metadata)
    monkeypatch.setattr(resolver, "persist_artifact", persist_artifact)

    identifier, returned = await resolver.resolve_prowlarr(
        uuid4(),
        item,
        "indexer-reference",
        expected_generation=7,
        downloader_id=connection_id,
        downloader_generation=11,
    )

    assert returned is item
    assert calls["metadata"][0][1] == connection_id
    assert calls["metadata"][0][2] == magnet
    assert calls["metadata"][1]["expected_generation"] == 11
    assert calls["persist"][0][2].content == b"verified-torrent"
    assert calls["persist"][1]["expected_downloader"] == (connection_id, 12)
    assert identifier is not None


@pytest.mark.asyncio
async def test_magnet_without_selected_downloader_fails_without_guessing(monkeypatch):
    item = release()

    async def prowlarr_call(*args, **kwargs):
        return MagnetRedirect("magnet:?xt=urn:btih:" + "a" * 40), 7

    async def resolve_metadata(*args, **kwargs):
        pytest.fail("Must not guess a qBittorrent connection")

    monkeypatch.setattr(resolver, "prowlarr_call", prowlarr_call)
    monkeypatch.setattr(resolver, "resolve_metadata", resolve_metadata)

    with pytest.raises(HTTPException) as caught:
        await resolver.resolve_prowlarr(
            uuid4(), item, "indexer-reference", expected_generation=7
        )

    assert caught.value.status_code == 422
    assert "Select a qBittorrent downloader" in caught.value.detail


@pytest.mark.asyncio
async def test_invalid_magnet_is_rejected_before_metadata_resolution(monkeypatch):
    item = release()

    async def prowlarr_call(*args, **kwargs):
        return MagnetRedirect("magnet:?xt=urn:btih:private"), 7

    async def resolve_metadata(*args, **kwargs):
        pytest.fail("Invalid magnet must not reach qBittorrent")

    monkeypatch.setattr(resolver, "prowlarr_call", prowlarr_call)
    monkeypatch.setattr(resolver, "resolve_metadata", resolve_metadata)

    with pytest.raises(AdapterError) as caught:
        await resolver.resolve_prowlarr(
            uuid4(),
            item,
            "indexer-reference",
            expected_generation=7,
            downloader_id=uuid4(),
        )

    assert caught.value.kind == FailureKind.UNSUPPORTED
    assert "private" not in str(caught.value)
