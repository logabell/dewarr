"""Synthetic ABS import HTTP contract backed by a disposable worker folder."""

import json
import subprocess

import httpx
from defusedxml.ElementTree import parse

from app.adapters.audiobookshelf import Audiobookshelf


class ImportBackendFixture:
    def __init__(self, root, *, library_id="synthetic", backend_path="/books"):
        self.root = root
        self.library_id = library_id
        self.backend_path = backend_path
        self.version = "2.36.1"
        self.user_type = "root"
        self.settings = {
            "audiobooksOnly": False,
            "disableWatcher": False,
            "metadataPrecedence": [
                "folderStructure",
                "audioMetatags",
                "nfoFile",
                "txtFiles",
                "opfFile",
                "absMetadata",
            ],
        }
        self.requests = []
        self.before_library = None

    async def handle(self, request):
        assert request.headers["authorization"] == "Bearer private-import-token"
        path = request.url.path
        self.requests.append(path)
        if path == "/status":
            return httpx.Response(
                200, json={"app": "audiobookshelf", "serverVersion": self.version}
            )
        if path == "/api/authorize":
            return httpx.Response(
                200,
                json={
                    "user": {"id": "fixture", "type": self.user_type, "permissions": {}},
                    "serverVersion": self.version,
                },
            )
        if path == f"/api/libraries/{self.library_id}":
            if self.before_library:
                await self.before_library()
            return httpx.Response(
                200,
                json={
                    "id": self.library_id,
                    "mediaType": "book",
                    "folders": [{"fullPath": self.backend_path}],
                    "settings": self.settings,
                },
            )
        raise AssertionError(f"Unexpected ABS fixture request {path}")

    def client(self, url="http://fixture", token="private-import-token"):
        return Audiobookshelf(url, token, transport=httpx.MockTransport(self.handle))


class ScanningBackend(ImportBackendFixture):
    def __init__(self, root):
        super().__init__(root)
        self.items = {}
        self.item_ids = {}
        self.detect = True
        self.scan_count = 0

    def scan(self):
        self.scan_count += 1
        if not self.detect:
            return
        dc = "{http://purl.org/dc/elements/1.1/}"
        role = "{http://www.idpf.org/2007/opf}role"
        for opf in sorted(self.root.rglob("metadata.opf")):
            xml = parse(opf)
            folder = opf.parent
            relative = folder.relative_to(self.root)
            files = [
                {
                    "ino": str(path.stat().st_ino),
                    "metadata": {
                        "path": f"{self.backend_path}/{relative}/{path.name}",
                        "ext": path.suffix,
                        "size": path.stat().st_size,
                        "mtimeMs": path.stat().st_mtime * 1000,
                    },
                }
                for path in sorted(folder.iterdir())
                if path.is_file()
            ]
            media = {
                "coverPath": f"{self.backend_path}/{relative}/cover.jpg"
                if (folder / "cover.jpg").is_file()
                else None,
                "metadata": {
                    "title": xml.find(f".//{dc}title").text,
                    "authors": [
                        {"name": node.text}
                        for node in xml.findall(f".//{dc}creator")
                        if node.get(role) != "nrt"
                    ],
                    "narrators": [
                        node.text
                        for node in xml.findall(f".//{dc}creator")
                        if node.get(role) == "nrt"
                    ],
                    "language": xml.findtext(f".//{dc}language"),
                    "publishedYear": xml.findtext(f".//{dc}date"),
                },
                "audioFiles": [],
            }
            ebooks = sorted(
                (file for file in files if file["metadata"]["ext"] in {".epub", ".pdf", ".cbz"}),
                key=lambda file: (file["metadata"]["ext"] != ".epub", file["metadata"]["path"]),
            )
            if ebooks:
                media["ebookFile"] = {**ebooks[0], "ebookFormat": ebooks[0]["metadata"]["ext"][1:]}
            for file in ebooks:
                file["isSupplementary"] = file is not ebooks[0]
            for file in files:
                if file["metadata"]["ext"] not in {".mp3", ".m4b"}:
                    continue
                path = folder / file["metadata"]["path"].rsplit("/", 1)[-1]
                probe = json.loads(
                    subprocess.run(
                        ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
                        capture_output=True,
                        check=True,
                        timeout=10,
                    ).stdout
                )["format"]
                media["audioFiles"].append(
                    {
                        **file,
                        "duration": float(probe["duration"]),
                        # ABS also orders untagged tracks by their organized
                        # filenames; an absent tag is not track 1 on every file.
                        "index": int(
                            probe.get("tags", {})
                            .get("track", str(len(media["audioFiles"]) + 1))
                            .split("/")[0]
                        ),
                    }
                )
            # Adding an earlier-sorting book must not reassign an existing item's
            # identity. Multi-child imports can publish in either order.
            item_id = self.item_ids.setdefault(str(relative), f"import-{len(self.item_ids)}")
            self.items[item_id] = {
                "id": item_id,
                "libraryId": self.library_id,
                "path": f"{self.backend_path}/{relative}",
                "mediaType": "book",
                "media": media,
                "libraryFiles": files,
            }

    async def handle(self, request):
        import json

        path = request.url.path
        if path == f"/api/libraries/{self.library_id}/scan":
            self.scan()
            return httpx.Response(200)
        if path == f"/api/libraries/{self.library_id}/items":
            return httpx.Response(
                200, json={"total": len(self.items), "results": [{"id": key} for key in self.items]}
            )
        if path == "/api/items/batch/get":
            return httpx.Response(
                200,
                json={
                    "libraryItems": [
                        self.items[key] for key in json.loads(request.content)["libraryItemIds"]
                    ]
                },
            )
        if path.startswith("/api/items/"):
            return httpx.Response(200, json=self.items[path.split("/")[-1]])
        return await super().handle(request)
