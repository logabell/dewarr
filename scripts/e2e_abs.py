"""Synthetic ABS HTTP endpoint for browser tests; never a compatibility certificate."""

import json
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.adapters.torrent_probe import describe
from app.config import get_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.abs_import_fixture import ScanningBackend  # noqa: E402
from tests.mam_fixture import release_row, search_response  # noqa: E402
from tests.torrent_fixture import torrent_bytes  # noqa: E402

app = FastAPI()
catalog_state = {"narrator": "Sample Narrator", "discovery_failure": False}
backend_state = {"watcher_enabled": True}
qbit_state = {"transfers": {}, "adds": 0}
mam_state = {"cookie": "browser-mam-fixture", "requests": 0}
item = json.loads(
    (Path(__file__).resolve().parents[1] / "tests/fixtures/audiobookshelf-item.json").read_text()
)
scanner = ScanningBackend(get_settings().import_destinations["ebooks"])
scanner.backend_path, scanner.library_id = "/fixture/books", "library-one"


@app.api_route("/qbit/api/v2/{path:path}", methods=["GET", "POST"])
async def qbit_fixture(path: str, request: Request):
    if path == "auth/login":
        credentials = parse_qs((await request.body()).decode())
        if credentials != {
            "username": ["browser-qbit-user"],
            "password": ["browser-qbit-password"],
        }:
            raise HTTPException(401)
        response = Response(status_code=204)
        response.set_cookie("SID", "browser-qbit-session", httponly=True)
        return response
    if request.cookies.get("SID") != "browser-qbit-session":
        raise HTTPException(403)
    if path == "app/version":
        return PlainTextResponse("v5.2.3")
    if path == "app/webapiVersion":
        return PlainTextResponse("2.15.1")
    if path in {"torrents/fetchMetadata", "torrents/saveMetadata"}:
        magnet = parse_qs((await request.body()).decode()).get("source", [""])[0]
        digest = describe(torrent_bytes())["infohash_v1"]
        if digest not in magnet:
            raise HTTPException(404)
        if path == "torrents/fetchMetadata":
            return {"hash": digest, "info": {}}
        return Response(torrent_bytes(), media_type="application/x-bittorrent")
    if path == "torrents/add":
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
            + await request.body()
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in message.iter_parts()
        }
        descriptor = describe(fields["torrents"])
        digest = descriptor["infohash_v1"]
        if digest in qbit_state["transfers"]:
            raise HTTPException(409, "Duplicate synthetic add")
        qbit_state["adds"] += 1
        qbit_state["transfers"][digest] = {
            "row": {
                "hash": digest,
                "save_path": fields["savepath"].decode(),
                "tags": fields["tags"].decode(),
                "category": fields["category"].decode(),
                "auto_tmm": False,
                "state": "downloading",
                "amount_left": 18,
                "total_size": descriptor["content_bytes"],
                "progress": 0.25,
                "added_on": 100,
                "name": descriptor["name"],
            },
            "descriptor": descriptor,
        }
        return PlainTextResponse("Ok.")
    if path == "torrents/info":
        rows = [
            value["row"]
            for digest, value in qbit_state["transfers"].items()
            if (not request.query_params.get("hashes") or request.query_params["hashes"] == digest)
            and (
                not request.query_params.get("tag")
                or request.query_params["tag"] == value["row"]["tags"]
            )
        ]
        if request.query_params.get("sort") == "hash":
            rows.sort(key=lambda row: row["hash"])
        offset = int(request.query_params.get("offset", 0))
        limit = int(request.query_params.get("limit", len(rows) or 1))
        return rows[offset : offset + limit]
    if path in {"torrents/properties", "torrents/files"}:
        value = qbit_state["transfers"].get(request.query_params.get("hash"))
        if not value:
            raise HTTPException(404)
        descriptor = value["descriptor"]
        if path.endswith("properties"):
            return {
                "save_path": value["row"]["save_path"],
                "infohash_v1": descriptor["infohash_v1"],
                "infohash_v2": descriptor["infohash_v2"] or "",
            }
        return [
            {
                "index": item["index"],
                "name": item["path"],
                "size": item["size_bytes"],
                "priority": 1,
                "progress": 0.25,
            }
            for item in descriptor["files"]
        ]
    raise HTTPException(404, "Unknown synthetic downloader operation")


@app.get("/fixture/recovery-stats")
async def recovery_stats():
    return {
        "adds": qbit_state["adds"],
        "library_items": [item, *scanner.items.values()],
        "transfers": {key: value["row"] for key, value in sorted(qbit_state["transfers"].items())},
    }


