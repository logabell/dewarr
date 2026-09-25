import base64
import io
import logging

import httpx
import pytest
from PIL import Image

from app.importing import covers
from app.importing.cover_image import MAX_INPUT, normalize
from app.importing.covers import CoverError, download_cover, normalize_cover
from app.importing.naming import fingerprint
from app.importing.publication import PublicationSpec, publish_item, specification_fingerprint
from tests.media_fixtures import cover_bytes
from tests.unit.test_import_publication import specification  # noqa: F401


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for start in range(0, len(self.data), 65536):
            yield self.data[start : start + 65536]


async def public(host):
    return ["1.1.1.1"]


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
async def test_cover_decoder_normalizes_supported_rasters_without_metadata(format):
    exif = Image.Exif()
    exif[270] = "Private input metadata"
    output = await normalize_cover(cover_bytes(size=(1600, 800), format=format, exif=exif))
    with Image.open(io.BytesIO(output)) as image:
        image.load()
        assert image.format == "JPEG" and image.size == (1200, 600)
        assert not image.getexif() and not image.info.get("icc_profile")
    assert b"Private input metadata" not in output


@pytest.mark.parametrize(
    "data", [b"<svg/>", b"<html>not an image</html>", b"bad JPEG", cover_bytes(size=(1, 1))]
)
async def test_cover_decoder_rejects_non_raster_truncated_and_placeholder_inputs(data):
    with pytest.raises(CoverError, match="decoded"):
        await normalize_cover(data)


def test_cover_decoder_rejects_pixel_bombs_and_animation():
    with pytest.raises(ValueError, match="dimensions"):
        normalize(cover_bytes(size=(4200, 4000)))
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(
        output,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (64, 64), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError, match="Animated"):
        normalize(output.getvalue())


async def test_cover_fetch_pins_dns_preserves_tls_host_and_omits_signed_url_logs(caplog):
    requests = []
    data = cover_bytes()

    async def handle(request):
        requests.append(request)
        assert request.url.host == "1.1.1.1"
        assert request.headers["host"] == "assets.hardcover.app"
        assert request.extensions["sni_hostname"] == "assets.hardcover.app"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=Bytes(data))

    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await download_cover(
            "https://assets.hardcover.app/cover.png?signature=fixture-secret",
            transport=httpx.MockTransport(handle),
            resolver=public,
        )
    assert result == data and len(requests) == 1
    assert "fixture-secret" not in caplog.text


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "224.0.0.1", "64:ff9b::7f00:1"]
)
async def test_cover_fetch_rejects_nonpublic_resolution_before_connection(address):
    async def resolve(host):
        return [address]

    async def forbidden(request):
        pytest.fail("A private/translated cover route must never receive a request")

    with pytest.raises(CoverError, match="public"):
        await download_cover(
            "https://assets.hardcover.app/cover.jpg",
            resolver=resolve,
            transport=httpx.MockTransport(forbidden),
        )


@pytest.mark.parametrize(
    "location",
    [
        "http://assets.hardcover.app/cover.jpg",
        "https://localhost/cover.jpg",
        "https://assets.hardcover.app:8443/cover.jpg",
        "https://user:secret@assets.hardcover.app/cover.jpg",
    ],
)
async def test_cover_redirects_cannot_escape_supported_https_hosts(location):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(CoverError, match="HTTPS"):
        await download_cover(
            "https://assets.hardcover.app/cover.jpg",
            resolver=public,
            transport=httpx.MockTransport(handle),
        )
    assert len(requests) == 1


async def test_redirect_rechecks_dns_and_does_not_forward_cookies():
    hosts = []
    requests = []

    async def resolve(host):
        hosts.append(host)
        return ["1.1.1.1"]

    async def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "/new.jpg", "set-cookie": "secret=value; Path=/"}
            )
        assert "cookie" not in request.headers
        return httpx.Response(
            200, headers={"content-type": "image/png"}, stream=Bytes(cover_bytes())
        )

    await download_cover(
        "https://assets.hardcover.app/old.jpg",
        resolver=resolve,
        transport=httpx.MockTransport(handle),
    )
    assert hosts == ["assets.hardcover.app"] * 2


ARCHIVE_DOWNLOAD = "https://archive.org/download/olcovers24/olcovers24-L.zip/240727-L.jpg"
ARCHIVE_IMAGE = (
    "https://ia800603.us.archive.org/view_archive.php"
    "?archive=/35/items/olcovers24/olcovers24-L.zip&file=240727-L.jpg"
)


async def test_openlibrary_archive_redirect_keeps_exact_member_and_public_tls_route():
    hosts = []
    requests = []
    data = cover_bytes()

    async def resolve(host):
        hosts.append(host)
        return ["1.1.1.1"]

    async def handle(request):
        requests.append(request)
        assert request.url.host == "1.1.1.1"
        assert request.extensions["sni_hostname"] == request.headers["host"]
        assert "cookie" not in request.headers
        if len(requests) < 3:
            return httpx.Response(
                302,
                headers={
                    "location": [ARCHIVE_DOWNLOAD, ARCHIVE_IMAGE][len(requests) - 1],
                    "set-cookie": "unneeded=value; Path=/",
                },
            )
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=Bytes(data))

    assert (
        await download_cover(
            "https://covers.openlibrary.org/b/id/240727-L.jpg",
            resolver=resolve,
            transport=httpx.MockTransport(handle),
        )
        == data
    )
    assert hosts == ["covers.openlibrary.org", "archive.org", "ia800603.us.archive.org"]


