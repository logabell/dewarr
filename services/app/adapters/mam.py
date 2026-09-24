"""Native MAM read API.

Request-field adaptations reference MouseSearch (c) 2026 sevenlayercookie and
myanonamouse-mcp (c) 2026 Sandy McArthur, Jr. MIT notices ship in app/notices
and docs/notices; exact upstream revisions are in docs/REUSE-LEDGER.md.
"""

import asyncio
import ipaddress
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.adapters.contracts import AdapterError, FailureKind, Release
from app.adapters.http import configured_url
from app.adapters.mam_transport import route_error
from app.domain.catalog_network import retry_delay

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SEARCH_PATH = "tor/js/loadSearchJSONbasic.php"
VIP_POINTS_PER_WEEK = 1250
VIP_MAX_WEEKS = 12.85
UPLOAD_CREDIT_GB = 50
RATIO_FLOOR = 2.5
BUFFER_FLOOR_GB = 10
BONUS_CEILING = 5000
UPLOAD_CHECK_HOURS = 3
UPLOAD_PURCHASE_CAP = 12
VIP_DOWNLOAD_BLOCKED = "This torrent requires active MyAnonamouse VIP."
SEEDBOX_REFRESH = timedelta(hours=24)
logger = logging.getLogger(__name__)


class PrivateDownloadLogFilter(logging.Filter):
    def filter(self, record):
        # httpx logs complete request URLs at INFO, including MAM passkeys.
        return not any(
            isinstance(value, httpx.URL) and "/tor/download.php/" in value.path
            for value in (record.args if isinstance(record.args, tuple) else ())
        )


logging.getLogger("httpx").addFilter(PrivateDownloadLogFilter())


class AccountAutomation(BaseModel):
    """Account actions an administrator can turn on. All of them default off."""

    seedbox_ip: bool = False
    seedbox_interval_seconds: int = Field(default=300, ge=60, le=86400)
    auto_vip: bool = False
    vip_interval_hours: int = Field(default=24, ge=1, le=168)
    use_wedge: bool = False
    wedge_min_size: bool = False
    wedge_min_size_mb: float = Field(default=0, ge=0, le=10_000_000)
    protect_ratio: bool = False
    ratio_below: float = Field(default=RATIO_FLOOR, gt=0, le=1000)
    ratio_buy_gb: int = Field(default=UPLOAD_CREDIT_GB, ge=50, le=100_000)
    maintain_buffer: bool = False
    buffer_below_gb: float = Field(default=BUFFER_FLOOR_GB, ge=0, le=10_000_000)
    buffer_buy_gb: int = Field(default=UPLOAD_CREDIT_GB, ge=50, le=100_000)
    spend_bonus: bool = False
    bonus_above: int = Field(default=BONUS_CEILING, ge=0, le=100_000_000)
    bonus_buy_gb: int = Field(default=UPLOAD_CREDIT_GB, ge=50, le=100_000)
    upload_interval_hours: int = Field(default=UPLOAD_CHECK_HOURS, ge=1, le=168)

    @field_validator(
        "seedbox_ip",
        "auto_vip",
        "use_wedge",
        "wedge_min_size",
        "protect_ratio",
        "maintain_buffer",
        "spend_bonus",
        mode="before",
    )
    @classmethod
    def strict_switch(cls, value):
        # Reject "yes" and 1. A loose coercion must not turn an action on.
        if not isinstance(value, bool):
            raise ValueError("Use true or false")
        return value


class HelperCommand(BaseModel):
    seedbox: bool = False
    known_ip: str | None = None
    known_asn: str | None = None
    seedbox_stale: bool = False
    vip: bool = False
    upload_ratio: bool = False
    upload_buffer: bool = False
    upload_bonus: bool = False
    ratio_below: float = RATIO_FLOOR
    ratio_buy_gb: int = UPLOAD_CREDIT_GB
    buffer_below_gb: float = BUFFER_FLOOR_GB
    buffer_buy_gb: int = UPLOAD_CREDIT_GB
    bonus_above: int = BONUS_CEILING
    bonus_buy_gb: int = UPLOAD_CREDIT_GB

    @property
    def uploads(self):
        return self.upload_ratio or self.upload_buffer or self.upload_bonus


