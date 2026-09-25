import json
import re
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.adapters.audiobookshelf import Audiobookshelf
from app.config import get_settings
from app.db.models import (
    AssetContains,
    AuditEvent,
    ImportDestination,
    Integration,
    Library,
    LibraryAsset,
    MetadataSettings,
    Operation,
    PartCombine,
)
from app.domain.inventory import synchronize
from app.importing import combine
from app.importing.destinations import destination_configuration
from app.importing.naming import fingerprint
from tests.contracts.test_audiobookshelf import connect

pytestmark = pytest.mark.integration

DISC = re.compile(r"^(disc|cd)\s*\d+$", re.I)
AUDIO = {".mp3", ".m4b"}


def opf(title):
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
        f"<dc:title>{title}</dc:title>"
        '<dc:creator opf:role="aut">Alex Morgan</dc:creator>'
        '<dc:creator opf:role="nrt">Full Cast</dc:creator>'
        "</metadata></package>"
    )


class FolderABS:
    """Synthetic Audiobookshelf that scans a real folder, not live scanner certification."""

    def __init__(self, root, backend="/books"):
        self.root, self.backend = root, backend
        self.ids, self.items = {}, {}
        self.progress, self.deleted = [], []
        # A new folder that Audiobookshelf treats as an existing item that moved.
        self.adopt = {}

    def item_folders(self):
        folders = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO:
                continue
            folder = path.parent
            if DISC.match(folder.name):
                folder = folder.parent
            folders[folder if folder != self.root else path] = True
        return list(folders)

    def scan(self):
        present = set()
        for folder in self.item_folders():
            relative = str(folder.relative_to(self.root))
            path = f"{self.backend}/{relative}"
            if path in self.adopt and path not in self.ids:
                moved = self.adopt[path]
                self.ids = {key: value for key, value in self.ids.items() if value != moved}
                self.ids[path] = moved
            identifier = self.ids.setdefault(path, f"abs-{len(self.ids) + len(self.deleted)}")
            present.add(identifier)
            files = sorted(folder.rglob("*")) if folder.is_dir() else [folder]
            files = [file for file in files if file.is_file()]
            title = folder.stem if folder.is_file() else folder.name
            metadata = folder / "metadata.opf" if folder.is_dir() else None
            if metadata and metadata.exists():
                title = re.search(r"<dc:title>(.*?)</dc:title>", metadata.read_text()).group(1)

            def entry(file):
                return {
                    "ino": str(file.stat().st_ino),
                    "metadata": {
                        "path": f"{self.backend}/{file.relative_to(self.root)}",
                        "ext": file.suffix,
                        "size": file.stat().st_size,
                        "mtimeMs": file.stat().st_mtime * 1000,
                    },
                }

            audio = [file for file in files if file.suffix.lower() in AUDIO]
            self.items[identifier] = {
                "id": identifier,
                "libraryId": "library-one",
                "path": path,
                "mediaType": "book",
                "isMissing": False,
                "isInvalid": False,
                "updatedAt": 1,
                "media": {
                    "metadata": {
                        "title": title,
                        "authors": [{"name": "Alex Morgan"}],
                        "narrators": ["Full Cast"],
                    },
                    "audioFiles": [
                        {**entry(file), "duration": 60, "index": index}
                        for index, file in enumerate(audio, 1)
                    ],
                },
                "libraryFiles": [entry(file) for file in files],
            }
        for identifier, item in self.items.items():
            if identifier not in present:
                item["isMissing"] = True
                item["updatedAt"] = 2

    async def handle(self, request):
        path = request.url.path.removeprefix("/abs/")
        if path == "api/authorize":
            return httpx.Response(
                200,
                json={
                    "user": {"id": "fixture-user", "type": "root", "permissions": {}},
                    "serverVersion": "2.36.1",
                },
            )
        if path == "api/libraries":
            return httpx.Response(
                200,
                json={"libraries": [{"id": "library-one", "name": "Audio", "mediaType": "book"}]},
            )
        if path == "api/libraries/library-one/scan":
            self.scan()
            return httpx.Response(200)
        if path == "api/libraries/library-one/items":
            values = list(self.items.values())
            start = int(request.url.params["page"]) * 100
            return httpx.Response(
                200,
                json={
                    "results": [
                        {key: item[key] for key in ("id", "path", "updatedAt", "isMissing")}
                        for item in values[start : start + 100]
                    ],
                    "total": len(values),
                },
            )
        if path == "api/items/batch/get":
            ids = json.loads(request.content)["libraryItemIds"]
            return httpx.Response(200, json={"libraryItems": [self.items[i] for i in ids]})
        if path == "api/me":
            return httpx.Response(200, json={"mediaProgress": self.progress})
        if path.startswith("api/items/"):
            identifier = path.removeprefix("api/items/")
            if request.method == "DELETE":
                assert self.items[identifier]["isMissing"]
                self.items.pop(identifier)
                self.deleted.append(identifier)
                return httpx.Response(200)
            item = self.items.get(identifier)
            return httpx.Response(200, json=item) if item else httpx.Response(404)
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    def client(self, endpoint, token):
        return Audiobookshelf(endpoint, token, transport=httpx.MockTransport(self.handle))


