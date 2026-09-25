"""Prowlarr v1 read/search and binary resolution; never use its grab endpoint."""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from app.adapters.contracts import AdapterError, FailureKind, Release
from app.adapters.http import configured_url
from app.domain.catalog_network import retry_delay


class PrivateProxyLogFilter(logging.Filter):
    def filter(self, record):
        return not any(
            isinstance(arg, httpx.URL) and arg.path.endswith("/download")
            for arg in (record.args if isinstance(record.args, tuple) else ())
        )


logging.getLogger("httpx").addFilter(PrivateProxyLogFilter())


class ProwlarrSearch(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    indexer_id: int = Field(gt=0)
    medium: Literal["all", "ebook", "audio"] = "all"
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator("q")
    @classmethod
    def query(cls, value):
        if not value.strip():
            raise ValueError("Enter a title, author or series")
        return value.strip()


class ProwlarrIndexer(BaseModel):
    id: int
    name: str
    definition: str
    protocol: Literal["torrent", "nzb", "unknown"]
    enabled: bool
    supports_search: bool
    supports_pagination: bool
    categories: list[int]
    native_mam: bool
    excluded: bool = False


class ProwlarrRelease(Release):
    source: Literal["prowlarr"] = "prowlarr"
    title: str
    indexer_name: str
    categories: list[int]
    observed_at: datetime
    acquisition_supported: bool
    limitation: str | None = None

    @model_validator(mode="after")
    def explicit_formats(self):
        if not self.formats:
            formats = r"m4b|mp3|epub|pdf|flac|aac|ogg|opus|azw3|mobi|azw|cbz|cbr"
            labels = re.findall(rf"\[({formats})\]", self.title, re.I)
            if suffix := re.search(rf"\.({formats})$", self.title, re.I):
                labels.append(suffix[1])
            self.formats = list(dict.fromkeys(label.lower() for label in labels))
            if self.formats:
                self.details = {**self.details, "format_basis": "release_title"}
        return self


@dataclass(frozen=True)
class SearchHit:
    release: ProwlarrRelease
    reference: str | None = field(repr=False)


@dataclass(frozen=True)
class SearchBatch:
    hits: list[SearchHit]
    returned_count: int


@dataclass(frozen=True)
class ProwlarrArtifact:
    release: ProwlarrRelease
    content: bytes = field(repr=False)


def number(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def protocol(value):
    return {1: "nzb", 2: "torrent", "usenet": "nzb", "torrent": "torrent"}.get(
        value if type(value) in (int, str) else None, "unknown"
    )


def categories(value):
    if not isinstance(value, list):
        return []
    found = set()
    for category in value[:1000]:
        if isinstance(category, dict):
            identifier = number(category.get("id"))
            if identifier:
                found.add(identifier)
            # API capability trees contain subcategories; release rows are flat.
            children = category.get("subCategories")
            for child in children[:1000] if isinstance(children, list) else []:
                if isinstance(child, dict) and number(child.get("id")):
                    found.add(child["id"])
    return sorted(found)


def safe_text(value, fallback="", limit=600):
    if not isinstance(value, str):
        return fallback
    return "".join(c for c in value if c >= " " and c != "\x7f")[:limit]


class ProwlarrClient:
    def __init__(self, base_url, api_key, *, transport=None):
        self.base_url = configured_url(base_url)
        self.cooldown = 0
        self.client = httpx.AsyncClient(
            base_url=self.base_url + "/",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(40, connect=10),
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, path, *, params=None, binary=False):
        if self.cooldown:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "Prowlarr is cooling down.",
                retry_after=max(1, self.cooldown),
            )
        try:
            async with (
                asyncio.timeout(50),
                self.client.stream("GET", path, params=params) as response,
            ):
                self.cooldown = max(self.cooldown, retry_delay(response.headers, datetime.now(UTC)))
                kind = {
                    401: FailureKind.AUTHENTICATION,
                    403: FailureKind.PERMISSION,
                    404: FailureKind.NOT_FOUND,
                    429: FailureKind.RATE_LIMIT,
                }.get(response.status_code)
                if kind:
                    raise AdapterError(
                        kind,
                        "Prowlarr rejected this request. Check connection diagnostics.",
                        retry_after=max(1, self.cooldown)
                        if kind == FailureKind.RATE_LIMIT
                        else None,
                    )
                if 300 <= response.status_code < 400:
                    raise AdapterError(
                        FailureKind.UNSUPPORTED if binary else FailureKind.ROUTE,
                        "Prowlarr redirected the request. "
                        "Direct redirects and magnets are not supported here.",
                    )
                if not 200 <= response.status_code < 300:
                    raise AdapterError(
                        FailureKind.UNAVAILABLE, "Prowlarr could not complete this request."
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise AdapterError(
                            FailureKind.PARSER, "Prowlarr response exceeded the size limit."
                        )
                if binary:
                    return bytes(content)
                try:
                    return json.loads(content)
                except (ValueError, UnicodeError) as error:
                    raise AdapterError(
                        FailureKind.PARSER, "Prowlarr returned an unreadable response."
                    ) from error
        except (httpx.TimeoutException, TimeoutError) as error:
            raise AdapterError(FailureKind.TIMEOUT, "Prowlarr did not respond in time.") from error
        except httpx.HTTPError as error:
            raise AdapterError(FailureKind.ROUTE, "Prowlarr could not be reached.") from error

    async def test(self):
        result = await self.request("api/v1/system/status")
        if not isinstance(result, dict) or not isinstance(result.get("version"), str):
            raise AdapterError(FailureKind.PARSER, "Prowlarr status response is incompatible.")
        return safe_text(result["version"], limit=100)

    async def indexers(self):
        values = await self.request("api/v1/indexer")
        if not isinstance(values, list) or len(values) > 1000:
            raise AdapterError(FailureKind.PARSER, "Prowlarr returned an invalid indexer list.")
        result = []
        for row in values:
            if not isinstance(row, dict) or not number(row.get("id")):
                raise AdapterError(FailureKind.PARSER, "Prowlarr returned an invalid indexer.")
            definition = safe_text(row.get("definitionName"))
            capabilities = row.get("capabilities") or {}
            if not isinstance(capabilities, dict):
                raise AdapterError(FailureKind.PARSER, "Prowlarr returned invalid capabilities.")
            result.append(
                ProwlarrIndexer(
                    id=row["id"],
                    name=safe_text(row.get("name"), "Unnamed indexer"),
                    definition=definition,
                    protocol=protocol(row.get("protocol")),
                    enabled=row.get("enable") is True,
                    supports_search=row.get("supportsSearch") is True,
                    supports_pagination=row.get("supportsPagination") is True,
                    categories=categories(capabilities.get("categories")),
                    native_mam=re.sub(r"[^a-z]", "", definition.lower()) == "myanonamouse",
                )
            )
        return result

    def reference(self, value, indexer_id):
        # Accept only the exact configured Prowlarr proxy, not tracker URLs, API
        # mutations, redirects or a provider-selected local-network destination.
        if not isinstance(value, str) or len(value) > 16000:
            return None
        try:
            expected, actual = urlsplit(self.base_url), urlsplit(value)
            if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
                return None
            if actual.path != expected.path + f"/{indexer_id}/download" or actual.fragment:
                return None
            query = parse_qs(actual.query, strict_parsing=True, max_num_fields=3)
            if set(query) - {"apikey", "link", "file"} or any(len(v) != 1 for v in query.values()):
                return None
            link = query.get("link", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,12000}", link):
                return None
            return link
        except ValueError:
            return None

    async def search(self, body):
        cats = {"ebook": [7020], "audio": [3030], "all": [7020, 3030]}[body.medium]
        values = await self.request(
            "api/v1/search",
            params={
                "query": body.q,
                "type": "search",
                "indexerIds": body.indexer_id,
                "categories": cats,
                "offset": body.offset,
                "limit": body.limit,
            },
        )
        if not isinstance(values, list) or len(values) > 2000:
            raise AdapterError(FailureKind.PARSER, "Prowlarr returned an invalid search result.")
        result, seen = [], set()
        for row in values[: body.limit]:
            if not isinstance(row, dict) or row.get("indexerId") != body.indexer_id:
                raise AdapterError(
                    FailureKind.PARSER, "Prowlarr returned results for an unexpected indexer."
                )
            guid, title = row.get("guid"), safe_text(row.get("title"))
            if not isinstance(guid, str) or not guid or len(guid) > 16000 or not title:
                raise AdapterError(FailureKind.PARSER, "Prowlarr returned an unidentified release.")
            source_id = f"{body.indexer_id}:" + hashlib.sha256(guid.encode()).hexdigest()
            if source_id in seen:
                continue
            seen.add(source_id)
            cats = categories(row.get("categories"))
            medium = (
                "ebook"
                if 7020 in cats and 3030 not in cats
                else "audio"
                if 3030 in cats and 7020 not in cats
                else None
            )
            transport = protocol(row.get("protocol"))
            reference = (
                self.reference(row.get("downloadUrl"), body.indexer_id)
                if transport in {"torrent", "nzb"}
                else None
            )
            result.append(
                SearchHit(
                    ProwlarrRelease(
                        source_id=source_id,
                        indexer_id=str(body.indexer_id),
                        indexer_name=safe_text(row.get("indexer")),
                        title=title,
                        raw_title=title,
                        medium=medium,
                        categories=cats,
                        size_bytes=number(row.get("size")) or None,
                        seeders=number(row.get("seeders")),
                        protocol=transport,
                        observed_at=datetime.now(UTC),
                        acquisition_supported=bool(reference),
                        limitation=None
                        if reference
                        else (
                            "A proxied torrent or NZB file is required; direct links "
                            "and magnet-only results cannot be acquired yet."
                        ),
                    ),
                    reference,
                )
            )
        return SearchBatch(result, len(values))

    async def resolve(self, argument):
        release, link = argument
        if not release.acquisition_supported or not re.fullmatch(r"[A-Za-z0-9_-]{1,12000}", link):
            kind = "NZB" if release.protocol == "nzb" else "torrent"
            raise AdapterError(
                FailureKind.UNSUPPORTED, f"This result has no supported {kind} file."
            )
        content = await self.request(
            f"{int(release.indexer_id)}/download",
            params={
                "link": link,
                "file": "book.nzb" if release.protocol == "nzb" else "book.torrent",
            },
            binary=True,
        )
        return ProwlarrArtifact(release, content)
