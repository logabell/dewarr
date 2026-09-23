import json
from copy import deepcopy
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.grimmory import Grimmory, parse_book, unreadable_book
from app.db.models import Operation
from app.domain.inventory import synchronize
from app.importing.backend import verify_backend
from app.importing.execution import _size_matches
from app.importing.publication import PublicationError


def file(path, *, size_kb=12, book_type="EPUB", folder=False):
    return {
        "filePath": path,
        "fileSubPath": path.rsplit("/", 1)[-1],
        "fileSizeKb": size_kb,
        "extension": path.rsplit(".", 1)[-1],
        "bookType": book_type,
        "folderBased": folder,
    }


def book(
    identifier=1,
    *,
    library_id=7,
    ebook=True,
    audio=False,
    physical=False,
    folder=False,
    supplementary=False,
):
    metadata = {
        "title": "The First Harbor",
        "authors": ["Alex Morgan"],
        "narrator": "Jordan Lee, Casey Reed" if audio else None,
        "language": "en",
        "publishedDate": "2024-03-01",
        "isbn13": "9780306406157",
        "asin": "B012345678",
        "seriesName": "Harbor",
        "seriesNumber": 1.0,
        "audiobookMetadata": {"durationSeconds": 3600} if audio else None,
    }
    if physical:
        return {
            "id": identifier,
            "libraryId": library_id,
            "isPhysical": True,
            "metadata": {"title": "Shelf copy"},
        }
    primary = (
        file(f"/books/{identifier}/story.m4b", book_type="AUDIOBOOK", folder=folder)
        if audio and not ebook
        else file(f"/books/{identifier}/story.epub")
    )
    extras = []
    if audio and ebook:
        extras.append(file(f"/books/{identifier}/story.m4b", book_type="AUDIOBOOK"))
    if supplementary:
        extras.append(file(f"/books/{identifier}/notes.pdf", book_type="PDF"))
    return {
        "id": identifier,
        "libraryId": library_id,
        "metadata": metadata,
        "primaryFile": primary,
        "alternativeFormats": [item for item in extras if item["bookType"] != "PDF"],
        "supplementaryFiles": [item for item in extras if item["bookType"] == "PDF"],
        "libraryPath": {"path": "/books"},
        "addedOn": "2026-01-01T00:00:00Z",
    }