@pytest.fixture
async def shelf(client, admin, database, tmp_path, monkeypatch, request):
    base = tmp_path.resolve()
    root, staging, downloads = base / "library", base / "staging", base / "downloads"
    if getattr(request, "param", "sibling") == "nested":
        root.mkdir()
        staging = root / ".book-search-staging"
    staging.mkdir(mode=0o700)
    downloads.mkdir()
    for index in (1, 2, 3):
        folder = root / "Alex Morgan" / f"Dark Age (Part {index} of 3)"
        folder.mkdir(parents=True)
        (folder / f"{index:02}.mp3").write_bytes(b"audio" * index)
        (folder / "metadata.opf").write_text(opf(f"Dark Age (Part {index} of 3)"))
    (root / "Alex Morgan" / "Dark Age (Part 1 of 3)" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    fake = FolderABS(root)
    fake.scan()
    monkeypatch.setattr(combine, "Audiobookshelf", fake.client)
    monkeypatch.setattr(combine, "CONFIRM_DELAY", 0)
    monkeypatch.setattr(combine, "CONFIRM_ATTEMPTS", 2)
    settings = get_settings()
    monkeypatch.setattr(settings, "import_sources", {"fixture": downloads})
    monkeypatch.setattr(settings, "import_destinations", {"audio": root})
    monkeypatch.setattr(settings, "import_staging_root", staging)
    connection = await connect(client)
    await sync(client, connection, fake)
    async with database() as db, db.begin():
        library = await db.scalar(select(Library))
        destination = ImportDestination(
            root_key="audio", library_id=library.id, medium="audio", backend_path="/books"
        )
        db.add(destination)
        await db.flush()
        destination.probe = {
            "status": "verified",
            "source_key": "fixture",
            "source_path": str(downloads),
            "configuration_revision": fingerprint(await destination_configuration(db, destination)),
            "backend": {"root_mapping": True},
        }
        library_id = library.id
    return {
        "fake": fake,
        "root": root,
        "staging": staging,
        "connection": connection,
        "library_id": library_id,
        "owner_id": UUID(admin["id"]),
    }


async def sync(client, connection, fake):
    response = await client.post(
        f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 202, response.text
    await synchronize(UUID(response.json()["id"]), client_factory=fake.client)


async def parts(database, library_id):
    async with database() as db:
        return (
            await db.execute(
                select(LibraryAsset, AssetContains)
                .join(AssetContains, AssetContains.asset_id == LibraryAsset.id)
                .where(LibraryAsset.library_id == library_id)
                .order_by(AssetContains.part_index.nulls_last(), LibraryAsset.external_id)
            )
        ).all()


async def automatic_pass(database, shelf):
    async with database() as db, db.begin():
        integration = await db.scalar(select(Integration))
        operation = await combine.schedule_library_combine(
            db, shelf["owner_id"], integration.id, uuid4()
        )
        operation_id = operation.id if operation else None
    if operation_id:
        await combine.run(operation_id)
    return operation_id


async def run_latest(database):
    async with database() as db:
        operation = await db.scalar(
            select(Operation)
            .where(Operation.kind == "library.combine", Operation.status == "queued")
            .order_by(Operation.created_at.desc())
        )
        operation_id = operation.id
    await combine.run(operation_id)
    async with database() as db:
        return await db.get(Operation, operation_id)


async def status(client, work_id):
    response = await client.get(f"/api/library/works/{work_id}/part-sets")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("shelf", ["sibling", "nested"], indirect=True)
async def test_a_complete_part_set_becomes_one_book_and_separates_again(
    client, admin, database, shelf
):
    root, fake, library_id = shelf["root"], shelf["fake"], shelf["library_id"]
    rows = await parts(database, library_id)
    assert [contains.part_index for _, contains in rows] == [1, 2, 3]
    assert len({asset.version_id for asset, _ in rows}) == 1
    work_id, version_id = rows[0][1].work_id, rows[0][0].version_id
    part_items = [asset.external_id for asset, _ in rows]
    # The sync that saw every part queued a combine pass.
    async with database() as db:
        assert await db.scalar(select(Operation.id).where(Operation.kind == "library.combine"))
    [current] = await status(client, work_id)
    assert current["state"] == "ready" and current["can_combine"]

    fake.progress = [{"libraryItemId": part_items[1], "progress": 0.4, "currentTime": 900}]
    await run_latest(database)
    [current] = await status(client, work_id)
    assert current["state"] == "skipped"
    assert "started part 2" in current["reason"] and current["can_combine"]
    assert (root / "Alex Morgan" / "Dark Age (Part 2 of 3)" / "02.mp3").exists()

    fake.progress = []
    response = await client.post(
        f"/api/library/versions/{version_id}/combine", json={"library_id": str(library_id)}
    )
    assert response.status_code == 202, response.text
    operation = await run_latest(database)
    assert operation.status == "completed", operation.message
    assert operation.message == "Combined the parts into one book"
    book = root / "Alex Morgan" / "Dark Age - Full Cast"
    assert sorted(path.name for path in (root / "Alex Morgan").iterdir()) == [
        "Dark Age - Full Cast"
    ]
    assert sorted(path.name for path in book.iterdir()) == [
        "Disc 1",
        "Disc 2",
        "Disc 3",
        "cover.jpg",
        "metadata.opf",
    ]
    assert "<dc:title>Dark Age</dc:title>" in (book / "metadata.opf").read_text()
    assert sorted(fake.deleted) == sorted(part_items)
    [current] = await status(client, work_id)
    assert current["state"] == "combined" and current["can_separate"]
    assert current["folder"] == "Alex Morgan/Dark Age - Full Cast"

    rows = await parts(database, library_id)
    by_state = {asset.external_id: (asset.state, contains.part_index) for asset, contains in rows}
    combined = next(asset for asset, contains in rows if contains.part_index is None)
    assert combined.state == "present" and combined.version_id == version_id
    assert all(
        by_state[item] == ("intentionally-removed", index)
        for index, item in enumerate(part_items, 1)
    )
    works = (await client.get("/api/catalog/works")).json()["items"]
    assert [work["availability"]["owned"] for work in works] == [True]
    async with database() as db:
        event = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.parts.combined")
        )
        assert event.entity_id == combined.id

    # A later sync keeps the combined book whole and the old parts retired.
    await sync(client, shelf["connection"], fake)
    rows = await parts(database, library_id)
    assert {(asset.state, contains.part_index) for asset, contains in rows} == {
        ("present", None),
        ("intentionally-removed", 1),
        ("intentionally-removed", 2),
        ("intentionally-removed", 3),
    }
    review = (await client.get("/api/library/review/summary")).json()
    assert review["total"] == 0

    response = await client.post(
        f"/api/library/versions/{version_id}/separate", json={"library_id": str(library_id)}
    )
    assert response.status_code == 202, response.text
    operation = await run_latest(database)
    assert operation.message == "Separated the parts again", operation.message
    assert sorted(path.name for path in (root / "Alex Morgan").iterdir()) == [
        "Dark Age (Part 1 of 3)",
        "Dark Age (Part 2 of 3)",
        "Dark Age (Part 3 of 3)",
    ]
    assert (root / "Alex Morgan" / "Dark Age (Part 3 of 3)" / "metadata.opf").exists()
    rows = await parts(database, library_id)
    live = sorted(
        (contains.part_index, asset.state) for asset, contains in rows if asset.state == "present"
    )
    assert live == [(1, "present"), (2, "present"), (3, "present")]
    assert combined.external_id in fake.deleted
    [current] = await status(client, work_id)
    assert current["state"] == "separated" and current["can_combine"]
    works = (await client.get("/api/catalog/works")).json()["items"]
    assert [work["availability"]["owned"] for work in works] == [True]

    # The admin separated this book, so the next automatic pass leaves it alone.
    await automatic_pass(database, shelf)
    [current] = await status(client, work_id)
    assert current["state"] == "separated"
    assert (root / "Alex Morgan" / "Dark Age (Part 1 of 3)").exists()


async def test_audiobookshelf_may_keep_a_parts_item_for_the_combined_book(
    client, admin, database, shelf
):
    root, fake, library_id = shelf["root"], shelf["fake"], shelf["library_id"]
    rows = await parts(database, library_id)
    first = rows[0][0]
    fake.adopt["/books/Alex Morgan/Dark Age - Full Cast"] = first.external_id
    await run_latest(database)
    rows = await parts(database, library_id)
    combined = next(asset for asset, contains in rows if contains.part_index is None)
    assert combined.id == first.id and combined.state == "present"
    assert sorted(
        contains.part_index for asset, contains in rows if asset.state == "intentionally-removed"
    ) == [2, 3]
    assert first.external_id not in fake.deleted
    assert (root / "Alex Morgan" / "Dark Age - Full Cast" / "Disc 3" / "03.mp3").exists()


async def test_incomplete_or_unmapped_sets_wait_with_a_reason(client, admin, database, shelf):
    root, fake, library_id = shelf["root"], shelf["fake"], shelf["library_id"]
    rows = await parts(database, library_id)
    work_id = rows[0][1].work_id
    async with database() as db, db.begin():
        destination = await db.scalar(select(ImportDestination))
        destination.probe = None
    await run_latest(database)
    [current] = await status(client, work_id)
    assert current["state"] == "skipped"
    assert "Verify this library's audio import destination" in current["reason"]

    third = root / "Alex Morgan" / "Dark Age (Part 3 of 3)"
    for file in third.iterdir():
        file.unlink()
    third.rmdir()
    fake.scan()
    async with database() as db, db.begin():
        asset = await db.scalar(
            select(LibraryAsset).where(LibraryAsset.external_id == rows[2][0].external_id)
        )
        asset.state = "missing-confirmed"
    [current] = await status(client, work_id)
    assert current["state"] == "waiting"
    assert current["reason"] == "Waiting for part 3 of 3" and not current["can_combine"]
    response = await client.post(
        f"/api/library/versions/{rows[0][0].version_id}/combine",
        json={"library_id": str(library_id)},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Waiting for part 3 of 3"


async def test_turning_combining_off_stops_the_automatic_pass(client, admin, database, shelf):
    async with database() as db, db.begin():
        db.add(MetadataSettings(id=1, preferences={"combine_library_parts": False}))
    assert await automatic_pass(database, shelf) is None
    async with database() as db:
        assert not await db.scalar(select(PartCombine.id))
    assert (shelf["root"] / "Alex Morgan" / "Dark Age (Part 1 of 3)").exists()


def test_combining_is_on_by_default():
    from app.domain.catalog_metadata import MetadataPreferences

    assert MetadataPreferences().combine_library_parts
