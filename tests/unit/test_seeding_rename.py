import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapters.contracts import AdapterError, DownloadFile, FailureKind
from app.adapters.qbittorrent import QbitState
from app.importing.publication import (
    PublicationBusy,
    PublicationError,
    PublicationSpec,
    PublishFile,
)
from app.importing.seeding_rename import (
    apply_plan,
    commit_seeding_plan,
    confirm_client_library,
    order_renames,
    plan_renames,
    restore_plan,
    restore_unplaced_rename,
    seeding_lock,
)

HASH = "a" * 40


def spec():
    return PublicationSpec(
        entry_id=uuid4(),
        plan_revision="c" * 64,
        source_root=Path("/downloads"),
        source_relative="Book",
        source_directory={"device": 1, "inode": 2, "size": 1, "mtime_ns": 1},
        destination_root=Path("/library"),
        staging_root=Path("/staging"),
        folder="Author/Title",
        mode="rename",
        files=[
            PublishFile(
                source="book.m4b",
                name="Title.m4b",
                sha256="ab" * 32,
                identity={"device": 1, "inode": 2, "size": 12, "mtime_ns": 3},
            )
        ],
    )


def state(**changes):
    values = {
        "external_id": HASH,
        "state": "stalledUP",
        "completed": True,
        "save_path": "/downloads",
        "files": [DownloadFile(relative_path="Book/book.m4b", size_bytes=12, complete=True)],
        "infohash_v1": HASH,
        "category": "book-search",
        "auto_managed": False,
        "progress": 1.0,
        "total_bytes": 12,
        "all_files_selected": True,
    }
    values.update(changes)
    return QbitState(**values)


MAPPINGS = [{"download_root": "/downloads", "source_path": "/downloads", "source_key": "fixture"}]


def test_plan_renames_the_seeding_file_into_the_organized_library_name():
    plan = plan_renames(state(), spec(), MAPPINGS, "/library")
    assert plan["location"] == "/library"
    assert plan["previous_location"] == "/downloads"
    assert plan["renames"] == [["Book/book.m4b", "Author/Title/Title.m4b"]]
    assert plan["targets"] == ["Author/Title/Title.m4b"]
    assert plan["incidentals"] == []
    assert plan["originals"] == ["Book/book.m4b"]


def test_incidental_files_are_parked_inside_the_book_folder():
    extra = state(
        files=[
            DownloadFile(relative_path="Book/book.m4b", size_bytes=12, complete=True),
            DownloadFile(relative_path="Book/notes.txt", size_bytes=4, complete=True),
            DownloadFile(relative_path="Book/cover.jpg", size_bytes=4, complete=True),
            DownloadFile(relative_path="Book/_____padding_file_0", size_bytes=4, complete=True),
        ]
    )
    plan = plan_renames(extra, spec(), MAPPINGS, "/library")
    assert plan["targets"] == ["Author/Title/Title.m4b"]
    assert plan["incidentals"] == [
        "Author/Title/.torrent/Book/_____padding_file_0",
        "Author/Title/.torrent/Book/cover.jpg",
        "Author/Title/.torrent/Book/notes.txt",
    ]


def test_samples_and_booklets_stay_with_the_torrent():
    extra = state(
        files=[
            DownloadFile(relative_path="Book/book.m4b", size_bytes=12, complete=True),
            DownloadFile(relative_path="Book/sample.mp3", size_bytes=1, complete=True),
            DownloadFile(relative_path="Book/Booklet.pdf", size_bytes=8, complete=True),
            DownloadFile(relative_path="Book/notes.pdf", size_bytes=1, complete=True),
        ]
    )
    plan = plan_renames(extra, spec(), MAPPINGS, "/library")
    assert plan["targets"] == ["Author/Title/Title.m4b"]
    assert plan["incidentals"] == [
        "Author/Title/.torrent/Book/Booklet.pdf",
        "Author/Title/.torrent/Book/notes.pdf",
        "Author/Title/.torrent/Book/sample.mp3",
    ]