def test_book_evidence_uses_kilobyte_sizes_and_skips_physical_copies():
    ebook = parse_book(book())
    assert ebook.full_ebook and not ebook.full_audio
    assert ebook.series == [{"name": "Harbor", "sequence": "1"}]
    assert ebook.identifiers == {"isbn": "9780306406157", "asin": "B012345678"}
    tagged = book()
    tagged["metadata"]["isbn13"] = "978-0-306-40615-7"
    tagged["metadata"]["language"] = "en-US"
    tagged["metadata"]["hardcoverId"] = "the-first-harbor"
    tagged["metadata"]["hardcoverBookId"] = "4242"
    tagged["metadata"]["authors"] = ["", "Alex Morgan"]
    identified = parse_book(tagged)
    assert identified.identifiers["isbn"] == "9780306406157"
    assert identified.language == "en"
    assert identified.identifiers["hardcover"] == "4242"
    assert identified.authors == ["Alex Morgan"]
    slug_only = book()
    slug_only["metadata"]["hardcoverId"] = "the-first-harbor"
    assert "hardcover" not in parse_book(slug_only).identifiers
    folder = book(audio=True, ebook=False, folder=True)
    folder["primaryFile"] = {
        "filePath": "/books/Harbor",
        "fileSizeKb": 10,
        "extension": "mp3",
        "bookType": "AUDIOBOOK",
        "folderBased": True,
    }
    parsed_folder = parse_book(
        folder,
        tracks=[
            {
                "filePath": "/books/Harbor/01.mp3",
                "fileSizeBytes": 1000,
                "extension": "mp3",
                "bookType": "AUDIOBOOK",
                "playbackIndex": 0,
                "durationMs": 1500,
            },
            {
                "filePath": "/books/Harbor/02.mp3",
                "fileSizeBytes": 2000,
                "extension": "mp3",
                "bookType": "AUDIOBOOK",
                "playbackIndex": 1,
                "durationMs": 1500,
            },
        ],
    )
    assert parsed_folder.path == "/books/Harbor"
    assert [item.path for item in parsed_folder.audio] == [
        "/books/Harbor/01.mp3",
        "/books/Harbor/02.mp3",
    ]
    assert parsed_folder.audio[0].size == 1000
    assert parsed_folder.audio[0].size_unit == "byte"
    assert ebook.library_files[0].size == 12 * 1024
    assert ebook.library_files[0].size_unit == "kilobyte"
    assert _size_matches(12 * 1024 + 500, ebook.library_files[0])
    assert not _size_matches(12 * 1024 + 1024, ebook.library_files[0])
    audio = parse_book(book(audio=True, ebook=False))
    assert audio.full_audio and audio.narrators == ["Jordan Lee", "Casey Reed"]
    last_first = book(audio=True, ebook=False)
    last_first["metadata"]["narrator"] = "Smith, John"
    assert parse_book(last_first).narrators == ["Smith, John"]
    separated = book(audio=True, ebook=False)
    separated["metadata"]["narrator"] = "Smith, John; Casey Reed"
    assert parse_book(separated).narrators == ["Smith, John", "Casey Reed"]
    beside = book(audio=True, ebook=False)
    beside["alternativeFormats"] = [file("/books/1/story.pdf", book_type="PDF")]
    parsed = parse_book(beside)
    assert parsed.ebook_supplementary and not parsed.full_ebook and parsed.full_audio
    notes = book()
    notes["primaryFile"] = file("/books/1/notes.pdf", book_type="PDF")
    notes["alternativeFormats"] = []
    notes["supplementaryFiles"] = [notes["primaryFile"]]
    parsed_notes = parse_book(notes)
    assert parsed_notes.ebook_supplementary and not parsed_notes.full_ebook
    physical = parse_book(book(physical=True))
    assert not physical.ebook and not physical.audio and not physical.full_ebook
    broken = book()
    broken["primaryFile"] = {"filePath": "relative.epub", "fileSizeKb": 1, "bookType": "EPUB"}
    with pytest.raises(AdapterError) as error:
        parse_book(broken)
    assert error.value.kind == FailureKind.PARSER


