import json
from datetime import UTC, datetime

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.slskd import (
    SEARCH_BUDGET_SECONDS,
    SEARCH_CALL_TIMEOUT,
    SEARCH_TIMEOUT_MS,
    SlskdClient,
    SlskdRelease,
    SlskdState,
    absolute_download_root,
    file_list_descriptor,
    group_responses,
    search_settled,
    soulseek_connected,
)
from app.domain.automatic_eligibility import eligibility
from app.domain.download_attempts import transfer_stage
from app.domain.downloaders import mapped_path
from app.domain.release_profiles import ReleasePreferences
from app.domain.slskd_connection import LEASE_SECONDS

KEY = "k" * 16


def responses():
    return [
        {
            "username": "reader",
            "queueLength": 1,
            "uploadSpeed": 50,
            "hasFreeUploadSlot": True,
            "files": [
                {
                    "filename": r"@@Ada Writer\Harbor\Harbor.m4b",
                    "size": 20,
                },
                {"filename": r"@@music\song.flac", "size": 8},
            ],
        }
    ]


def test_search_timeout_is_milliseconds_for_slskd_0_26():
    assert SEARCH_TIMEOUT_MS == 15000
    assert LEASE_SECONDS >= SEARCH_CALL_TIMEOUT * 3 + SEARCH_BUDGET_SECONDS


def test_a_windows_download_root_does_not_crash_selection():
    from types import SimpleNamespace

    from fastapi import HTTPException

    row = SimpleNamespace(
        config={"save_path": r"D:\downloads", "mappings": [], "client_managed": True}
    )
    with pytest.raises(HTTPException) as caught:
        mapped_path(row, r"D:\downloads", {})
    assert caught.value.status_code == 422


def test_connection_requires_the_soulseek_session():
    assert absolute_download_root("/downloads")
    assert absolute_download_root("D:\\downloads")
    assert not absolute_download_root("downloads")
    assert search_settled({"state": "Completed"})
    assert not search_settled({"state": "InProgress"})
    assert not search_settled({"state": "Incomplete"})
    assert not search_settled({})
    assert soulseek_connected({"server": {"state": "Connected"}})
    assert not soulseek_connected({"server": {"state": "Disconnected"}})
    assert not soulseek_connected({"state": "logged in"})


def test_folders_become_releases_without_inventing_identity():
    found = group_responses(
        responses(),
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )
    harbor = next(item for item in found if item.directory.endswith("Harbor"))
    assert harbor.protocol == "soulseek"
    assert harbor.title == "Harbor"
    assert harbor.authors == ["Writer"]
    assert harbor.peer_online is True
    assert harbor.seeders is None
    assert harbor.files[0].filename.endswith("Harbor.m4b")
    assert "\\" in harbor.files[0].filename
    music = next(item for item in found if "music" in item.directory)
    assert music.title != "Harbor"
    assert music.authors == []
    short = group_responses(
        [{"username": "reader", "files": [{"filename": "Anniversary/song.mp3", "size": 4}]}],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Ann",
        authors=["Ann"],
    )
    assert short[0].title != "Ann"
    assert short[0].authors == []
    titled = group_responses(
        [{"username": "reader", "files": [{"filename": "It's Here/song.mp3", "size": 4}]}],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="It",
        authors=["It"],
    )
    assert titled[0].title != "It"
    colon = group_responses(
        [{"username": "reader", "files": [{"filename": "Book: Title/Harbor.m4b", "size": 8}]}],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )[0]
    described, _content = file_list_descriptor(colon)
    assert described.name == "Book_ Title"
    assert described.files[0].path == "Book_ Title/Harbor.m4b"


def test_a_cut_off_or_locked_folder_is_not_an_automatic_grab():
    complete = [{"filename": "Harbor/Harbor.m4b", "size": 20}]
    complete += [{"filename": f"Other/{index}.mp3", "size": 1} for index in range(500)]
    kept = group_responses(
        [{"username": "reader", "files": complete}],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )
    assert any(item.directory == "Harbor" for item in kept)
    cut_files = [{"filename": f"Album/{index:03}.mp3", "size": 1} for index in range(400)]
    cut_files += [{"filename": f"Other/{index}.mp3", "size": 1} for index in range(100)]
    cut_files.append({"filename": "Album/999.mp3", "size": 1})
    cut = group_responses(
        [{"username": "reader", "files": cut_files}],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Album",
        authors=["Writer"],
    )
    assert not any(item.directory == "Album" for item in cut)
    assert any(item.directory == "Other" for item in cut)
    locked = group_responses(
        [
            {
                "username": "reader",
                "files": [{"filename": r"@@Ada\Harbor\Harbor.m4b", "size": 20}],
                "lockedFiles": [{"filename": r"@@Ada\Harbor\02.mp3", "size": 5}],
            }
        ],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )[0]
    assert locked.locked_files == 1
    descriptor, _content = file_list_descriptor(locked)
    reasons = eligibility(
        locked,
        {"title": "Harbor", "authors": ["Writer"]},
        {"medium": "audio", "language": None, "version_id": None, "abridged": None},
        ReleasePreferences(),
        descriptor=descriptor,
        unattended=True,
    )
    assert "Soulseek locked some files in this folder" in reasons