class HelperResult(BaseModel):
    checked: list[str] = Field(default_factory=list)
    seedbox_ip: str | None = None
    seedbox_asn: str | None = None
    seedbox_authorized: bool = False
    seedbox_unchanged: bool = False
    vip_purchased: bool = False
    upload_purchased: bool = False


def stored_automation(value):
    try:
        return AccountAutomation.model_validate(value or {})
    except ValidationError:
        return AccountAutomation()


def download_path(value, source_id, *, personal_freeleech=False):
    if not isinstance(value, str) or not 0 < len(value) <= 4096:
        raise AdapterError(FailureKind.PARSER, "MAM did not provide a usable download reference.")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise AdapterError(
            FailureKind.PARSER, "MAM returned an invalid download reference."
        ) from error
    segments = [unquote(segment) for segment in parts.path.split("/")]
    if (
        parts.scheme
        or parts.netloc
        or parts.fragment
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
        or any(
            segment in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._~=-]+", segment)
            for segment in segments
        )
    ):
        raise AdapterError(FailureKind.PARSER, "MAM returned an unsupported download reference.")
    try:
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=10)
    except ValueError as error:
        raise AdapterError(
            FailureKind.PARSER, "MAM returned an unsupported download query."
        ) from error
    if any(key.lower() != "tid" for key, _ in query):
        # Personal-freeleech flags require a separate explicit policy. Resolving
        # an artifact must not spend account tokens as a hidden URL side effect.
        raise AdapterError(
            FailureKind.UNSUPPORTED, "MAM download reference has unsupported options."
        )
    path = "tor/download.php/" + parts.path + "?" + urlencode({"tid": source_id})
    # `fl` spends one wedge the account already owns. Only an enabled setting adds it.
    if personal_freeleech:
        path += "&fl"
    return path


class MAMSearch(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    medium: Literal["all", "ebook", "audio"] = "all"
    fields: list[
        Literal["title", "author", "series", "narrator", "description", "tags", "filenames"]
    ] = Field(default=["title", "author", "series"], min_length=1, max_length=7)
    language_ids: list[int] = Field(default=[1], max_length=20)
    sort: Literal["relevance", "seeders"] = "relevance"
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=25, ge=1, le=100)

    @field_validator("q")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Enter a title, author or series")
        return value.strip()

    @field_validator("language_ids")
    @classmethod
    def languages(cls, value):
        if any(item < 1 or item > 10000 for item in value):
            raise ValueError("Use positive MAM language IDs")
        return list(dict.fromkeys(value))

    def payload(self):
        tor = {
            "text": self.q,
            "srchIn": list(dict.fromkeys(self.fields)),
            "searchType": "all",
            "searchIn": "torrents",
            "main_cat": {"all": [13, 14], "audio": [13], "ebook": [14]}[self.medium],
            "sortType": "seeders" if self.sort == "seeders" else "default",
            "startNumber": self.offset,
        }
        if self.language_ids:
            tor["browse_lang"] = self.language_ids
        return {
            "tor": tor,
            "perpage": self.limit,
            "description": "true",
            "isbn": "true",
            "mediaInfo": "true",
        }


class SourceSeries(BaseModel):
    source_id: str
    name: str
    position: str | None = None


class MAMRelease(Release):
    source: Literal["mam"] = "mam"
    title: str
    series: list[SourceSeries] = Field(default_factory=list)
    category: str | None = None
    language_id: int | None = None
    size_display: str | None = None
    filetype_display: str | None = None
    leechers: int | None = None
    snatches: int | None = None
    uploaded_at: str | None = None
    freeleech: bool | None = None
    personal_freeleech: bool | None = None
    vip_freeleech: bool | None = None
    vip: bool | None = None
    tags: list[str] = Field(default_factory=list)
    isbn: str | None = None
    media_info: str | None = None
    observed_at: datetime


@dataclass(frozen=True)
class MAMArtifact:
    release: MAMRelease
    content: bytes = field(repr=False)


class ReleasePage(BaseModel):
    source: Literal["mam"] = "mam"
    items: list[MAMRelease]
    offset: int
    limit: int
    total: int | None
    has_more: bool
    warnings: list[str] = Field(default_factory=list)


