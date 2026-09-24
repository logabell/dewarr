import json
from uuid import uuid4

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
