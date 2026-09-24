"""Public catalog-cover retrieval, with pinned DNS and bounded raster decoding."""

import asyncio
import contextvars
import logging
import re
import socket
import sys
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from app.adapters.catalog_types import cover_url
from app.importing.cover_image import MAX_INPUT, MAX_OUTPUT
from app.network_addresses import public_address

_cover_request = contextvars.ContextVar("cover_request", default=False)


class _CoverLogFilter(logging.Filter):
    def filter(self, record):
        # HTTPX INFO includes full URLs. Signed image queries must not enter logs.
        return not _cover_request.get()


logging.getLogger("httpx").addFilter(_CoverLogFilter())


class CoverError(ValueError):
    pass


def archive_member(value):
    """Recognize only the Open Library cover member route observed in redirects."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.hostname != "archive.org" or parts.query:
        return None
    match = re.fullmatch(r"/download/(olcovers\d+)/(\1-([SML])\.zip)/(\d+-\3\.jpg)", parts.path)
    return (match[1], match[2], match[4]) if match else None


def permitted_archive_url(parts, member):
    if not member:
        return False
    item, bundle, filename = member
    if parts.hostname == "archive.org":
        return parts.path == f"/download/{item}/{bundle}/{filename}" and not parts.query
    if not re.fullmatch(r"ia\d+\.(?:us|eu)\.archive\.org", parts.hostname or ""):
        return False
    if parts.path != "/view_archive.php":
        return False
    query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=2)
    if set(query) != {"archive", "file"} or query["file"] != [filename]:
        return False
    return len(query["archive"]) == 1 and bool(
        re.fullmatch(rf"/\d+/items/{re.escape(item)}/{re.escape(bundle)}", query["archive"][0])
    )


def validated_url(value, *, member=None):
    try:
        parts = urlsplit(value)
        if (
            (cover_url(value) != value and not permitted_archive_url(parts, member))
            or parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.fragment
            or "\\" in value
            or any(ord(character) < 33 for character in value)
        ):
            raise ValueError
        return parts.hostname
    except (ValueError, TypeError):
        raise CoverError("The selected cover is not on a supported HTTPS image host") from None


async def public_addresses(host):
    try:
        records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not public_address(address) for address in addresses):
            raise CoverError("The cover host did not resolve exclusively to public addresses")
        return addresses
    except OSError:
        raise CoverError("The cover host could not be resolved") from None


async def download_cover(url, *, transport=None, resolver=public_addresses):
    marker = _cover_request.set(True)
    try:
        member = None
        validated_url(url)
        async with (
            asyncio.timeout(30),
            httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10, connect=5),
                headers={
                    "Accept": "image/jpeg,image/png,image/webp",
                    "Accept-Encoding": "identity",
                    "User-Agent": "BookSearch/0.1 (catalog cover export)",
                },
            ) as client,
        ):
            for _ in range(4):
                host = validated_url(url, member=member)
                addresses = await resolver(host)
                if not addresses or any(not public_address(address) for address in addresses):
                    raise CoverError("The cover route is not a public address")
                # Pin this actual connection to the checked address, retaining TLS
                # hostname validation and Host. No second DNS lookup can rebind it.
                async with AsyncExitStack() as streams:
                    response = None
                    for address in addresses[:4]:
                        target = httpx.URL(url).copy_with(host=address)
                        client.cookies.clear()
                        try:
                            response = await streams.enter_async_context(
                                client.stream(
                                    "GET",
                                    target,
                                    headers={"Host": host},
                                    extensions={"sni_hostname": host},
                                )
                            )
                            break
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            # Try only already validated addresses, within the
                            # same total deadline. Never retry a partial body.
                            continue
                    if response is None:
                        raise CoverError("The cover service could not be reached")
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise CoverError("The cover service returned an incomplete redirect")
                        next_url = urljoin(url, location)
                        if host == "covers.openlibrary.org" and member is None:
                            member = archive_member(next_url)
                        validated_url(next_url, member=member)
                        url = next_url
                        continue
                    if response.status_code != 200:
                        raise CoverError("The cover service did not return an available image")
                    if response.headers.get("content-encoding", "identity").lower() not in {
                        "",
                        "identity",
                    }:
                        raise CoverError("Compressed cover responses are unsupported")
                    kind = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if kind not in {"image/jpeg", "image/png", "image/webp"}:
                        raise CoverError("The cover response is not a supported raster image")
                    try:
                        length = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        raise CoverError("The cover response has an invalid size") from None
                    if length < 0 or length > MAX_INPUT:
                        raise CoverError("The cover response exceeds the size limit")
                    content = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(content) + len(chunk) > MAX_INPUT:
                            raise CoverError("The cover response exceeds the size limit")
                        content.extend(chunk)
                    return bytes(content)
            raise CoverError("The cover service redirected too many times")
    except (httpx.HTTPError, TimeoutError, OSError):
        raise CoverError("The cover service could not be reached within its time limit") from None
    finally:
        _cover_request.reset(marker)


async def normalize_cover(data):
    if len(data) > MAX_INPUT:
        raise CoverError("The cover response exceeds the size limit")
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.importing.cover_image",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise CoverError("The cover decoder could not be started") from None
    try:
        async with asyncio.timeout(15):
            output, _ = await process.communicate(data)
        if process.returncode or not output or len(output) > MAX_OUTPUT:
            raise CoverError("The cover could not be decoded within image limits")
        return output
    except TimeoutError:
        raise CoverError("The cover decoder exceeded its time limit") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def fetch_cover(url):
    return await normalize_cover(await download_cover(url))