@app.get("/fixture/mam-session")
async def fixture_mam_session():
    # Invented isolated browser-fixture credential; never a real tracker session.
    return {"cookie": mam_state["cookie"]}


@app.api_route("/mam/{path:path}", methods=["GET", "POST"])
async def mam_fixture(path: str, request: Request):
    if request.cookies.get("mam_id") != mam_state["cookie"]:
        raise HTTPException(401)
    mam_state["requests"] += 1
    mam_state["cookie"] = f"browser-mam-rotated-{mam_state['requests']}"
    if path == "tor/download.php/fixture-private-download-token":
        if dict(request.query_params) not in (
            {"tid": "501"},
            {"tid": "502"},
            {"tid": "503"},
            {"tid": "504"},
            {"tid": "505"},
        ):
            raise HTTPException(400)
        content = (
            torrent_bytes(
                name=b"Hardcover Later Arrival"
                if request.query_params["tid"] == "503"
                else b"List Policy Arrival"
                if request.query_params["tid"] == "505"
                else b"Hardcover List Arrival",
                files=[{b"length": 24, b"path": [b"book.epub"]}],
            )
            if request.query_params["tid"] in {"503", "504", "505"}
            else torrent_bytes(
                name=b"The Next Harbor", files=[{b"length": 24, b"path": [b"book.epub"]}]
            )
            if request.query_params["tid"] == "502"
            else torrent_bytes()
        )
        response = Response(content, media_type="application/x-bittorrent")
        response.set_cookie("mam_id", mam_state["cookie"], httponly=True)
        return response
    if path == "jsonLoad.php":
        body = {"uid": 99, "username": "Synthetic MAM account"}
    elif path == "tor/js/loadSearchJSONbasic.php":
        query = await request.json()
        body = (
            {"error": "Nothing returned, out of 0"}
            if query["tor"].get("text") == "No source matches"
            else search_response()
        )
        if query["tor"].get("text") == "Many source matches":
            body = search_response(
                data=[
                    release_row(id=700 + i, title=f"Paged fixture release {i:02}")
                    for i in range(50)
                ]
            )
        if query["tor"].get("id") == 502 or query["tor"].get("text") == "The Next Harbor":
            body = search_response(
                data=[
                    release_row(
                        id=502,
                        title="The Next Harbor",
                        main_cat=14,
                        filetype="EPUB",
                        narrator_info="{}",
                        catname="Ebooks - Fiction",
                    )
                ]
            )
        titles = {
            503: "Hardcover Later Arrival",
            504: "Hardcover List Arrival",
            505: "List Policy Arrival",
        }
        identifier = next(
            (
                key
                for key, title in titles.items()
                if query["tor"].get("id") == key or query["tor"].get("text") == title
            ),
            None,
        )
        if identifier:
            body = search_response(
                data=[
                    release_row(
                        id=identifier,
                        title=titles[identifier],
                        main_cat=14,
                        filetype="EPUB",
                        narrator_info="{}",
                        series_info="{}",
                        author_info='{"1":"Catalog Author"}',
                        size="24 B",
                        catname="Ebooks - Fiction",
                        description="A synthetic standalone ebook.",
                    )
                ]
            )
    else:
        raise HTTPException(404)
    response = JSONResponse(body)
    response.set_cookie("mam_id", mam_state["cookie"], httponly=True)
    return response