def test_a_disc_folder_is_not_an_automatic_grab():
    release = group_responses(
        [
            {
                "username": "reader",
                "files": [
                    {"filename": r"Writer\Harbor\CD1\01.mp3", "size": 10},
                    {"filename": r"Writer\Harbor\CD1\02.mp3", "size": 10},
                ],
            }
        ],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )[0]
    descriptor, _content = file_list_descriptor(release)
    reasons = eligibility(
        release,
        {"title": "Harbor", "authors": ["Writer"]},
        {"medium": "audio", "language": None, "version_id": None, "abridged": None},
        ReleasePreferences(),
        descriptor=descriptor,
        unattended=True,
    )
    assert "Soulseek returned one disc folder from a larger book" in reasons


def test_file_list_is_one_import_root():
    release = group_responses(
        responses(),
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )[0]
    if release.files[0].filename.endswith(".flac"):
        release = group_responses(
            responses(),
            search_id="search-1",
            observed_at=datetime.now(UTC),
            title="Harbor",
            authors=["Writer"],
        )[1]
    harbor = next(
        item
        for item in group_responses(
            responses(),
            search_id="search-1",
            observed_at=datetime.now(UTC),
            title="Harbor",
            authors=["Writer"],
        )
        if "m4b" in item.formats
    )
    descriptor, content = file_list_descriptor(harbor)
    assert descriptor.parser == "slskd-file-list"
    assert descriptor.artifact_sha256
    assert descriptor.name == "Harbor"
    assert descriptor.files[0].path == "Harbor/Harbor.m4b"
    assert json.loads(content)["username"] == "reader"
    reasons = eligibility(
        harbor,
        {"title": "Harbor", "authors": ["Writer"]},
        {"medium": "audio", "language": None, "version_id": None, "abridged": None},
        ReleasePreferences(),
        descriptor=descriptor,
        unattended=True,
    )
    assert reasons == []


def test_ebook_folders_survive_a_bad_size_and_mixed_folders_stay_unclassified():
    found = group_responses(
        [
            {
                "username": "reader",
                "queueLength": "busy",
                "files": [
                    {"filename": "Shelf/Book.epub", "size": 12},
                    {"filename": "Shelf/notes.txt", "size": 1},
                    {"filename": "Shelf/broken.epub", "size": "large"},
                ],
            },
            {
                "username": "reader",
                "files": [
                    {"filename": "Mix/Book.epub", "size": 12},
                    {"filename": "Mix/Book.m4b", "size": 20},
                ],
            },
        ],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Book",
        authors=["Writer"],
    )
    ebook = next(item for item in found if item.directory == "Shelf")
    mixed = next(item for item in found if item.directory == "Mix")
    assert ebook.medium == "ebook"
    assert ebook.queue_length is None
    assert [item.filename for item in ebook.files] == ["Shelf/Book.epub"]
    assert mixed.medium is None


def test_duplicate_leaf_names_still_match_completed_transfers():
    release = group_responses(
        [
            {
                "username": "reader",
                "files": [
                    {"filename": "Album/track.mp3", "size": 10},
                    {"filename": "Album/track.mp3", "size": 20},
                ],
            }
        ],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Album",
        authors=["Writer"],
    )[0]
    descriptor, _content = file_list_descriptor(release)
    assert [item.path for item in descriptor.files] == ["Album/track.mp3"]
    assert descriptor.files[0].size_bytes == 20


@pytest.mark.asyncio
async def test_a_finished_file_with_a_different_size_stops_the_folder():
    release = group_responses(
        [{"username": "reader", "files": [{"filename": "Album/track.mp3", "size": 10}]}],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Album",
        authors=["Writer"],
    )[0]

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={"transfers": [{"filename": "Album/track.mp3", "size": 99, "state": "Completed"}]},
        )

    async with SlskdClient(
        "http://slskd.test:5030", KEY, transport=httpx.MockTransport(handler)
    ) as client:
        state = await client.batch("attempt-1", release)
    assert state.state == "failed"
    assert state.completed is False


def test_a_failed_transfer_stops_the_folder():
    state = SlskdState(
        external_id="attempt-1",
        state="failed",
        completed=False,
        save_path="",
        reported_complete=False,
    )
    assert transfer_stage(None, state) == (
        "held",
        "Soulseek stopped this folder before every file finished",
    )