def test_plan_refuses_other_books_in_the_torrent():
    extra = state(
        files=[
            DownloadFile(relative_path="Book/book.m4b", size_bytes=12, complete=True),
            DownloadFile(relative_path="Other/second.m4b", size_bytes=8, complete=True),
        ]
    )
    with pytest.raises(PublicationError, match="hardlink or copy"):
        plan_renames(extra, spec(), MAPPINGS, "/library")
    ebook = spec().model_copy(
        update={
            "files": [
                spec().files[0].model_copy(update={"source": "book.epub", "name": "Title.epub"})
            ]
        }
    )
    packed = state(
        files=[
            DownloadFile(relative_path="Book/book.epub", size_bytes=12, complete=True),
            DownloadFile(relative_path="Other/second.epub", size_bytes=8, complete=True),
        ]
    )
    with pytest.raises(PublicationError, match="hardlink or copy"):
        plan_renames(packed, ebook, MAPPINGS, "/library")
    full_sample = state(
        files=[
            DownloadFile(relative_path="Book/book.m4b", size_bytes=12, complete=True),
            DownloadFile(relative_path="Book/The Sample.m4b", size_bytes=12, complete=True),
        ]
    )
    with pytest.raises(PublicationError, match="hardlink or copy"):
        plan_renames(full_sample, spec(), MAPPINGS, "/library")
    second_book = state(
        files=[
            DownloadFile(relative_path="Book/book.m4b", size_bytes=12, complete=True),
            DownloadFile(relative_path="Other/Second.epub", size_bytes=12, complete=True),
            DownloadFile(relative_path="Other/Novel.pdf", size_bytes=12, complete=True),
        ]
    )
    with pytest.raises(PublicationError, match="hardlink or copy"):
        plan_renames(second_book, spec(), MAPPINGS, "/library")


def test_plan_refuses_automatic_torrent_management():
    with pytest.raises(PublicationError, match="automatic torrent management"):
        plan_renames(state(auto_managed=True), spec(), MAPPINGS, "/library")


def test_swapped_names_rename_through_a_temporary_path():
    assert order_renames([("a.m4b", "b.m4b"), ("b.m4b", "a.m4b")]) == [
        ["a.m4b", ".dewarr-rename-0"],
        ["b.m4b", "a.m4b"],
        [".dewarr-rename-0", "b.m4b"],
    ]


class Client:
    def __init__(self, current):
        self.state = current
        self.events = []

    async def set_location(self, key, location):
        self.events.append(("move", location, self.state.save_path))
        self.state = self.state.model_copy(update={"save_path": location})
        return True

    async def status(self, key):
        return self.state

    async def rename_file(self, key, old, new):
        self.events.append(("rename", old, new, self.state.save_path))
        files = []
        found = False
        for item in self.state.files:
            if item.relative_path == old:
                files.append(item.model_copy(update={"relative_path": new}))
                found = True
            else:
                files.append(item)
        self.state = self.state.model_copy(update={"files": files})
        return found


def library_spec(tmp_path, planned=None):
    library, staging = tmp_path.resolve() / "library", tmp_path.resolve() / "staging"
    library.mkdir()
    staging.mkdir(mode=0o700)
    return (planned or spec()).model_copy(
        update={"destination_root": library, "staging_root": staging}
    )


async def test_apply_renames_before_the_library_move_and_a_repeat_is_unchanged():
    client = Client(state())
    plan = plan_renames(client.state, spec(), MAPPINGS, "/library")
    moved = await apply_plan(client, client.state, plan)
    assert client.events == [
        ("rename", "Book/book.m4b", "Author/Title/Title.m4b", "/downloads"),
        ("move", "/library", "/downloads"),
    ]
    assert moved.save_path == "/library"
    assert moved.files[0].relative_path == "Author/Title/Title.m4b"
    again = await apply_plan(client, moved, plan)
    assert again.files[0].relative_path == "Author/Title/Title.m4b"
    assert len(client.events) == 2


async def test_restore_returns_the_torrent_to_the_download_folder():
    client = Client(state())
    plan = plan_renames(client.state, spec(), MAPPINGS, "/library")
    await apply_plan(client, client.state, plan)
    restored = await restore_plan(client, HASH, plan)
    assert restored.save_path == "/downloads"
    assert restored.files[0].relative_path == "Book/book.m4b"