@app.api_route("/abs/{path:path}", methods=["GET", "POST"])
async def endpoint(path: str, request: Request, authorization: str = Header(default="")):
    if authorization != "Bearer browser-abs-fixture-token":
        raise HTTPException(401)
    if path == "api/authorize":
        return {
            "user": {"id": "fixture-user", "type": "user", "permissions": {}},
            "serverVersion": "2.36.1",
        }
    if path == "api/libraries":
        return {"libraries": [{"id": "library-one", "name": "Fixture books", "mediaType": "book"}]}
    if path == "status":
        return {"app": "audiobookshelf", "serverVersion": "2.36.1"}
    if path == "api/libraries/library-one":
        return {
            "id": "library-one",
            "mediaType": "book",
            "folders": [{"fullPath": "/fixture/books"}],
            "settings": {
                "audiobooksOnly": False,
                "disableWatcher": not backend_state["watcher_enabled"],
                "metadataPrecedence": [
                    "folderStructure",
                    "audioMetatags",
                    "opfFile",
                    "absMetadata",
                ],
            },
        }
    if path == "api/filesystem/pathexists":
        body = await request.json()
        if (
            body["folderPath"] != "/fixture/books"
            or not body["directory"].startswith("book-search-check-")
            or "/" in body["directory"]
        ):
            raise HTTPException(400)
        return {
            "exists": (get_settings().import_destinations["ebooks"] / body["directory"]).is_dir()
        }
    if path == "api/libraries/library-one/items":
        return {
            "results": [
                {"id": row["id"], "updatedAt": 1} for row in [item, *scanner.items.values()]
            ],
            "total": 1 + len(scanner.items),
        }
    if path == "api/items/batch/get":
        body = await request.json()
        records = {item["id"]: item, **scanner.items}
        return {"libraryItems": [records[key] for key in body["libraryItemIds"]]}
    if path == f"api/items/{item['id']}":
        return item
    if path == "api/libraries/library-one/scan":
        scanner.scan()
        return {}
    if path.startswith("api/items/") and path.split("/")[-1] in scanner.items:
        return scanner.items[path.split("/")[-1]]
    raise HTTPException(404)


@app.post("/fixture/scan")
async def fixture_watch():
    scanner.scan()
    return {"items": len(scanner.items)}


@app.post("/fixture/watcher")
async def fixture_watcher(request: Request):
    body = await request.json()
    backend_state["watcher_enabled"] = body["enabled"] is True
    return backend_state


hardcover_list_state = {"mode": "normal"}
writeback_state = {"members": [], "writes": [], "lose_response": False}


@app.post("/fixture/writeback")
async def writeback_control(request: Request):
    body = await request.json()
    if body.get("reset"):
        writeback_state.update(
            members=[{"id": 1, "list_id": 92, "book_id": 42, "edition_id": None}],
            writes=[],
            lose_response=False,
        )
    if "lose_response" in body:
        writeback_state["lose_response"] = body["lose_response"]
    if body.get("readd"):
        writeback_state["members"] = [
            {**row, "id": row["id"] + 100} for row in writeback_state["members"]
        ]
    if body.get("comparison_books"):
        writeback_state["members"].extend(
            {"id": 1000 + n, "list_id": 92, "book_id": 2000 + n, "edition_id": None}
            for n in range(12)
        )
    return writeback_state


@app.get("/fixture/writeback")
async def writeback_status():
    return writeback_state


@app.post("/fixture/hardcover-list")
async def hardcover_list_control(request: Request):
    hardcover_list_state["mode"] = (await request.json())["mode"]
    return hardcover_list_state