@pytest.mark.asyncio
async def test_enqueue_stores_the_attempt_and_cancels_a_partial_batch():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        assert request.headers["x-api-key"] == KEY
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["options"]["externalId"] == "attempt-1"
            assert body["options"]["destination"] == "Harbor"
            assert "searchId" not in body
            assert body["id"] == "attempt-1"
            assert body["files"][0]["filename"].endswith("Harbor.m4b")
            return httpx.Response(
                207,
                json={
                    "batch": {
                        "transfers": [
                            {
                                "id": "11111111-1111-1111-1111-111111111111",
                                "filename": "a",
                                "size": 1,
                            }
                        ]
                    },
                    "failures": [{}],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/batches/attempt-1"):
            return httpx.Response(
                200,
                json={
                    "transfers": [{"id": "11111111-1111-1111-1111-111111111111", "filename": "a"}]
                },
            )
        if request.method == "DELETE" and "/searches/" in request.url.path:
            return httpx.Response(204)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    release = SlskdRelease(
        source_id="a" * 40,
        raw_title="Harbor",
        title="Harbor",
        authors=["Writer"],
        medium="audio",
        formats=["m4b"],
        protocol="soulseek",
        observed_at=datetime.now(UTC),
        username="reader",
        directory="Harbor",
        files=[{"filename": "Harbor/Harbor.m4b", "size": 20}],
        search_id="search-1",
    )
    transport = httpx.MockTransport(handler)
    async with SlskdClient("http://slskd.test:5030", KEY, transport=transport) as client:
        with pytest.raises(AdapterError) as caught:
            await client.enqueue(release, attempt_id="attempt-1")
    assert caught.value.kind == FailureKind.UNSUPPORTED
    assert any(
        method == "DELETE" and path.endswith("/11111111-1111-1111-1111-111111111111")
        for method, path in calls
    )


@pytest.mark.asyncio
async def test_search_sends_the_millisecond_timeout_and_stops():
    def handler(request: httpx.Request):
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["searchTimeout"] == SEARCH_TIMEOUT_MS
            return httpx.Response(200, json={"id": "s1", "state": "Completed"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=[])

    async with SlskdClient(
        "http://slskd.test:5030", KEY, transport=httpx.MockTransport(handler)
    ) as client:
        found = await client.search(
            "Harbor",
            title="Harbor",
            authors=["Writer"],
            observed_at=datetime.now(UTC),
        )
    assert found == []


def _album():
    return group_responses(
        [
            {
                "username": "reader",
                "files": [
                    {"filename": "Album/track.mp3", "size": 10},
                    {"filename": "Album/chapter.mp3", "size": 20},
                ],
            }
        ],
        search_id="search-1",
        observed_at=datetime.now(UTC),
        title="Album",
        authors=["Writer"],
    )[0]


@pytest.mark.asyncio
async def test_duplicate_leaves_complete_when_both_transfers_finish():
    release = _album()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "transfers": [
                    {"filename": "Album/track.mp3", "size": 10, "state": "Completed"},
                    {"filename": "Album/chapter.mp3", "size": 20, "state": "Succeeded"},
                ]
            },
        )

    async with SlskdClient(
        "http://slskd.test:5030", KEY, transport=httpx.MockTransport(handler)
    ) as client:
        state = await client.batch("attempt-1", release)
    assert state.state == "complete"
    assert state.completed is True
    assert {item.relative_path.rsplit("/", 1)[-1] for item in state.files} == {
        "track.mp3",
        "chapter.mp3",
    }


@pytest.mark.asyncio
async def test_an_errored_transfer_fails_the_batch():
    release = _album()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "transfers": [
                    {"filename": "Album/track.mp3", "size": 10, "state": "Completed"},
                    {"filename": "Album/chapter.mp3", "size": 20, "state": "Errored"},
                ]
            },
        )

    async with SlskdClient(
        "http://slskd.test:5030", KEY, transport=httpx.MockTransport(handler)
    ) as client:
        state = await client.batch("attempt-1", release)
    assert state.state == "failed"
    assert state.completed is False


@pytest.mark.asyncio
async def test_a_key_that_cannot_read_options_asks_for_an_administrator_key():
    """slskd 0.26 answers /options with 403 for read-write keys."""

    def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/application"):
            return httpx.Response(200, json={"server": {"state": "Connected, LoggedIn"}})
        if path.endswith("/application/version"):
            return httpx.Response(200, json="0.26.2.0")
        return httpx.Response(403)

    async with SlskdClient(
        "http://slskd.test:5030", KEY, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AdapterError) as raised:
            await client.test()
    assert raised.value.kind is FailureKind.AUTHENTICATION
    assert "administrator" in str(raised.value)