class GrimmoryFixture:
    def __init__(self, root=None):
        self.root = root
        self.version = "3.5.0"
        self.organization = "BOOK_PER_FOLDER"
        self.metadata_source = "PREFER_SIDECAR"
        self.watch = True
        self.allowed = []
        self.admin = True
        self.manage = False
        self.edit = True
        self.library_path = "/books"
        self.unauthorized = False
        self.reject_login = False
        self.metadata_updates = []
        self.full_books = None
        self.batch_missing = False
        folder_book = book(2, library_id=9, audio=True, ebook=False, folder=True)
        folder_book["primaryFile"] = {
            "filePath": "/books/9/Harbor",
            "fileSizeKb": 36,
            "extension": "mp3",
            "bookType": "AUDIOBOOK",
            "folderBased": True,
        }
        self.catalog = [book(1), folder_book, book(3)]
        self.tracks = [
            {
                "index": 0,
                "fileName": "01.mp3",
                "fileSizeBytes": 12000,
                "durationMs": 1000,
            },
            {
                "index": 1,
                "fileName": "02.mp3",
                "fileSizeBytes": 13000,
                "durationMs": 1000,
            },
            {
                "index": 2,
                "fileName": "story.m4b",
                "fileSizeBytes": 14000,
                "durationMs": 1000,
            },
        ]
        self.calls = []
        self.shift_total = False

    def handle(self, request):
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/v1/auth/login":
            assert request.method == "POST"
            assert "authorization" not in request.headers
            assert json.loads(request.content) == {"username": "reader", "password": "secret"}
            if self.reject_login:
                return httpx.Response(401)
            return httpx.Response(200, json={"accessToken": "jwt-token", "refreshToken": "later"})
        assert request.headers["authorization"] == "Bearer jwt-token"
        if path == "/api/v1/users/me":
            return httpx.Response(
                200,
                json={
                    "id": 4,
                    "username": "reader",
                    "permissions": {
                        "admin": self.admin,
                        "canManageLibrary": self.manage,
                        "canEditMetadata": self.edit,
                    },
                    "assignedLibraries": [{"id": 7}, {"id": 9}],
                },
            )
        if path == "/api/v1/version":
            return httpx.Response(200, json={"current": self.version, "latest": "3.5.0"})
        if path == "/api/v1/libraries":
            return httpx.Response(
                200,
                json=[
                    self.library(7, "Fiction"),
                    self.library(9, "Audio"),
                ],
            )
        if path in {"/api/v1/libraries/7", "/api/v1/libraries/9"}:
            library_id = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=self.library(library_id, "Fiction"))
        if path == "/api/v1/books/page":
            if self.unauthorized:
                self.unauthorized = False
                return httpx.Response(401)
            size = int(request.url.params["size"])
            number = int(request.url.params["page"])
            start = number * size
            total = len(self.catalog) + (1 if self.shift_total and number else 0)
            pages = (len(self.catalog) + size - 1) // size
            return httpx.Response(
                200,
                json={
                    "content": self.catalog[start : start + size],
                    "page": {
                        "size": size,
                        "number": number,
                        "totalElements": total,
                        "totalPages": pages,
                    },
                },
            )
        if path == "/api/v1/audiobooks/2/info":
            return httpx.Response(
                200, json={"bookId": 2, "folderBased": True, "tracks": self.tracks}
            )
        if path == "/api/v1/path":
            assert request.url.params["path"] == self.library_path
            names = [child.name for child in self.root.iterdir()] if self.root else []
            return httpx.Response(200, json=names)
        if path == "/api/v1/libraries/7/refresh":
            assert request.method == "PUT"
            return httpx.Response(204)
        if (
            request.method == "PUT"
            and path.startswith("/api/v1/books/")
            and path.endswith("/metadata")
        ):
            identifier = int(path.removeprefix("/api/v1/books/").removesuffix("/metadata"))
            body = json.loads(request.content)
            assert request.url.params["replaceMode"] == "REPLACE_WHEN_PROVIDED"
            assert request.url.params["mergeCategories"] == "false"
            match = next(item for item in self.catalog if item["id"] == identifier)
            match["metadata"].update(body["metadata"])
            self.metadata_updates.append(body)
            return httpx.Response(200, json=match)
        if path == "/api/v1/books/batch":
            if self.batch_missing:
                return httpx.Response(404)
            requested = {int(value) for value in request.url.params.get_list("ids")}
            source = self.catalog if self.full_books is None else self.full_books
            selected = [item for item in source if item["id"] in requested]
            return httpx.Response(200, json=selected)
        if path.startswith("/api/v1/books/"):
            identifier = int(path.removeprefix("/api/v1/books/"))
            match = next(item for item in self.catalog if item["id"] == identifier)
            return httpx.Response(200, json=match)
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    def library(self, identifier, name):
        return {
            "id": identifier,
            "name": name,
            "watch": self.watch,
            "paths": [{"id": 1, "libraryId": identifier, "path": self.library_path}],
            "allowedFormats": self.allowed,
            "organizationMode": self.organization,
            "metadataSource": self.metadata_source,
        }

    def client(self, endpoint="http://grimmory.test", secrets=None):
        return Grimmory(
            endpoint,
            secrets or {"username": "reader", "password": "secret"},
            transport=httpx.MockTransport(self.handle),
        )


async def test_login_pages_and_library_filter():
    fixture = GrimmoryFixture()
    async with fixture.client() as api:
        api.page_size = 1
        capabilities, _scope = await api.authorize()
        libraries = await api.libraries()
        first, total = await api.page("7", 0)
        rest, _ = await api.page("7", 1)
        summaries = first + rest
        again, _ = await api.page("7", 0)
        audio, audio_total = await api.page("9", 0)
        items = await api.expanded([item["id"] for item in summaries])
        tracks = await api.expanded([item["id"] for item in audio])
        await api.scan("7")
    assert capabilities.version == "3.5.0" and "scan" in capabilities.operations
    assert libraries == [{"id": "7", "name": "Fiction"}, {"id": "9", "name": "Audio"}]
    assert total == 2 and [item["id"] for item in summaries] == ["1", "3"]
    assert again == first
    assert audio_total == 1 and tracks[0].full_audio
    assert tracks[0].path == "/books/9/Harbor"
    assert [track.format for track in tracks[0].audio] == ["mp3", "mp3", "m4b"]
    assert [track.size for track in tracks[0].audio] == [12000, 13000, 14000]
    assert {item.id for item in items} == {"1", "3"}
    assert ("POST", "/api/v1/auth/login") in fixture.calls
    assert ("PUT", "/api/v1/libraries/7/refresh") in fixture.calls
    # One catalog download serves every library. Three books at page size 1.
    assert fixture.calls.count(("GET", "/api/v1/books/page")) == 3