def cookie_value(value):
    value = value.strip()
    if value.startswith("mam_id="):
        value = value.removeprefix("mam_id=")
    if (
        not value
        or len(value) > 8192
        or any(ord(char) < 33 or ord(char) > 126 or char in ';,"\\' for char in value)
    ):
        raise ValueError("Enter only the mam_id cookie value")
    return value


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,19}", str(value)):
        return None
    number = int(value)
    return number if number <= 2**63 - 1 else None


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit=1000):
    if not isinstance(value, str):
        return None
    parser = PlainText()
    parser.feed(value[:limit])
    return (
        "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
        or None
    )


def structured(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return None
    return value


def contributors(value):
    parsed = structured(value)
    values = (
        parsed.values() if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
    )
    return list(dict.fromkeys(name for item in list(values)[:100] if (name := plain(item))))


def flag(value):
    if type(value) in {bool, int, str} and value in (0, 1, "0", "1"):
        return value in (1, "1")
    return None


def release(row, observed_at):
    if not isinstance(row, dict) or not (identifier := integer(row.get("id"))):
        raise ValueError("Release ID is missing")
    if not (title := plain(row.get("title"), 2000)):
        raise ValueError("Release title is missing")
    size_display = plain(row.get("size"), 100)
    size = integer(row.get("size"))
    if size is None and size_display:
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?I?B)", size_display.upper())
        if match:
            number = (
                float(match[1])
                * 1024
                ** {
                    "B": 0,
                    "KB": 1,
                    "MB": 2,
                    "GB": 3,
                    "TB": 4,
                    "KIB": 1,
                    "MIB": 2,
                    "GIB": 3,
                    "TIB": 4,
                }[match[2]]
            )
            size = int(number) if number <= 2**63 - 1 else None
    series = []
    series_info = structured(row.get("series_info"))
    if isinstance(series_info, dict):
        for key, item in list(series_info.items())[:100]:
            if isinstance(item, list) and item and (name := plain(item[0])):
                position = (
                    str(item[1])[:100]
                    if len(item) > 1 and isinstance(item[1], (str, int, float))
                    else None
                )
                series.append(SourceSeries(source_id=str(key)[:200], name=name, position=position))
    tags = structured(row.get("tags"))
    if isinstance(tags, dict):
        tags = list(tags.values())
    if not isinstance(tags, list):
        tags = re.split(r"[,;]", row.get("tags", "")) if isinstance(row.get("tags"), str) else []
    filetype = plain(row.get("filetype"), 300)
    return MAMRelease(
        source_id=str(identifier),
        raw_title=row["title"][:2000],
        title=title,
        medium={13: "audio", 14: "ebook"}.get(integer(row.get("main_cat"))),
        authors=contributors(row.get("author_info")),
        narrators=contributors(row.get("narrator_info")),
        series=series,
        category=plain(row.get("catname")),
        language=plain(row.get("lang_code"), 30),
        language_id=integer(row.get("language")),
        size_bytes=size,
        size_display=size_display,
        formats=list(
            dict.fromkeys(
                re.findall(
                    r"\b(?:epub|pdf|mobi|azw3?|m4b|mp3|flac|aac|ogg|opus|cbz|cbr)\b",
                    (filetype or "").lower(),
                )
            )
        ),
        filetype_display=filetype,
        seeders=integer(row.get("seeders")),
        leechers=integer(row.get("leechers")),
        snatches=integer(row.get("times_completed")),
        uploaded_at=plain(row.get("added"), 100),
        freeleech=flag(row.get("free")),
        personal_freeleech=flag(row.get("personal_freeleech")),
        vip_freeleech=flag(row.get("fl_vip")),
        vip=flag(row.get("vip")),
        tags=[text for tag in tags[:100] if (text := plain(tag))],
        isbn=str(row["isbn"])[:200] if isinstance(row.get("isbn"), (str, int)) else None,
        description=plain(row.get("description"), 100000),
        media_info=plain(row.get("mediainfo"), 100000),
        protocol="torrent",
        observed_at=observed_at,
        details={"size_is_estimate": size is not None and integer(row.get("size")) is None},
    )