@pytest.mark.parametrize(
    "location",
    [
        ARCHIVE_IMAGE.replace("240727-L.jpg", "other.jpg"),
        ARCHIVE_IMAGE.replace("olcovers24-L.zip", "olcovers25-L.zip"),
        ARCHIVE_IMAGE.replace("ia800603.us.archive.org", "archive.org.attacker.example"),
        ARCHIVE_IMAGE.replace("ia800603.us.archive.org", "arbitrary.archive.org"),
        ARCHIVE_IMAGE.replace("https:", "http:"),
        ARCHIVE_IMAGE + "&file=240727-L.jpg",
        ARCHIVE_IMAGE.replace("/35/items/", "/35/../items/"),
        "https://archive.org/download/unrelated/book.epub",
    ],
)
async def test_archive_redirect_cannot_switch_member_route_or_host(location):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": ARCHIVE_DOWNLOAD if len(requests) == 1 else location}
        )

    with pytest.raises(CoverError, match="HTTPS"):
        await download_cover(
            "https://covers.openlibrary.org/b/id/240727-L.jpg",
            resolver=public,
            transport=httpx.MockTransport(handle),
        )
    assert len(requests) == 2


async def test_archive_urls_are_not_accepted_directly_or_from_hardcover():
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": ARCHIVE_DOWNLOAD})

    for start in (ARCHIVE_DOWNLOAD, "https://assets.hardcover.app/cover.jpg"):
        with pytest.raises(CoverError, match="HTTPS"):
            await download_cover(start, resolver=public, transport=httpx.MockTransport(handle))
    assert len(requests) == 1


async def test_archive_redirect_rechecks_public_resolution():
    requests = []

    async def resolve(host):
        return ["1.1.1.1"] if host == "covers.openlibrary.org" else ["127.0.0.1"]

    async def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": ARCHIVE_DOWNLOAD})

    with pytest.raises(CoverError, match="public"):
        await download_cover(
            "https://covers.openlibrary.org/b/id/240727-L.jpg",
            resolver=resolve,
            transport=httpx.MockTransport(handle),
        )
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_cover_connect_failure_tries_next_validated_address(failure):
    requests = []
    resolutions = []
    data = cover_bytes()

    async def resolve(host):
        resolutions.append(host)
        return ["1.1.1.1", "8.8.8.8"]

    async def handle(request):
        requests.append(request.url.host)
        if len(requests) == 1:
            raise failure("Fixture network unavailable", request=request)
        assert request.headers["host"] == "assets.hardcover.app"
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=Bytes(data))

    assert (
        await download_cover(
            "https://assets.hardcover.app/cover.jpg",
            resolver=resolve,
            transport=httpx.MockTransport(handle),
        )
        == data
    )
    assert requests == ["1.1.1.1", "8.8.8.8"]
    assert resolutions == ["assets.hardcover.app"]


async def test_decoder_start_failure_reports_optional_artwork_error(monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("Private host process diagnostic")

    monkeypatch.setattr(covers.asyncio, "create_subprocess_exec", fail)
    with pytest.raises(CoverError, match="could not be started"):
        await normalize_cover(cover_bytes())


@pytest.mark.parametrize("kind", ["length", "stream", "encoding", "html"])
async def test_cover_transfer_bounds(kind):
    async def handle(request):
        headers = {"content-type": "image/jpeg"}
        data = b"image bytes"
        if kind == "length":
            headers["content-length"] = str(MAX_INPUT + 1)
        if kind == "stream":
            data = b"x" * (MAX_INPUT + 1)
        if kind == "encoding":
            headers["content-encoding"] = "gzip"
            data = b""  # Reject compression before consuming its stream.
        if kind == "html":
            headers["content-type"] = "text/html"
        return httpx.Response(200, headers=headers, stream=Bytes(data))

    with pytest.raises(CoverError):
        await download_cover(
            "https://assets.hardcover.app/cover.jpg",
            resolver=public,
            transport=httpx.MockTransport(handle),
        )


def test_cover_publication_is_independent_frozen_and_recoverable(specification):  # noqa: F811
    content = normalize(cover_bytes())
    spec = PublicationSpec.model_validate(
        {
            **specification.model_dump(),
            "binary_sidecars": {"cover.jpg": base64.b64encode(content).decode()},
        }
    )

    def crash(phase):
        if phase == "published-before-receipt":
            raise RuntimeError("Simulated cover publication crash")

    with pytest.raises(RuntimeError):
        publish_item(spec, checkpoint=crash)
    receipt = publish_item(spec)
    cover = spec.destination_root / spec.folder / "cover.jpg"
    assert cover.read_bytes() == content and cover.stat().st_nlink == 1
    assert publish_item(spec) == receipt
    assert not list(spec.source_root.rglob("*.jpg"))


@pytest.mark.parametrize("with_cover", [False, True])
def test_legacy_publication_fingerprints_remain_stable(specification, with_cover):  # noqa: F811
    if with_cover:
        specification = specification.model_copy(
            update={
                "binary_sidecars": {
                    "cover.jpg": base64.b64encode(normalize(cover_bytes())).decode()
                },
            }
        )
    old = specification.model_dump(mode="json")
    # Legacy eras predate separate journals, source_kind and chapter merging;
    # only the oldest predates artwork.
    old.pop("journal_root")
    old.pop("source_kind")
    old.pop("conversion")
    if not with_cover:
        old.pop("binary_sidecars")
    assert specification_fingerprint(specification) == fingerprint(old)