async def test_catalog_shift_holds_the_page():
    fixture = GrimmoryFixture()
    fixture.shift_total = True
    async with fixture.client() as api:
        api.page_size = 1
        with pytest.raises(AdapterError) as error:
            await api.page("7", 0)
    assert error.value.kind == FailureKind.UNCERTAIN


async def test_folder_mapping_uses_grimmory_path_listing(tmp_path):
    fixture = GrimmoryFixture(tmp_path.resolve())
    async with fixture.client() as adapter:
        result = await verify_backend(adapter, "7", "/books", fixture.root, "ebook")
    assert result["version"] == "3.5.0"
    assert result["root_mapping"] and result["scan_capable"]
    assert result["audio_extensions"] == ["m4a", "m4b", "mp3", "opus"]
    assert not list(fixture.root.iterdir())


@pytest.mark.parametrize(
    "mutate,medium,message",
    [
        ("auto", "ebook", "Book per folder"),
        ("audio-only", "ebook", "ebooks"),
        ("ebook-only", "audio", "audiobooks"),
        ("unwatched", "ebook", "folder watch"),
    ],
)
async def test_incompatible_grimmory_settings_fail_before_the_marker(
    tmp_path, mutate, medium, message
):
    fixture = GrimmoryFixture(tmp_path.resolve())
    if mutate == "auto":
        fixture.organization = "AUTO_DETECT"
    elif mutate == "audio-only":
        fixture.allowed = ["AUDIOBOOK"]
    elif mutate == "ebook-only":
        fixture.allowed = ["EPUB"]
    else:
        fixture.watch = False
        fixture.admin = False
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError, match=message):
            await verify_backend(adapter, "7", "/books", fixture.root, medium)
    assert not list(fixture.root.iterdir())


@pytest.mark.parametrize("version", ["3.4.0", "3.5.0", "3.6.1"])
async def test_other_grimmory_releases_still_verify_the_folder(tmp_path, version):
    fixture = GrimmoryFixture(tmp_path.resolve())
    fixture.version = version
    async with fixture.client() as adapter:
        result = await verify_backend(adapter, "7", "/books", fixture.root, "ebook")
    assert result["version"] == version and result["root_mapping"]
    assert not list(fixture.root.iterdir())


async def test_one_unreadable_book_does_not_abort_the_page():
    fixture = GrimmoryFixture()
    broken = book(4)
    broken["primaryFile"]["filePath"] = "notes.epub"
    fixture.catalog.append(broken)
    async with fixture.client() as api:
        rows, total = await api.page("7", 0)
        items = await api.expanded([row["id"] for row in rows])
    readable = [item for item in items if not item.unreadable]
    unreadable = [item for item in items if item.unreadable]
    assert total == 3
    assert {item.id for item in readable} == {"1", "3"}
    assert [item.id for item in unreadable] == ["4"]
    # The placeholder still says what Grimmory knows, so an admin can find the book.
    assert unreadable[0].title == "The First Harbor"
    assert unreadable[0].authors == ["Alex Morgan"]
    assert unreadable[0].read_issues == ["Incomplete file"]


def test_bad_book_fields_are_dropped_and_named():
    untitled = book(audio=True, ebook=False)
    untitled["metadata"].update(
        title="  ",
        authors=["Alex Morgan", {"name": "Jordan Lee"}],
        narrator=["Jordan Lee"],
        language=12,
        description={"html": "<p>Story</p>"},
        abridged="no",
        publishedDate="sometime",
    )
    parsed = parse_book(untitled)
    assert not parsed.unreadable and parsed.full_audio
    assert parsed.title == "1"
    assert parsed.authors == ["Alex Morgan"]
    assert parsed.narrators == [] and parsed.language is None
    assert parsed.description is None and parsed.abridged is None and parsed.year is None
    assert parsed.read_issues == [
        "authors",
        "title",
        "narrators",
        "language",
        "description",
        "abridged",
        "year",
    ]
    # Short scalar values are kept for the admin review queue; free text is not.
    assert parsed.read_issue_values == {"language": "12", "abridged": "no", "year": "sometime"}
    blank = book()
    blank["metadata"]["authors"] = ["", "Alex Morgan"]
    assert parse_book(blank).read_issues == []
    assert parse_book(book()).read_issues == []


