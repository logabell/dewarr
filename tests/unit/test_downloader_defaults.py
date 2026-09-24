from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from app.domain.downloader_defaults import protocol_default
from app.domain.release_profiles import ReleasePreferences


def client(kind):
    return SimpleNamespace(id=uuid4(), kind=kind)


async def test_one_client_of_each_type_is_independently_automatic():
    torrent, nzb, soulseek = client("qbittorrent"), client("sabnzbd"), client("slskd")
    db = SimpleNamespace(scalars=AsyncMock(return_value=[torrent, nzb, soulseek]))
    prefs = ReleasePreferences()
    assert await protocol_default(db, prefs, "torrent") == torrent.id
    assert await protocol_default(db, prefs, "nzb") == nzb.id
    assert await protocol_default(db, prefs, "soulseek") == soulseek.id


async def test_multiple_clients_require_a_choice_for_their_type_only():
    first, second, nzb = client("qbittorrent"), client("transmission"), client("sabnzbd")
    db = SimpleNamespace(scalars=AsyncMock(return_value=[first, second, nzb]))
    prefs = ReleasePreferences()
    assert await protocol_default(db, prefs, "torrent") is None
    assert await protocol_default(db, prefs, "nzb") == nzb.id
    assert await protocol_default(db, prefs, "soulseek") is None


async def test_explicit_choice_is_preserved_even_when_unavailable():
    saved = uuid4()
    db = SimpleNamespace(scalars=AsyncMock(return_value=[client("qbittorrent")]))
    assert (
        await protocol_default(db, ReleasePreferences(torrent_downloader_id=saved), "torrent")
        == saved
    )
    db.scalars.assert_not_called()


async def test_legacy_usenet_default_does_not_hide_the_only_torrent_client():
    torrent, nzb = client("qbittorrent"), client("sabnzbd")
    db = SimpleNamespace(
        get=AsyncMock(return_value=nzb), scalars=AsyncMock(return_value=[torrent, nzb])
    )
    prefs = ReleasePreferences(downloader_id=nzb.id)
    assert await protocol_default(db, prefs, "torrent") == torrent.id
    assert await protocol_default(db, prefs, "nzb") == nzb.id


async def test_soulseek_does_not_select_a_torrent_or_usenet_fallback():
    from app.domain.automatic_routes import other_client

    db = SimpleNamespace(scalars=AsyncMock())
    assert await other_client(db, ReleasePreferences(), client("slskd")) is None
    db.scalars.assert_not_called()