def parse_page(value, query):
    error = value.get("error")
    if error:
        empty = re.fullmatch(r"Nothing returned, out of (\d+)", str(error).strip())
        if empty and query.offset >= int(empty[1]):
            return ReleasePage(
                items=[],
                offset=query.offset,
                limit=query.limit,
                total=int(empty[1]),
                has_more=False,
            )
        message = str(error).lower()
        if any(token in message for token in ("not signed in", "not logged in", "invalid session")):
            raise AdapterError(
                FailureKind.AUTHENTICATION,
                "MAM rejected this session. Update mam_id for the configured route.",
            )
        raise AdapterError(
            FailureKind.PARSER,
            "MAM returned a source error. Check the connection and search settings.",
        )
    rows = value.get("data")
    if not isinstance(rows, list) or len(rows) > query.limit:
        raise AdapterError(FailureKind.PARSER, "MAM returned an unexpected search page.")
    total = integer(value.get("found"))
    items, warnings, seen = [], [], set()
    if not rows and total is not None and total > query.offset:
        raise AdapterError(FailureKind.PARSER, "MAM omitted results from a nonempty search page.")
    if total is not None and rows and total < query.offset + len(rows):
        total = None
        warnings.append("MAM's result count changed; the total is unknown.")
    now = datetime.now(UTC)
    for row in rows:
        try:
            item = release(row, now)
        except (ValueError, TypeError, RecursionError):
            warnings.append("A malformed source result was omitted; this page is incomplete.")
            continue
        if item.source_id not in seen:
            items.append(item)
            seen.add(item.source_id)
    if rows and not items:
        raise AdapterError(
            FailureKind.PARSER,
            "MAM results could not be decoded. No valid empty-result claim can be made.",
        )
    return ReleasePage(
        items=items,
        offset=query.offset,
        limit=query.limit,
        total=total,
        has_more=query.offset + len(rows) < total
        if total is not None
        else len(rows) == query.limit,
        warnings=list(dict.fromkeys(warnings)),
    )