async def test_grimmory_books_with_bad_metadata_are_kept_for_review(
    client, admin, database, caplog
):
    fixture = GrimmoryFixture()
    with patch("app.api.integrations.Grimmory", fixture.client):
        response = await client.post(
            "/api/integrations",
            json={
                "kind": "grimmory",
                "name": "Home Grimmory",
                "base_url": "http://grimmory.test",
                "username": "reader",
                "password": "secret",
            },
        )
    assert response.status_code == 201, response.text
    connection = response.json()["id"]

    async def sync(key):
        response = await client.post(
            f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": key}
        )
        assert response.status_code == 202, response.text
        operation = UUID(response.json()["id"])
        await synchronize(operation, client_factory=fixture.client)
        async with database() as db:
            return await db.get(Operation, operation)

    def by_book(page):
        return {asset["open_url"].rsplit("/", 1)[-1]: asset for asset in page["items"]}

    first = await sync("grimmory-readable")
    assert first.status == "completed" and first.message == "Synced 2 Grimmory libraries"
    before = by_book((await client.get("/api/library/assets")).json())
    assert before["3"]["work_ids"]

    fixture.catalog[2]["metadata"]["authors"] = [{"name": "Alex Morgan"}]
    untitled = book(5)
    untitled["metadata"]["title"] = None
    untitled["primaryFile"] = file("/books/Salt Roads/salt.epub")
    broken = book(6)
    broken["primaryFile"]["filePath"] = "notes.epub"
    fixture.catalog += [untitled, broken]
    finished = await sync("grimmory-malformed")
    assert finished.status == "completed"
    assert finished.message == "Synced 2 Grimmory libraries. 3 items need review"
    assert finished.payload["review"] == {
        "total": 3,
        "needs_matching": 1,
        "read_issues": 3,
        "details": 0,
    }
    assets = by_book((await client.get("/api/library/assets")).json())
    # The earlier match survives an author list Dewarr can no longer read.
    assert assets["3"]["work_ids"] == before["3"]["work_ids"]
    assert assets["3"]["read_issues"] == ["authors"]
    assert assets["5"]["title"] == "Salt Roads"
    assert assets["5"]["match_status"] == "needs-review" and not assets["5"]["work_ids"]
    assert "6" not in assets
    review = (await client.get("/api/library/review?kind=read-issue")).json()
    unread = next(row["read_issue"] for row in review["items"] if row["kind"] == "read-issue")
    assert unread["reasons"] == ["Incomplete file"]
    assert unread["open_url"] == "http://grimmory.test/book/6"
    messages = [record.getMessage() for record in caplog.records]
    assert "Grimmory book 3 was read without some fields (authors)" in messages
    assert "Grimmory book 6 could not be read (Incomplete file)" in messages

    fixture.catalog[2]["metadata"]["authors"] = ["Alex Morgan"]
    broken["primaryFile"] = file("/books/6/story.epub")
    await sync("grimmory-repaired")
    summary = (await client.get("/api/library/review/summary")).json()
    assert summary["total"] == 1 and summary["needs_matching"] == 1
    assert "6" in by_book((await client.get("/api/library/assets")).json())


def test_unreadable_book_keeps_its_folder_name():
    broken = book(5)
    del broken["metadata"]["title"]
    broken["primaryFile"] = {"filePath": "/books/Salt Roads/salt.epub", "bookType": "EPUB"}
    broken["alternativeFormats"] = "not a list"
    with pytest.raises(AdapterError):
        parse_book(broken)
    placeholder = unreadable_book(broken, "Invalid file list")
    assert placeholder.unreadable and placeholder.invalid
    assert placeholder.title == "Salt Roads"
    assert placeholder.authors == ["Alex Morgan"]
    assert placeholder.path == "/books/Salt Roads"
    assert placeholder.read_issues == ["Invalid file list"]


async def test_expired_session_logs_in_once_and_retries():
    fixture = GrimmoryFixture()
    fixture.unauthorized = True
    async with fixture.client() as api:
        await api.page("7", 0)
    assert fixture.calls.count(("POST", "/api/v1/auth/login")) == 2
    assert fixture.calls.count(("GET", "/api/v1/books/page")) == 2


