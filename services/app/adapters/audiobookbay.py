"""Bounded native AudiobookBay HTML observations; file rows are source claims.

Independently implemented from the public page contract. Neither search nor
detail starts a torrent. A separate downloader metadata boundary resolves magnets.
"""

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

from app.adapters.contracts import AdapterError, FailureKind, Release
from app.adapters.http import configured_url
from app.adapters.qbittorrent import magnet_hashes
from app.domain.catalog_network import retry_delay

MAX_HTML = 2 * 1024 * 1024
LANGUAGES = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "russian": "ru",
    "polish": "pl",
    "swedish": "sv",
    "danish": "da",
    "norwegian": "no",
    "finnish": "fi",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
}


class ABBSearch(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    page: int = Field(default=1, ge=1, le=201)

    @field_validator("q")
    @classmethod
    def query(cls, value):
        if not value.strip():
            raise ValueError("Enter a title, author or series")
        return value.strip()


class ClaimedFile(BaseModel):
    path: str
    size_bytes: int | None = None
    evidence: Literal["claimed"] = "claimed"


class ABBRelease(Release):
    source: Literal["audiobookbay"] = "audiobookbay"
    medium: Literal["audio"] = "audio"
    protocol: Literal["torrent"] = "torrent"
    title: str
    detail_path: str
    observed_at: datetime
    acquisition_supported: bool = True
    metadata_resolved: bool = False
    abridged: bool | None = None
    files: list[ClaimedFile] = Field(default_factory=list)
    limitation: str | None = None


@dataclass(frozen=True)
class ABBPage:
    items: list[ABBRelease]
    page: int
    has_more: bool


@dataclass(frozen=True)
class ABBDetail:
    release: ABBRelease
    magnet: str | None = field(repr=False)


def endpoint(value):
    value = configured_url(value)
    if urlsplit(value).path:
        raise ValueError("Use the AudiobookBay site origin without a path")
    return value


def detail_path(value, base_url):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Invalid AudiobookBay posting link")
    target, base = urlsplit(urljoin(base_url + "/", value)), urlsplit(base_url)
    decoded = unquote(target.path)
    if (
        target.scheme not in {"http", "https"}
        or target.netloc.lower() != base.netloc.lower()
        or target.username
        or target.password
        or target.query
        or target.fragment
        or any(ord(c) < 33 or ord(c) == 127 for c in decoded)
        or not re.fullmatch(r"/(?:abss|audio-books)/[^/\\]+/?", decoded)
        or decoded.rstrip("/").split("/")[-1] in {".", ".."}
        or "%" in decoded
    ):
        raise ValueError("Posting links must stay on the configured AudiobookBay origin")
    return target.path.rstrip("/") + "/"


def source_id(path):
    return hashlib.sha256(path.encode()).hexdigest()


def text(node, limit=20000):
    return node.get_text("\n", strip=True).replace("\xa0", " ")[:limit] if node else ""


def field_value(content, labels):
    match = re.search(rf"(?im)^(?:{labels})\s*:\s*([^\n]+)", content)
    return match[1].strip()[:600] if match else None


def size(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(bytes?|[kmgt](?:i?b)s?)", value.strip(), re.I)
    if not match:
        return None
    try:
        unit = match[2].lower()
        amount = int(
            Decimal(match[1]) * 1024 ** (0 if unit.startswith("b") else "kmgt".index(unit[0]) + 1)
        )
        return amount if 0 <= amount <= 2**63 - 1 else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def document(content):
    if len(content.encode("utf-8")) > MAX_HTML:
        raise AdapterError(FailureKind.PARSER, "AudiobookBay page exceeds the size limit.")
    soup = BeautifulSoup(content, "html.parser")
    if soup.select_one(
        'input[type="password"], #challenge-form, .cf-challenge, #cf-challenge-running'
    ):
        raise AdapterError(
            FailureKind.AUTHENTICATION, "AudiobookBay returned a login or challenge page."
        )
    for node in soup.select("script, style, noscript, iframe, template"):
        node.decompose()
    return soup


def posting(post, path):
    heading = post.select_one(".postTitle h2, .postTitle h1")
    raw = " ".join(text(heading, 2000).split())
    if not raw:
        raise AdapterError(FailureKind.PARSER, "AudiobookBay posting title could not be read.")
    content = text(post.select_one(".postContent"))
    info = text(post.select_one(".postInfo"))
    author = field_value(content, "written by|author")
    narrator = field_value(content, "read by|narrated by|narrator")
    # A conventional title suffix is displayed as part of the raw release title;
    # catalog matching separately checks conventional title/credit pairs.
    title = raw.rsplit(" - ", 1)[0] if author and raw.endswith(" - " + author) else raw
    language = field_value(info + "\n" + content, "language")
    formats = sorted(
        set(
            re.findall(
                r"\b(?:m4b|mp3|flac|aac|ogg|opus)\b", (field_value(content, "format") or "").lower()
            )
        )
    )
    # Descriptions may discuss another edition. Only explicit complete labels
    # establish this posting's claim; conflicting labels remain unknown.
    abridgment = {
        match.casefold()
        for match in re.findall(
            r"(?im)^\s*(?:abridgment\s*:\s*)?(unabridged|abridged)\s*$",
            "\n".join((info, content)),
        )
    }
    abridged = next(iter(abridgment)) == "abridged" if len(abridgment) == 1 else None
    return ABBRelease(
        source_id=source_id(path),
        title=title,
        raw_title=raw,
        detail_path=path,
        observed_at=datetime.now(UTC),
        authors=[author] if author else [],
        narrators=[n.strip() for n in re.split(r"\s*;\s*", narrator) if n.strip()]
        if narrator
        else [],
        language=LANGUAGES.get((language or "").lower()),
        formats=formats,
        size_bytes=size(field_value(content, "file size|combined file size") or ""),
        description=content or None,
        abridged=abridged,
        details={
            "bitrate": field_value(content, "bitrate"),
            "language_label": language,
            "posted": field_value(content, "posted"),
            "metadata_evidence": "source-posting",
        },
    )


def parse_search(content, base_url, page):
    soup = document(content)
    posts = soup.select(".post")
    if not posts:
        # A changed layout or an interstitial is not a successful empty search.
        empty = soup.select_one("#content .notfound, #content .no-results")
        empty = empty or next(
            (
                h
                for h in soup.select("#content h2")
                if text(h).casefold() in {"not found", "nothing found", "no results found"}
            ),
            None,
        )
        if not empty:
            raise AdapterError(FailureKind.PARSER, "AudiobookBay search layout was not recognized.")
        return ABBPage([], page, False)
    if len(posts) > 100:
        raise AdapterError(FailureKind.PARSER, "AudiobookBay returned too many postings.")
    items = {}
    for post in posts:
        # ABB's browser script expands base64 HTML inside marked postings.
        # Decode that public markup without running scripts; one encoded row
        # must not hide the rest of an otherwise valid search page.
        if "re-ab" in post.get("class", []):
            try:
                markup = base64.b64decode("".join(post.get_text().split()), validate=True).decode(
                    "utf-8"
                )
            except (binascii.Error, ValueError) as error:
                raise AdapterError(
                    FailureKind.PARSER, "AudiobookBay encoded posting could not be read."
                ) from error
            post = document(markup)
        link = post.select_one(".postTitle h2 a, .postTitle h1 a")
        try:
            path = detail_path(link.get("href") if link else None, base_url)
        except ValueError as error:
            raise AdapterError(
                FailureKind.PARSER, "AudiobookBay returned an invalid posting link."
            ) from error
        release = posting(post, path)
        items[release.source_id] = release
    has_more = False
    for link in soup.select(".wp-pagenavi a, .navigation a, a.next.page-numbers"):
        target = urlsplit(urljoin(base_url + "/", link.get("href", "")))
        if (
            target.netloc == urlsplit(base_url).netloc
            and target.path.rstrip("/") == f"/page/{page + 1}"
        ):
            has_more = True
    return ABBPage(list(items.values()), page, has_more)


def tracker(value):
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"udp", "http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            return None
        if len(value) > 2000 or any(ord(c) < 33 or ord(c) == 127 for c in value):
            return None
        host = parsed.hostname.lower()
        if (
            host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
            or "." not in host
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        _ = parsed.port
        return value.strip()
    except ValueError:
        return None


def magnet_from_post(post):
    hashes = set()
    trackers = set()
    for row in post.select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 2:
            continue
        label, value = (
            text(cells[0], 100).strip().rstrip(":"),
            "".join(text(cells[1], 4000).split()),
        )
        if label.casefold() == "info hash":
            if re.fullmatch(r"[0-9a-fA-F]{40}", value):
                hashes.add("urn:btih:" + value.lower())
            elif re.fullmatch(r"[0-9a-fA-F]{64}", value):
                hashes.add("urn:btmh:1220" + value.lower())
        elif label.casefold().startswith("tracker") and (allowed := tracker(value)):
            trackers.add(allowed)
    for link in post.select('a[href^="magnet:?"]'):
        value = link.get("href", "")
        try:
            magnet_hashes(value)
            args = parse_qs(urlsplit(value).query, max_num_fields=100)
            for xt in args["xt"]:
                if xt.lower().startswith("urn:btih:"):
                    digest = xt[9:]
                    if len(digest) == 32:
                        digest = base64.b32decode(digest.upper()).hex()
                    hashes.add("urn:btih:" + digest.lower())
                else:
                    hashes.add(xt.lower())
            trackers.update(t for raw in args.get("tr", []) if (t := tracker(raw)))
        except ValueError:
            continue
    if not hashes:
        return None
    result = "magnet:?" + urlencode(
        [*(("xt", h) for h in sorted(hashes)), *(("tr", t) for t in sorted(trackers)[:20])]
    )
    try:
        magnet_hashes(result)
    except ValueError as error:
        raise AdapterError(
            FailureKind.PARSER, "AudiobookBay posting has conflicting torrent identities."
        ) from error
    return result


def parse_detail(content, base_url, path):
    path = detail_path(path, base_url)
    soup = document(content)
    posts = soup.select(".post")
    if len(posts) != 1:
        raise AdapterError(FailureKind.PARSER, "AudiobookBay detail layout was not recognized.")
    post = posts[0]
    release = posting(post, path)
    files, claimed = [], []
    for row in post.select("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        label = text(cells[0], 2200)
        if label.casefold().startswith("combined file size"):
            files = claimed
            if len(cells) == 2:
                release.size_bytes = size(text(cells[1], 100))
            break
        if len(cells) != 1 or "multifile torrent" in label.casefold():
            claimed = []
            continue
        match = re.fullmatch(
            r"(.+?)\s+(\d+(?:\.\d+)?\s*(?:bytes?|[kmgt](?:i?b)s?))", label, re.I | re.S
        )
        if match:
            claimed.append(ClaimedFile(path=match[1].strip(), size_bytes=size(match[2])))
            if len(claimed) > 10000:
                raise AdapterError(FailureKind.PARSER, "AudiobookBay file claims exceed the limit.")
    magnet = magnet_from_post(post)
    return ABBDetail(
        release.model_copy(
            update={
                "files": files,
                "acquisition_supported": bool(magnet),
                "limitation": None
                if magnet
                else "No supported magnet identity was found in this posting",
            }
        ),
        magnet,
    )


class _HomepageRedirect(AdapterError):
    """A search redirect to the configured root can be retried without following it."""


class ABBClient:
    def __init__(
        self,
        base_url,
        *,
        proxy_url=None,
        proxy_username=None,
        proxy_password=None,
        transport=None,
        request_interval=2.0,
    ):
        self.base_url = endpoint(base_url)
        self.cooldown = 0
        self.request_interval = request_interval
        self.last_request = 0.0
        proxy = (
            httpx.Proxy(proxy_url, auth=(proxy_username or "", proxy_password or ""))
            if proxy_url and (proxy_username or proxy_password)
            else proxy_url
        )
        self.client = httpx.AsyncClient(
            base_url=self.base_url + "/",
            headers={"User-Agent": "BookSearch/0.1", "Accept": "text/html"},
            trust_env=False,
            follow_redirects=False,
            proxy=proxy,
            transport=transport,
            timeout=httpx.Timeout(30, connect=10),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, path, params=None):
        if self.cooldown:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "AudiobookBay is cooling down.", retry_after=self.cooldown
            )
        delay = self.request_interval - (time.monotonic() - self.last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_request = time.monotonic()
        try:
            async with (
                asyncio.timeout(40),
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
                        "AudiobookBay rejected this request. Check connection diagnostics.",
                        retry_after=max(1, self.cooldown)
                        if kind == FailureKind.RATE_LIMIT
                        else None,
                    )
                if 300 <= response.status_code < 400:
                    error_type = (
                        _HomepageRedirect
                        if self._homepage_redirect(response.headers.get("location", ""))
                        else AdapterError
                    )
                    raise error_type(
                        FailureKind.ROUTE,
                        "AudiobookBay redirected the request. Configure its final site origin.",
                    )
                if response.status_code != 200:
                    raise AdapterError(
                        FailureKind.UNAVAILABLE, "AudiobookBay could not complete the request."
                    )
                if "html" not in response.headers.get("content-type", "").lower():
                    raise AdapterError(
                        FailureKind.PARSER, "AudiobookBay returned an unexpected page type."
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_HTML:
                        raise AdapterError(
                            FailureKind.PARSER, "AudiobookBay page exceeds the size limit."
                        )
                return content.decode("utf-8", errors="replace")
        except (httpx.TimeoutException, TimeoutError) as error:
            raise AdapterError(
                FailureKind.TIMEOUT, "AudiobookBay did not respond in time."
            ) from error
        except httpx.HTTPError as error:
            raise AdapterError(
                FailureKind.ROUTE, "AudiobookBay or its configured proxy could not be reached."
            ) from error

    async def test(self):
        soup = document(await self.request("/"))
        if not soup.select_one('.postTitle, form input[name="s"]'):
            raise AdapterError(FailureKind.PARSER, "AudiobookBay site layout was not recognized.")
        return True

    def _homepage_redirect(self, location):
        if not location:
            return False
        try:
            target = urlsplit(urljoin(self.base_url + "/", location.strip()))
        except ValueError:
            return False
        origin = urlsplit(self.base_url)
        return (
            target.scheme == origin.scheme
            and target.netloc.lower() == origin.netloc.lower()
            and target.path in {"", "/"}
            and not target.query
            and not target.fragment
        )

    async def search(self, query):
        await self.request("/")  # Some hosts initialize a public session cookie here.
        path = "/" if query.page == 1 else f"/page/{query.page}/"
        try:
            html = await self.request(path, {"s": query.q, "cat": "undefined"})
        except _HomepageRedirect:
            lower = query.q.lower()
            if lower == query.q:
                raise
            html = await self.request(path, {"s": lower, "cat": "undefined"})
        return parse_search(html, self.base_url, query.page)

    async def detail(self, path):
        path = detail_path(path, self.base_url)
        await self.request("/")
        return parse_detail(await self.request(path), self.base_url, path)