_BYTE_UNITS = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "")
    if not text or "---" in text:
        return None
    lowered = text.lower()
    if "∞" in text or "inf" in lowered:
        return math.inf
    if "nan" in lowered:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def gigabytes(value):
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?I?B)", value.strip().upper())
        if match:
            return float(match[1]) * _BYTE_UNITS[match[2]] / (1024**3)
        value = number(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if isinstance(value, int) and value >= 1024**2:
        return value / (1024**3)
    return float(value)


def vip_until_from(payload):
    raw = payload.get("vip_until") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def vip_weeks_available(payload, now=None):
    """Weeks of VIP this account can buy without passing the store cap. Zero means skip."""
    now = now or datetime.now(UTC)
    points = number(payload.get("seedbonus") if isinstance(payload, dict) else None)
    if points is None or not math.isfinite(points) or points < VIP_POINTS_PER_WEEK:
        return 0
    expiry = vip_until_from(payload)
    current = (expiry - now).total_seconds() / (7 * 24 * 3600) if expiry and expiry > now else 0
    room = VIP_MAX_WEEKS - current
    if room < 1:
        return 0
    return min(points / VIP_POINTS_PER_WEEK, room)


def _with_bonus(payload, bought):
    points = number(bought.get("seedbonus")) if isinstance(bought, dict) else None
    if points is None or not isinstance(payload, dict):
        return payload
    return {**payload, "seedbonus": points}


def credit_amount(payload, command):
    """GB of upload credit for ratio or buffer. Ratio wins, and bonus is a separate purchase."""
    if not isinstance(payload, dict):
        return None
    if command.upload_ratio:
        ratio = number(payload.get("ratio"))
        if ratio is not None and math.isfinite(ratio) and ratio < command.ratio_below:
            return command.ratio_buy_gb
    if command.upload_buffer:
        uploaded = gigabytes(payload.get("uploaded"))
        downloaded = gigabytes(payload.get("downloaded"))
        if (
            uploaded is not None
            and downloaded is not None
            and uploaded - downloaded < command.buffer_below_gb
        ):
            return command.buffer_buy_gb
    return None


def accepted(payload):
    if not isinstance(payload, dict):
        return False
    return flag(payload.get("success")) is True or flag(payload.get("Success")) is True


def route_identity(payload):
    if not isinstance(payload, dict):
        raise AdapterError(FailureKind.PARSER, "MAM did not report a usable route address.")
    ip = payload.get("ip")
    asn = payload.get("ASN")
    try:
        ip = str(ipaddress.ip_address(ip))
    except (TypeError, ValueError) as error:
        raise AdapterError(
            FailureKind.PARSER, "MAM did not report a usable route address."
        ) from error
    if isinstance(asn, bool) or not isinstance(asn, (int, str)):
        raise AdapterError(FailureKind.PARSER, "MAM did not report a usable network.")
    asn_text = str(asn).strip()
    if not re.fullmatch(r"[0-9]{1,12}", asn_text):
        raise AdapterError(FailureKind.PARSER, "MAM did not report a usable network.")
    return ip, asn_text


def seedbox_update_target(base_url):
    host = (urlsplit(str(base_url)).hostname or "").lower().rstrip(".")
    if host == "myanonamouse.net" or host.endswith(".myanonamouse.net"):
        return "https://t.myanonamouse.net/json/dynamicSeedbox.php"
    return "json/dynamicSeedbox.php"


def spend_wedge(item, vip_until, enabled, now=None, *, min_bytes=None):
    """Apply an owned wedge only when the torrent is not already free for this account."""
    if not enabled or item.freeleech is True or item.personal_freeleech is True:
        return False
    now = now or datetime.now(UTC)
    if item.vip_freeleech is True and vip_until is not None and vip_until > now:
        return False
    if min_bytes is not None and (item.size_bytes is None or item.size_bytes <= min_bytes):
        return False
    return True


def resolve_target(argument):
    requested = False
    source_id = argument
    if isinstance(argument, dict):
        source_id = argument.get("source_id")
        requested = argument.get("use_wedge") is True
    if not isinstance(source_id, str) or not re.fullmatch(r"[1-9][0-9]{0,17}", source_id):
        raise ValueError("Invalid MAM release identifier")
    return source_id, requested


def wedge_limit(settings, explicit):
    """Minimum size applies to automatic wedges. A choice for one torrent ignores it."""
    if explicit or not settings.use_wedge or not settings.wedge_min_size:
        return None
    return int(settings.wedge_min_size_mb * 1024 * 1024)


class MAMClient:
    def __init__(
        self,
        base_url,
        mam_id,
        *,
        proxy_url=None,
        proxy_username=None,
        proxy_password=None,
        transport=None,
        request_interval=2.0,
    ):
        proxy = (
            httpx.Proxy(proxy_url, auth=(proxy_username or "", proxy_password or ""))
            if proxy_url and (proxy_username or proxy_password)
            else proxy_url
        )
        self.client = httpx.AsyncClient(
            base_url=configured_url(base_url) + "/",
            headers={
                "Cookie": f"mam_id={cookie_value(mam_id)}",
                "Accept": "application/json",
                "User-Agent": "BookSearch/0.1 (self-hosted MAM client)",
            },
            proxy=proxy,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(25, connect=10),
            transport=transport,
        )
        self.rotated_cookie = None
        self.automation = AccountAutomation()
        self.uses_proxy = bool(proxy_url)
        self.request_interval = request_interval
        self.cooldown = 0

    @property
    def use_wedge(self):
        return self.automation.use_wedge

    @use_wedge.setter
    def use_wedge(self, value):
        self.automation = self.automation.model_copy(update={"use_wedge": value is True})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, path, payload=None, *, binary=False, params=None, authenticated=True):
        headers = {}
        if binary:
            headers["Accept"] = "application/x-bittorrent"
        if not authenticated:
            # Route checks identify the network. They must not send the session cookie.
            headers["Cookie"] = ""
        try:
            async with (
                asyncio.timeout(40),
                self.client.stream(
                    "POST" if payload else "GET",
                    path,
                    json=payload,
                    params=params,
                    headers=headers or None,
                ) as response,
            ):
                self.cooldown = retry_delay(dict(response.headers), datetime.now(UTC))
                # Only the named session cookie from this non-redirected response is persisted.
                host = response.request.url.host
                cookies = [
                    cookie
                    for cookie in response.cookies.jar
                    if cookie.name == "mam_id"
                    and (
                        host == cookie.domain.lstrip(".")
                        or host.endswith("." + cookie.domain.lstrip("."))
                    )
                    and response.status_code in {200, 429}
                ]
                if len(cookies) == 1:
                    try:
                        self.rotated_cookie = cookie_value(cookies[0].value)
                        self.client.headers["Cookie"] = f"mam_id={self.rotated_cookie}"
                    except ValueError:
                        pass
                status = response.status_code
                if status in {401, 403}:
                    raise AdapterError(
                        FailureKind.AUTHENTICATION,
                        "MAM rejected this session. Update mam_id for the configured route.",
                    )
                if status == 429:
                    self.cooldown = max(60, self.cooldown)
                    raise AdapterError(
                        FailureKind.RATE_LIMIT,
                        "MAM is limiting requests. Wait before retrying.",
                        retry_after=int(self.cooldown),
                    )
                if status == 407 or 300 <= status < 400:
                    error = AdapterError(
                        FailureKind.ROUTE,
                        "The source or proxy rejected the route. Check connection settings.",
                    )
                    error.proxy_retryable = self.uses_proxy and status == 407
                    raise error
                if status != 200:
                    error = AdapterError(
                        FailureKind.UNAVAILABLE, "MAM could not complete the request."
                    )
                    error.proxy_retryable = self.uses_proxy and status in {502, 503, 504}
                    raise error
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise AdapterError(
                            FailureKind.PARSER, "MAM's response exceeded the supported page limit."
                        )
                if "text/html" in response.headers.get("content-type", "").lower():
                    raise AdapterError(
                        FailureKind.AUTHENTICATION,
                        "MAM returned a login or challenge page. Check the session and route.",
                    )
                if binary:
                    if not content or not content.startswith(b"d"):
                        raise AdapterError(
                            FailureKind.PARSER, "MAM did not return torrent metadata."
                        )
                    return bytes(content)
                try:
                    value = json.loads(content)
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise AdapterError(
                        FailureKind.PARSER, "MAM returned unreadable JSON."
                    ) from error
                if not isinstance(value, dict):
                    raise AdapterError(FailureKind.PARSER, "MAM returned an unexpected response.")
                return value
        except (httpx.TimeoutException, TimeoutError) as error:
            failure = AdapterError(
                FailureKind.TIMEOUT,
                route_error(error, proxy=True)
                if self.uses_proxy
                else "MAM did not respond in time.",
            )
            failure.proxy_retryable = self.uses_proxy
            raise failure from error
        except httpx.HTTPError as error:
            message = route_error(error, proxy=self.uses_proxy)
            if self.uses_proxy:
                message += " No direct fallback was attempted."
            failure = AdapterError(
                FailureKind.ROUTE,
                message,
            )
            failure.proxy_retryable = self.uses_proxy
            raise failure from error

    async def search(self, query):
        return parse_page(await self.request(SEARCH_PATH, query.payload()), query)

    async def detail(self, source_id):
        query = MAMSearch(q="detail", limit=1)
        payload = query.payload()
        payload["tor"] = {
            "id": int(source_id),
            "searchType": "all",
            "searchIn": "torrents",
            "startNumber": 0,
        }
        page = parse_page(await self.request(SEARCH_PATH, payload), query)
        if not page.items:
            raise AdapterError(FailureKind.NOT_FOUND, "This MAM release is no longer available.")
        if len(page.items) != 1 or page.items[0].source_id != source_id:
            raise AdapterError(
                FailureKind.PARSER, "MAM returned a different release than requested."
            )
        return page.items[0]

    async def test(self):
        value = await self.request("jsonLoad.php")
        if not integer(value.get("uid")) or not isinstance(value.get("username"), str):
            raise AdapterError(
                FailureKind.AUTHENTICATION,
                "MAM did not confirm an authenticated account. Check mam_id and route.",
            )
        return None

    async def resolve(self, argument):
        source_id, requested = resolve_target(argument)
        query = MAMSearch(q="detail", limit=1)
        payload = query.payload()
        payload["dlLink"] = "true"
        payload["tor"] = {
            "id": int(source_id),
            "searchType": "all",
            "searchIn": "torrents",
            "startNumber": 0,
        }
        value = await self.request(SEARCH_PATH, payload)
        page = parse_page(value, query)
        if not page.items:
            raise AdapterError(FailureKind.NOT_FOUND, "This MAM release is no longer available.")
        if (
            len(page.items) != 1
            or page.items[0].source_id != source_id
            or len(value.get("data", [])) != 1
        ):
            raise AdapterError(
                FailureKind.PARSER, "MAM returned a different release than requested."
            )
        item = page.items[0]
        enabled = requested or self.automation.use_wedge
        vip_until = None
        if item.vip is True or (enabled and item.vip_freeleech is True):
            if self.cooldown:
                raise AdapterError(
                    FailureKind.RATE_LIMIT,
                    "MAM requested a cooldown before fetching torrent metadata. Retry later.",
                    retry_after=int(self.cooldown),
                )
            await asyncio.sleep(self.request_interval)
            vip_until = vip_until_from(await self.request("jsonLoad.php"))
            if item.vip is True and not (vip_until and vip_until > datetime.now(UTC)):
                raise AdapterError(FailureKind.UNSUPPORTED, VIP_DOWNLOAD_BLOCKED)
        path = download_path(
            value["data"][0].get("dl"),
            source_id,
            personal_freeleech=spend_wedge(
                item,
                vip_until,
                enabled,
                min_bytes=wedge_limit(self.automation, requested),
            ),
        )
        if self.cooldown:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "MAM requested a cooldown before fetching torrent metadata. Retry later.",
                retry_after=int(self.cooldown),
            )
        await asyncio.sleep(self.request_interval)
        content = await self.request(path, binary=True)
        return MAMArtifact(release=item, content=content)

    async def _pause(self):
        if self.cooldown:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "MAM requested a cooldown. Account automation will retry later.",
                retry_after=int(self.cooldown) or 60,
            )
        await asyncio.sleep(self.request_interval)

    async def _buy(self, spendtype, extra):
        params = {
            "spendtype": spendtype,
            "_": int(datetime.now(UTC).timestamp() * 1000),
            **extra,
        }
        return await self.request("json/bonusBuy.php", params=params)

    async def maintain(self, command):
        if not isinstance(command, HelperCommand):
            command = HelperCommand.model_validate(command)
        result = HelperResult()
        if not (command.seedbox or command.vip or command.uploads):
            return result
        if command.seedbox:
            seen = await self.request("json/jsonIp.php", authenticated=False)
            ip, asn = route_identity(seen)
            result.seedbox_ip = ip
            result.seedbox_asn = asn
            result.checked.append("seedbox")
            if ip != command.known_ip or asn != command.known_asn or command.seedbox_stale:
                await self._pause()
                update = await self.request(seedbox_update_target(str(self.client.base_url)))
                result.seedbox_authorized = accepted(update)
                if result.seedbox_authorized:
                    logger.info("Account automation updated the dynamic seedbox")
            else:
                result.seedbox_unchanged = True
        if command.vip or command.uploads:
            if result.checked:
                await self._pause()
            payload = await self.request("jsonLoad.php")
            if command.vip:
                result.checked.append("vip")
                if vip_weeks_available(payload) >= 1:
                    await self._pause()
                    result.vip_purchased = accepted(await self._buy("VIP", {"duration": "max"}))
                    if result.vip_purchased:
                        logger.info("Account automation purchased VIP")
                        await self._pause()
                        payload = await self.request("jsonLoad.php")
            if command.uploads:
                result.checked.append("upload")
                result.upload_purchased = await self._buy_upload(payload, command)
        return result

    async def _buy_upload(self, payload, command):
        purchased = False
        amount = credit_amount(payload, command)
        if amount:
            await self._pause()
            bought = await self._buy("upload", {"amount": amount})
            purchased = accepted(bought)
            if purchased:
                logger.info("Account automation purchased upload credit")
                payload = _with_bonus(payload, bought)
        if not command.upload_bonus:
            return purchased
        for _ in range(UPLOAD_PURCHASE_CAP):
            points = number(payload.get("seedbonus")) if isinstance(payload, dict) else None
            if points is None or not math.isfinite(points) or points <= command.bonus_above:
                break
            await self._pause()
            bought = await self._buy("upload", {"amount": command.bonus_buy_gb})
            if not accepted(bought):
                break
            purchased = True
            logger.info("Account automation purchased upload credit")
            nxt = number(bought.get("seedbonus")) if isinstance(bought, dict) else None
            if nxt is None or nxt >= points:
                break
            payload = {**payload, "seedbonus": nxt}
        return purchased