async def test_a_move_without_final_names_renames_from_the_download_folder():
    original = state()
    plan = plan_renames(original, spec(), MAPPINGS, "/library")
    client = Client(state(save_path="/library"))
    moved = await apply_plan(client, client.state, plan)
    assert client.events[0] == ("move", "/downloads", "/library")
    assert client.events[1][0] == "rename" and client.events[1][3] == "/downloads"
    assert moved.save_path == "/library"
    assert moved.files[0].relative_path == "Author/Title/Title.m4b"


async def test_an_existing_library_file_is_not_replaced(tmp_path):
    rooted = library_spec(tmp_path)
    leaf = rooted.destination_root / "Author" / "Title"
    leaf.mkdir(parents=True)
    existing = leaf / "Title.m4b"
    existing.write_bytes(b"already here")
    client = Client(state())
    plan = plan_renames(client.state, spec(), MAPPINGS, "/library")
    with pytest.raises(PublicationError, match="different file"):
        await commit_seeding_plan(client, client.state, plan, rooted, checks=1)
    assert client.events == []
    assert existing.read_bytes() == b"already here"


async def test_another_copy_of_the_same_bytes_is_not_replaced(tmp_path):
    data = b"0123456789ab"
    planned = spec().model_copy(
        update={
            "files": [
                spec()
                .files[0]
                .model_copy(
                    update={
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "identity": {
                            **spec().files[0].identity,
                            "size": len(data),
                            "device": 0,
                            "inode": 0,
                        },
                    }
                )
            ]
        }
    )
    rooted = library_spec(tmp_path, planned)
    leaf = rooted.destination_root / "Author" / "Title"
    leaf.mkdir(parents=True)
    existing = leaf / "Title.m4b"
    existing.write_bytes(data)
    client = Client(state())
    plan = plan_renames(client.state, planned, MAPPINGS, "/library")
    with pytest.raises(PublicationError, match="another copy"):
        await commit_seeding_plan(client, client.state, plan, rooted, checks=1)
    assert client.events == []
    assert existing.read_bytes() == data


async def test_unplaced_rename_is_restored_until_the_book_is_in_the_library(tmp_path):
    rooted = library_spec(tmp_path)
    client = Client(state())
    plan = plan_renames(client.state, spec(), MAPPINGS, "/library")
    await apply_plan(client, client.state, plan)
    await restore_unplaced_rename(client, client.state, plan, rooted)
    assert client.state.save_path == "/downloads"
    assert client.state.files[0].relative_path == "Book/book.m4b"
    moved = Client(state())
    await apply_plan(moved, moved.state, plan)
    leaf = rooted.destination_root / "Author" / "Title"
    leaf.mkdir(parents=True)
    await restore_unplaced_rename(moved, moved.state, plan, rooted)
    assert moved.state.save_path == "/downloads"
    assert moved.state.files[0].relative_path == "Book/book.m4b"
    placed = Client(state())
    await apply_plan(placed, placed.state, plan)
    (leaf / "Title.m4b").write_bytes(b"0123456789ab")
    await restore_unplaced_rename(placed, placed.state, plan, rooted)
    assert placed.state.save_path == "/library"
    assert placed.state.files[0].relative_path == "Author/Title/Title.m4b"


async def test_a_restore_waits_while_the_import_holds_the_library_lock(tmp_path):
    rooted = library_spec(tmp_path)
    started = asyncio.Event()

    async def hold():
        with seeding_lock(rooted):
            started.set()
            await asyncio.sleep(0.2)

    async def contend():
        await started.wait()
        with seeding_lock(rooted):
            return "acquired"

    holder = asyncio.create_task(hold())
    with pytest.raises(PublicationBusy):
        await contend()
    await holder


async def test_missing_library_files_are_returned_before_the_import_is_held(tmp_path):
    client = Client(state())
    current = client.state
    plan = plan_renames(current, spec(), MAPPINGS, "/library")
    with pytest.raises(PublicationError, match="has not placed the renamed files"):
        await commit_seeding_plan(client, current, plan, library_spec(tmp_path), checks=1)
    assert client.state.save_path == "/downloads"
    assert client.state.files[0].relative_path == "Book/book.m4b"