async def test_rejected_password_names_the_account():
    fixture = GrimmoryFixture()
    fixture.reject_login = True
    with pytest.raises(AdapterError, match="username or password") as error:
        async with fixture.client():
            pass
    assert error.value.kind == FailureKind.AUTHENTICATION


async def test_metadata_permission_is_required_to_import(tmp_path):
    fixture = GrimmoryFixture(tmp_path.resolve())
    fixture.admin = False
    fixture.manage = True
    fixture.edit = False
    async with fixture.client() as adapter:
        with pytest.raises(PublicationError, match="edit metadata"):
            await verify_backend(adapter, "7", "/books", fixture.root, "ebook")
    assert not list(fixture.root.iterdir())


async def test_refresh_snapshot_downloads_the_catalog_once_for_every_library():
    fixture = GrimmoryFixture()
    async with fixture.client() as api:
        api.page_size = 1
        await api.page("7", 0)
        await api.page("9", 0)
        first = fixture.calls.count(("GET", "/api/v1/books/page"))
        await api.refresh_snapshot()
        await api.page("7", 0)
        await api.page("9", 0)
        second = fixture.calls.count(("GET", "/api/v1/books/page"))
    assert first == 3
    assert second == 6


async def test_catalog_metadata_replaces_embedded_fields_after_detection():
    fixture = GrimmoryFixture()
    async with fixture.client() as api:
        await api.apply_catalog_metadata(
            "1",
            {
                "medium": "ebook",
                "title": "The First Harbor",
                "authors": ["Alex Morgan"],
                "language": "en-US",
                "isbn": "978-0-306-40615-7",
                "edition_year": 2024,
                "series": "Harbor",
                "sequence": "01",
                "hardcover": "the-first-harbor",
            },
            observed_year=2024,
        )
        await api.apply_catalog_metadata(
            "1",
            {"medium": "ebook", "title": "The First Harbor", "edition_year": 2024},
            observed_year=1999,
        )
        item = await api.item("1")
    written = fixture.metadata_updates[0]["metadata"]
    assert "publishedDate" not in written
    assert fixture.metadata_updates[1]["metadata"]["publishedDate"] == "2024-01-01"
    assert written["isbn13"] == "9780306406157"
    assert written["seriesNumber"] == 1.0
    assert written["language"] == "en-US"
    assert "hardcover" not in written and "hardcoverId" not in written
    assert item.language == "en"
    assert item.identifiers["isbn"] == "9780306406157"
    assert item.series == [{"name": "Harbor", "sequence": "1"}]
    assert "hardcover" not in item.identifiers


async def test_library_status_reads_the_full_book_not_the_list_summary():
    fixture = GrimmoryFixture()
    fixture.full_books = deepcopy(fixture.catalog)
    listed = next(item for item in fixture.catalog if item["id"] == 2)
    listed["primaryFile"]["folderBased"] = False
    listed["metadata"].pop("asin")
    full = next(item for item in fixture.full_books if item["id"] == 2)
    full["metadata"]["hardcoverBookId"] = "4242"
    async with fixture.client() as api:
        rows, _total = await api.page("9", 0)
        items = await api.expanded([row["id"] for row in rows])
    assert items[0].path == "/books/9/Harbor"
    assert [track.path for track in items[0].audio] == [
        "/books/9/Harbor/01.mp3",
        "/books/9/Harbor/02.mp3",
        "/books/9/Harbor/story.m4b",
    ]
    assert items[0].identifiers["asin"] == "B012345678"
    assert items[0].identifiers["hardcover"] == "4242"
    assert ("GET", "/api/v1/books/batch") in fixture.calls


async def test_full_book_read_falls_back_when_the_batch_route_is_absent():
    fixture = GrimmoryFixture()
    fixture.batch_missing = True
    async with fixture.client() as api:
        rows, _total = await api.page("9", 0)
        items = await api.expanded([row["id"] for row in rows])
    assert items[0].path == "/books/9/Harbor"
    assert ("GET", "/api/v1/books/2") in fixture.calls


async def test_catalog_over_one_thousand_pages_still_downloads():
    fixture = GrimmoryFixture()
    fixture.catalog = [book(identifier) for identifier in range(1, 1002)]
    async with fixture.client() as api:
        api.page_size = 1
        _rows, total = await api.page("7", 0)
    assert total == 1001
    assert fixture.calls.count(("GET", "/api/v1/books/page")) == 1001
