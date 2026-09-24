import json
import socket
import ssl
from uuid import uuid4

import httpcore
import httpx
import pytest

from app.notifications import channels
from app.notifications.events import public_payload, safe_text


def test_payload_scrubs_private_urls_and_credentials():
    assert "secret" not in safe_text("Held at https://host/private?token=secret")
    assert safe_text("Cookie: session=private") == "[private detail]"
    with pytest.raises(ValueError):
        public_payload("Title", "Message", "//private.example")
    with pytest.raises(ValueError):
        public_payload("Title", "Message", "/requests?token=private")
    assert "cover_url" not in public_payload(
        "Title", "Message", "/requests", "https://private/cover?token=secret"
    )
    assert public_payload(
        "Title", "Message", "/requests", "https://assets.hardcover.app/cover.jpg"
    )["cover_url"].endswith("cover.jpg")


@pytest.mark.parametrize("kind", ["discord", "ntfy", "webhook"])
async def test_http_channel_payloads_and_redacted_errors(monkeypatch, kind):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        channels.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    payload = {
        "schema_version": 1,
        "delivery_id": str(uuid4()),
        "events": [
            {
                "id": str(uuid4()),
                "type": "import.available",
                "title": "Available",
                "message": "A book is ready",
                "url": "https://dewarr.example/requests",
                "cover_url": "https://assets.hardcover.app/book.jpg",
            }
        ],
    }
    url = (
        "https://discord.com/api/webhooks/123/fixture"
        if kind == "discord"
        else "https://fixture.invalid/topic"
    )
    await channels.send(
        kind, {"url": url, "token": "synthetic-token"}, payload, private_allowed=True
    )
    request = requests[0]
    assert request.headers["Idempotency-Key"] == payload["delivery_id"]
    if kind == "discord":
        data = json.loads(request.content)
        assert data["allowed_mentions"] == {"parse": []}
        assert data["embeds"][0]["thumbnail"]["url"].endswith("book.jpg")
    elif kind == "ntfy":
        assert request.headers["Click"] == payload["events"][0]["url"]
        assert "A book is ready" in request.content.decode()
    else:
        assert json.loads(request.content) == payload


async def test_apprise_adapter_uses_all_urls(monkeypatch):
    import apprise

    added = []

    async def notify(self, **kwargs):
        assert "ready" in kwargs["body"]
        return True

    monkeypatch.setattr(apprise.Apprise, "add", lambda self, value: added.append(value) or True)
    monkeypatch.setattr(apprise.Apprise, "async_notify", notify)
    await channels.send(
        "apprise",
        {"urls": ["ntfy://one", "ntfy://two"]},
        {
            "events": [
                {"title": "Book", "message": "ready", "url": "https://dewarr.example/requests"}
            ]
        },
        private_allowed=True,
    )
    assert added[-1] == ["ntfy://one", "ntfy://two"]


async def test_personal_destination_blocks_loopback():
    with pytest.raises(ValueError, match="public addresses"):
        await channels.check_public_destination("http://127.0.0.1/private")


@pytest.fixture
def payload():
    return {
        "delivery_id": str(uuid4()),
        "events": [{"title": "Book", "message": "Ready", "url": "https://dewarr.example/"}],
    }


def dns_record(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))


@pytest.mark.parametrize("kind", ["webhook", "ntfy", "discord"])
@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
async def test_delivery_pins_dns_at_the_tcp_boundary(monkeypatch, payload, kind, address):
    url = (
        "https://discord.com/api/webhooks/123/fixture"
        if kind == "discord"
        else "https://notifications.example:8443/topic?key=fixture"
        if kind == "webhook"
        else "https://notifications.example:8443/topic"
    )
    host = httpx.URL(url).host
    resolutions, connections, tls, writes = [], [], [], []

    def resolve(name, port, *args, **kwargs):
        if name == host:
            # A second lookup of the original name would rebind to loopback.
            result = address if not resolutions else "127.0.0.1"
            resolutions.append(result)
        else:
            result = name  # Numeric destination; no attacker-controlled DNS lookup.
        return [dns_record(result, port)]

    class Stream(httpcore.AsyncMockStream):
        async def write(self, buffer, timeout=None):  # noqa: ASYNC109 - transport interface
            writes.append(buffer)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109
            tls.append(server_hostname)
            assert ssl_context.check_hostname
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            return self

    async def connect(self, host, port, **kwargs):
        actual = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        connections.append((host, actual, port))
        return Stream([b"HTTP/1.1 204 No Content\r\n\r\n"])

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    await channels.send(kind, {"url": url}, payload, private_allowed=False)
    assert resolutions == [address]
    assert connections == [(address, address, 443 if kind == "discord" else 8443)]
    assert tls == [host]
    authority = host if kind == "discord" else host + ":8443"
    assert f"Host: {authority}\r\n".encode() in b"".join(writes)


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["127.0.0.1"],
        ["8.8.8.8", "10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["2002:7f00:1::"],
        ["64:ff9b::7f00:1"],
        ["ff02::1"],
        ["224.0.0.1"],
    ],
)
async def test_personal_delivery_rejects_unsafe_dns_before_connect(monkeypatch, payload, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [dns_record(address, port) for address in addresses],
    )

    async def unexpected_connect(*args, **kwargs):
        pytest.fail("Unsafe destination reached the TCP transport")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", unexpected_connect)
    with pytest.raises(ValueError, match="public addresses"):
        await channels.send(
            "webhook", {"url": "http://notifications.example/hook"}, payload, private_allowed=False
        )


@pytest.mark.parametrize(
    "outcome", ["connect-error", "connect-timeout", "write-error", "read-timeout", 503, 307]
)
async def test_failover_uses_only_checked_ips_and_never_replays_a_post(
    monkeypatch, payload, outcome
):
    resolutions, requests = [], []

    def resolve(host, port, *args, **kwargs):
        resolutions.append(host)
        return [dns_record(address, port) for address in ["8.8.8.8", "1.1.1.1"]]

    def respond(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(204)
        failures = {
            "connect-error": httpx.ConnectError,
            "connect-timeout": httpx.ConnectTimeout,
            "write-error": httpx.WriteError,
            "read-timeout": httpx.ReadTimeout,
        }
        if outcome in failures:
            raise failures[outcome]("Synthetic transport failure")
        return httpx.Response(outcome, headers={"Location": "http://127.0.0.1/private"})

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        channels.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    args = ("webhook", {"url": "https://notifications.example/hook"}, payload)
    if outcome in {"connect-error", "connect-timeout"}:
        await channels.send(*args, private_allowed=False)
        assert [request.url.host for request in requests] == ["8.8.8.8", "1.1.1.1"]
    else:
        with pytest.raises((httpx.TransportError, channels.DeliveryError)):
            await channels.send(*args, private_allowed=False)
        assert len(requests) == 1
    assert resolutions == ["notifications.example"]


async def test_personal_apprise_is_blocked_before_plugin_execution(monkeypatch, payload):
    import apprise

    def unexpected_add(*args, **kwargs):
        pytest.fail("Untrusted Apprise plugin was loaded")

    monkeypatch.setattr(apprise.Apprise, "add", unexpected_add)
    with pytest.raises(channels.DeliveryError, match="requires an administrator"):
        await channels.send(
            "apprise", {"urls": ["json://notifications.example"]}, payload, private_allowed=False
        )