@app.post("/catalog/v1/graphql")
async def catalog(request: Request, authorization: str = Header(default="")):
    if authorization != "Bearer browser-hardcover-token":
        raise HTTPException(401)
    body = await request.json()
    query = body.get("query", "")
    if any(
        name in query
        for name in [
            "WritableListOwner(",
            "WritableListMembership(",
            "AddListMembership(",
            "RemoveListMembership(",
        ]
    ) or ("ListMembershipPage(" in query and body["variables"]["id"] == 92):
        variables = body["variables"]
        header = {
            "id": 92,
            "name": "Write-back fixture list",
            "user_id": 7,
            "public": False,
            "books_count": len(writeback_state["members"]),
            "updated_at": "2026-09-19T00:00:00Z",
        }
        if "AddListMembership(" in query:
            row = {
                "id": max((row["id"] for row in writeback_state["members"]), default=0) + 1,
                "list_id": variables["list"],
                "book_id": variables["book"],
                "edition_id": None,
            }
            writeback_state["members"].append(row)
            writeback_state["writes"].append({"action": "add", **row})
            if writeback_state["lose_response"]:
                raise HTTPException(503, "Synthetic response lost after applying membership")
            return {"data": {"insert_list_book": {"id": row["id"], "list_book": row}}}
        if "RemoveListMembership(" in query:
            writeback_state["members"] = [
                r for r in writeback_state["members"] if r["id"] != variables["entry"]
            ]
            writeback_state["writes"].append({"action": "remove", "id": variables["entry"]})
            if writeback_state["lose_response"]:
                raise HTTPException(503, "Synthetic response lost after applying membership")
            return {"data": {"delete_list_book": {"id": variables["entry"], "list_id": 92}}}
        if "WritableListOwner(" in query:
            return {"data": {"me": [{"id": 7}], "lists": [header]}}
        if "WritableListMembership(" in query:
            rows = [r for r in writeback_state["members"] if r["book_id"] == variables["book"]]
            return {"data": {"me": [{"id": 7}], "lists": [{**header, "list_books": rows}]}}
        rows = [
            {
                **r,
                "position": r["id"],
                "date_added": None,
                "book": {
                    "id": r["book_id"],
                    "title": "The Catalog Journey"
                    if r["book_id"] == 42
                    else f"Compared remote book {r['book_id']}",
                    "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                },
            }
            for r in writeback_state["members"]
            if r["id"] > variables["after"]
        ]
        return {"data": {"lists": [{**header, "list_books": rows[:100]}]}}
    if "Community" in query or ("ListMembershipPage(" in query and body["variables"]["id"] == 9101):
        titles = {42: "The Catalog Journey", 9001: "The Discovered Harbor"}
        records = {
            key: {
                "id": key,
                "title": title,
                "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                "cached_image": {"url": f"https://covers.openlibrary.org/b/id/{key}-M.jpg"},
            }
            for key, title in titles.items()
        }
        members = [
            {
                "id": i,
                "book_id": key,
                "book": records[key],
                "edition_id": None,
                "position": i,
                "date_added": None,
            }
            for i, key in enumerate(titles, 1)
            if i > body["variables"].get("after", 0)
        ]
        header = {
            "id": 9101,
            "name": "Stories by the Sea",
            "description": "A community reading list for discovering your next chapter.",
            "books_count": 2,
            "followers_count": 28,
            "public": True,
            "user_id": 7,
            "updated_at": "2026-09-18T00:00:00Z",
            "list_books": members,
        }
        if "CommunitySearch(" in query:
            return {
                "data": {
                    "search": {"results": {"hits": [{"document": {"id": "9101"}}], "found": 1}}
                }
            }
        if "CommunityBooks(" in query:
            return {"data": {"books": [records[key] for key in body["variables"]["ids"]]}}
        return {"data": {"lists": [header]}}
    if "Discovery" in query:
        if catalog_state["discovery_failure"]:
            raise HTTPException(503)
        if "DiscoveryTrending(" in query:
            return {"data": {"books_trending": {"ids": [42, 9001, 9002]}}}
        if "DiscoveryRelated(" in query:
            return {
                "data": {
                    "books": [
                        {"id": body["variables"]["id"], "cached_similar_book_ids": [9001, 42]}
                    ]
                }
            }
        keys = body["variables"]["ids"] if "DiscoveryBooks(" in query else [9002]
        return {
            "data": {
                "books": [
                    {
                        "id": key,
                        "title": {
                            42: "The Catalog Journey",
                            9001: "The Discovered Harbor",
                            9002: "A New Chapter",
                        }[key],
                        "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                        **(
                            {"release_date": body["variables"]["to"]}
                            if "DiscoveryRecent(" in query
                            else {}
                        ),
                    }
                    for key in keys
                ]
            }
        }
    if "CatalogSeriesPage(" in query:
        members = [
            {
                "id": key,
                "position": key,
                "details": str(key),
                "compilation": key == 3,
                "featured": True,
                "book": {
                    "id": book_id,
                    "title": title,
                    "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                    "is_partial_book": False,
                    "release_date": "2020-01-01" if key != 4 else None,
                },
            }
            for key, book_id, title in [
                (1, 42, "The Catalog Journey"),
                (2, 7001, "Hardcover List Arrival"),
                (3, 7010, "Journey Collection"),
                (4, 7011, "Journey Without Date"),
            ]
        ]
        return {
            "data": {
                "series": [
                    {
                        "id": body["variables"]["id"],
                        "name": "The Journey Series",
                        "description": "A synthetic series for curation verification.",
                        "book_series_aggregate": {"aggregate": {"count": len(members)}},
                        "book_series": [m for m in members if m["id"] > body["variables"]["after"]],
                    }
                ]
            }
        }
    list_info = {
        "id": 91,
        "name": "Fixture Hardcover List",
        "books_count": 2,
        "updated_at": "2026-09-18T00:00:00+00:00",
        "public": False,
        "user_id": 7,
    }
    if "MyLists(" in query:
        return {"data": {"me": [{"id": 7, "lists": [list_info]}]}}
    if "FollowedLists(" in query:
        return {"data": {"me": [{"id": 7, "followed_lists": [{"id": 1, "list": list_info}]}]}}
    if "DiscoverLists(" in query:
        return {"data": {"lists": [{**list_info, "public": True}]}}
    if "ListMembershipPage(" in query:
        members = [
            {
                "id": 1,
                "book_id": 42,
                "edition_id": None,
                "position": 1,
                "date_added": None,
                "book": {
                    "id": 42,
                    "title": "The Catalog Journey",
                    "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                },
            },
            {
                "id": 2,
                "book_id": 7001,
                "edition_id": 8001,
                "position": 2,
                "date_added": None,
                "book": {
                    "id": 7001,
                    "title": "Hardcover List Arrival",
                    "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                },
            },
        ]
        mode = hardcover_list_state["mode"]
        if mode == "omission":
            members = members[:1]
        if mode in {"addition", "automation"}:
            members.append(
                {
                    "id": 3,
                    "book_id": 7002,
                    "edition_id": None,
                    "position": 3,
                    "date_added": None,
                    "book": {
                        "id": 7002,
                        "title": "Hardcover Later Arrival",
                        "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                    },
                }
            )
        if mode == "automation":
            members.append(
                {
                    "id": 4,
                    "book_id": 7003,
                    "edition_id": None,
                    "position": 4,
                    "date_added": None,
                    "book": {
                        "id": 7003,
                        "title": "List Policy Arrival",
                        "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                    },
                }
            )
        after = body["variables"]["after"]
        return {
            "data": {
                "lists": [
                    {
                        **list_info,
                        "books_count": len(members),
                        "list_books": [row for row in members if row["id"] > after][:1],
                    }
                ]
            }
        }
    if "ReaderBookDetails(" in query:
        return {
            "data": {
                "books": [
                    {
                        "id": body["variables"]["id"],
                        "slug": "the-discovered-harbor",
                        "rating": 4.25,
                        "ratings_count": 123,
                        "pages": 320,
                        "contributions": [
                            {
                                "contribution": "Author",
                                "author": {
                                    "id": 42,
                                    "name": "Discovery Writer",
                                    "slug": "discovery-writer",
                                    "bio": "A writer of coastal mysteries and distant journeys.",
                                },
                            }
                        ],
                    }
                ]
            }
        }
    if "ReaderBookReviews(" in query:
        return {
            "data": {
                "user_books": [
                    {
                        "id": 101,
                        "rating": 4.5,
                        "review_raw": "A thoughtful journey by the sea.",
                        "review_has_spoilers": False,
                        "user": {"username": "harbor_reader"},
                    },
                    {
                        "id": 102,
                        "rating": 4,
                        "review_raw": "The lighthouse keeper returns home.",
                        "review_has_spoilers": True,
                        "user": {"username": "coastal_reader"},
                    },
                ]
            }
        }

    def catalog_book():
        if body["variables"]["id"] in {9001, 9002, 9010}:
            key = body["variables"]["id"]
            return {
                "data": {
                    "books": [
                        {
                            "id": key,
                            "title": "The Discovered Harbor"
                            if key in {9001, 9010}
                            else "A New Chapter",
                            "description": "A synthetic discovery title.",
                            "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                        }
                    ]
                }
            }
        return {
            "data": {
                "books": [
                    {
                        "id": 42,
                        "title": "The Catalog Journey",
                        "description": "A synthetic book for catalog and metadata verification.",
                        "cached_contributors": [{"author": {"name": "Catalog Author"}}],
                        "book_series": [
                            {
                                "position": 1,
                                "compilation": False,
                                "series": {"id": 10, "name": "The Journey Series"},
                            }
                        ],
                    }
                ]
            }
        }

    def catalog_editions():
        if body["variables"]["id"] in {9001, 9002, 9010}:
            key = body["variables"]["id"]
            return {
                "data": {
                    "editions": [
                        {"id": key + 100, "book_id": key, "reading_format": {"format": "Ebook"}}
                    ]
                }
            }
        return {
            "data": {
                "editions": [
                    {
                        "id": 51,
                        "book_id": 42,
                        "isbn_13": "9781234567897",
                        "title": "The Catalog Journey",
                        "reading_format": {"format": "Ebook"},
                        "language": {"code2": "en"},
                    },
                    {
                        "id": 52,
                        "book_id": 42,
                        "title": "The Catalog Journey",
                        "reading_format": {"format": "Audio"},
                        "language": {"code2": "en"},
                        "cached_contributors": [
                            {
                                "contribution": "Narrator",
                                "author": {"name": catalog_state["narrator"]},
                            }
                        ],
                    },
                ]
            }
        }

    if "CatalogBook(" in query:
        return {"data": {**catalog_book()["data"], **catalog_editions()["data"]}}
    if "CatalogSearch(" in query:
        return {
            "data": {
                "search": {
                    "results": {
                        "found": 1,
                        "hits": [
                            {
                                "document": {
                                    "id": 42,
                                    "title": "The Catalog Journey",
                                    "author_names": ["Catalog Author"],
                                    "contribution_types": ["Author"],
                                    "release_year": 2020,
                                }
                            }
                        ],
                    }
                }
            }
        }
    raise HTTPException(400)


@app.post("/fixture/catalog/narrator")
async def set_fixture_narrator(request: Request):
    body = await request.json()
    catalog_state["narrator"] = body["narrator"]
    return {"status": "updated"}


@app.get("/openlibrary/{path:path}")
async def secondary_catalog(path: str):
    if path == "search.json":
        return {
            "numFound": 1,
            "docs": [
                {
                    "key": "/works/OL1W",
                    "title": "The Catalog Journey",
                    "author_name": ["Catalog Author"],
                }
            ],
        }
    if path == "works/OL1W.json":
        return {
            "key": "/works/OL1W",
            "title": "The Catalog Journey",
            "authors": [{"author": {"key": "/authors/OL1A"}}],
            "first_publish_date": "2020",
            "description": "Secondary fixture description",
        }
    if path == "authors/OL1A.json":
        return {"name": "Catalog Author"}
    if path == "works/OL1W/editions.json":
        return {"entries": []}
    raise HTTPException(404)


@app.get("/prowlarr/{path:path}")
async def prowlarr_fixture(path: str, request: Request):
    from tests.prowlarr_fixture import indexer, release

    if request.headers.get("x-api-key") != "browser-prowlarr-key":
        raise HTTPException(401)
    if path == "api/v1/system/status":
        return {"version": "2.3.0-fixture"}
    if path == "api/v1/indexer":
        return [
            indexer(),
            indexer(id=8, name="Unavailable tracker"),
            indexer(id=9, name="NZB books", protocol="usenet"),
            indexer(id=10, name="MAM duplicate", definitionName="MyAnonamouse"),
        ]
    if path == "api/v1/search":
        identifier = int(request.query_params["indexerIds"])
        if identifier == 8:
            raise HTTPException(503, "Synthetic source outage")
        return [
            release(
                indexerId=identifier,
                indexer="NZB books" if identifier == 9 else "Book tracker",
                title="Prowlarr browser audiobook" if identifier == 7 else "Unsupported NZB book",
                protocol="usenet" if identifier == 9 else "torrent",
                downloadUrl=f"http://127.0.0.1:13379/prowlarr/{identifier}/download?apikey=browser-prowlarr-key&link=private_fixture&file=book",
            )
        ]
    if path == "7/download":
        if request.query_params.get("link") != "private_fixture":
            raise HTTPException(404)
        return Response(torrent_bytes(), media_type="application/x-bittorrent")
    raise HTTPException(404)


shelf_state = {"version": 1, "mode": "normal"}


@app.post("/goodreads/control")
async def shelf_control(request: Request):
    body = await request.json()
    shelf_state.update(body)
    return shelf_state


@app.get("/goodreads/rss")
async def shelf_fixture(request: Request):
    from xml.sax.saxutils import escape

    if shelf_state["mode"] == "outage":
        return Response(status_code=503)
    etag = f'"shelf-{shelf_state["version"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    titles = [(100, "Goodreads Shelf Arrival"), (101, "Goodreads Next Read")]
    if shelf_state["mode"] == "omission":
        titles = titles[1:]
    if shelf_state["mode"] == "addition":
        titles.append((102, "Goodreads Later Addition"))
    items = "".join(
        f"<item><book_id>{titles_id}</book_id><title>{escape(title)}</title>"
        "<author_name>Fixture Shelf Author</author_name></item>"
        for titles_id, title in titles
    )
    return Response(
        '<rss version="2.0"><channel><title>Fixture shelf</title>'
        "<link>https://www.goodreads.com/review/list/123</link>" + items + "</channel></rss>",
        media_type="application/rss+xml",
        headers={"ETag": etag},
    )


@app.get("/")
async def abb_search_fixture():
    from tests.abb_fixture import search

    return Response(search(), media_type="text/html")


@app.get("/abss/harbor-alex-morgan/")
async def abb_detail_fixture():
    from tests.abb_fixture import detail

    return Response(detail(digest=describe(torrent_bytes())["infohash_v1"]), media_type="text/html")