async def test_a_failed_return_says_the_torrent_needs_review(tmp_path):
    class Stuck(Client):
        async def set_location(self, key, location):
            if location == "/downloads" and self.state.save_path == "/library":
                raise OSError("move failed")
            return await super().set_location(key, location)

    client = Stuck(state())
    current = client.state
    plan = plan_renames(current, spec(), MAPPINGS, "/library")
    with pytest.raises(PublicationError, match="could not be returned") as error:
        await commit_seeding_plan(client, current, plan, library_spec(tmp_path), checks=1)
    assert "Review the torrent" in str(error.value)
    assert client.state.save_path == "/library"
    assert client.state.files[0].relative_path == "Author/Title/Title.m4b"


async def test_route_sentinel_proves_qbittorrent_sees_the_library_folder(tmp_path):
    library = tmp_path.resolve() / "library"
    library.mkdir()

    class Sees:
        def __init__(self):
            self.paths = []

        async def directory_entries(self, path):
            self.paths.append(path)
            if path != str(library):
                return []
            return [item.name for item in library.iterdir() if item.is_dir()]

    client = Sees()
    await confirm_client_library(client, str(library), library)
    assert client.paths == [str(library)]
    assert not list(library.iterdir())
    with pytest.raises(PublicationError, match="cannot see this library folder"):
        await confirm_client_library(client, "/qbittorrent/library", library)
    assert not list(library.iterdir())

    class Missing:
        async def directory_entries(self, path):
            raise AdapterError(FailureKind.NOT_FOUND, "qBittorrent resource was not found.")

    with pytest.raises(PublicationError, match="cannot see this library folder"):
        await confirm_client_library(Missing(), str(library), library)
    assert not list(library.iterdir())


@pytest.mark.parametrize("changed", [None, "credentials", "endpoint", "deleted"])
async def test_existing_seeding_import_accepts_disabled_client_but_fences_changes(
    monkeypatch, changed
):
    from types import SimpleNamespace

    from app.db.models import (
        AcquisitionSelection,
        FrozenImportPlan,
        ImportEntry,
        ImportRun,
        Integration,
    )
    from app.importing import seeding_rename
    from app.importing.naming import fingerprint

    attempt = SimpleNamespace(
        id=uuid4(), selection_id=uuid4(), endpoint_key=fingerprint({"url": "http://qbit"})
    )
    selection = SimpleNamespace(
        id=attempt.selection_id,
        downloader_id=uuid4(),
        frozen={"downloader": {"generation": 1}, "descriptor": {"infohash_v1": HASH}},
    )
    downloader = SimpleNamespace(
        kind="qbittorrent",
        enabled=False,
        deleted_at=changed == "deleted",
        owner_id=None,
        status="connected",
        credential_generation=2 if changed == "credentials" else 1,
        base_url="http://other" if changed == "endpoint" else "http://qbit",
        config={"mappings": MAPPINGS},
        encrypted_secrets="fixture",
    )
    rows = {
        ImportEntry: SimpleNamespace(
            run_id=uuid4(),
            configuration={"destination": {"seeding_rename": True, "client_path": "/library"}},
        ),
        ImportRun: SimpleNamespace(plan_id=uuid4()),
        FrozenImportPlan: SimpleNamespace(inspection_id=uuid4()),
        AcquisitionSelection: selection,
        Integration: downloader,
    }

    class Database:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, identifier):
            return rows[model]

        async def scalar(self, query):
            return attempt

    async def sources(db):
        return {"fixture": Path("/downloads")}

    async def repaired(db, selected):
        return None

    monkeypatch.setattr(seeding_rename, "session_factory", lambda: Database)
    monkeypatch.setattr(seeding_rename, "import_sources", sources)
    monkeypatch.setattr(seeding_rename, "accepted_configuration", repaired)
    monkeypatch.setattr(
        seeding_rename, "decrypt_secrets", lambda value: {"username": "user", "password": "secret"}
    )
    if changed:
        with pytest.raises(PublicationError):
            await seeding_rename.seeding_connection(uuid4())
    else:
        connection = await seeding_rename.seeding_connection(uuid4())
        assert connection["torrent_hash"] == HASH
        assert connection["tag"] == "book-search:" + str(attempt.id)
        assert connection["client_root"] == "/library"
