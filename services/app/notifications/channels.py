"""Channel adapters. Errors returned to the UI never include response bodies or URLs."""

import asyncio
import logging
import socket
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.network_addresses import public_address

APPRISE_ADMIN_ONLY = "Apprise requires an administrator. Choose Discord, ntfy or a JSON webhook."


class ChannelSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(default="", max_length=2000)
    urls: list[str] = Field(default_factory=list, max_length=20)
    token: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def no_newlines(self):
        if any("\n" in value or "\r" in value for value in [self.url, self.token, *self.urls]):
            raise ValueError("Channel credentials cannot contain newlines")
        return self


def quiet_apprise():
    logger = logging.getLogger("apprise")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False


def validate_config(kind: str, config: ChannelSecrets):
    if kind == "apprise":
        import apprise

        quiet_apprise()
        if not config.urls or any(not apprise.Apprise().add(url) for url in config.urls):
            raise ValueError("Enter valid Apprise notification URLs")
    else:
        parsed = urlsplit(config.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.fragment
        ):
            raise ValueError("Enter an HTTP or HTTPS destination without embedded credentials")
        if kind == "discord" and (
            parsed.scheme != "https"
            or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")
        ):
            raise ValueError("Enter a Discord webhook URL")
        if kind == "ntfy" and (not parsed.path.strip("/") or parsed.query):
            raise ValueError("Enter the ntfy server URL including its topic")


async def check_public_destination(url: str):
    target = httpx.URL(url)
    if not target.host:
        raise ValueError("Invalid destination")
    records = await asyncio.get_running_loop().getaddrinfo(
        target.raw_host.decode("ascii"),
        target.port or (443 if target.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not public_address(address) for address in addresses):
        raise ValueError("Personal destinations must use public addresses")
    return addresses


class DeliveryError(Exception):
    pass


async def send(kind: str, config: dict, payload: dict, *, private_allowed: bool):
    # Apprise plugins own their transports (and may follow redirects or make
    # secondary requests). A DNS preflight cannot secure those connections.
    # Recheck at delivery so old channels and demoted owners cannot bypass this.
    if kind == "apprise" and not private_allowed:
        raise DeliveryError(APPRISE_ADMIN_ONLY)
    settings = ChannelSecrets.model_validate(config)
    validate_config(kind, settings)
    events = payload["events"]
    title = events[0]["title"] if len(events) == 1 else f"Dewarr: {len(events)} updates"
    body = "\n\n".join(f"{event['message']}\n{event['url']}" for event in events)
    if kind == "apprise":
        import apprise

        # Apprise diagnostics can include credentialed URLs. Keep them out of app logs.
        quiet_apprise()
        notifier = apprise.Apprise()
        notifier.add(settings.urls)
        if not await notifier.async_notify(title=title, body=body):
            raise DeliveryError("Apprise reported a delivery failure")
        return
    headers = {"Idempotency-Key": payload["delivery_id"]}
    if settings.token:
        headers["Authorization"] = f"Bearer {settings.token}"
    if kind == "discord":
        embeds = []
        for event in events[:10]:
            embed = {"title": event["title"], "description": event["message"], "url": event["url"]}
            if event.get("cover_url"):
                embed["thumbnail"] = {"url": event["cover_url"]}
            embeds.append(embed)
        data = {"embeds": embeds, "allowed_mentions": {"parse": []}}
        if len(events) > 10:
            data["content"] = f"{len(events)} updates. Open Dewarr to review all matches."
    elif kind == "ntfy":
        # POST to the topic endpoint; body remains UTF-8 and headers stay ASCII.
        headers["Title"] = title.encode("ascii", "replace").decode()
        headers["Click"] = events[0]["url"]
        if len(body.encode()) > 3500:
            body = body.encode()[:3400].decode(errors="ignore") + "\nMore updates in Dewarr."
        data = None
    else:
        data = payload
    target = httpx.URL(settings.url)
    targets, extensions = [target], {}
    if not private_allowed:
        addresses = await check_public_destination(settings.url)
        # Connect to the checked IP, never resolve the untrusted name a second
        # time. Preserve virtual hosting, custom ports and TLS certificate checks.
        targets = [target.copy_with(host=address) for address in addresses[:4]]
        headers["Host"] = httpx.Request("POST", target).headers["Host"]
        extensions["sni_hostname"] = target.raw_host.decode("ascii")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        for index, target in enumerate(targets):
            try:
                response = await client.post(
                    target,
                    headers=headers,
                    extensions=extensions,
                    **({"json": data} if data is not None else {"content": body.encode()}),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Only connection establishment can try another checked IP.
                # Never replay a POST after a write/read failure or HTTP response.
                if index == len(targets) - 1:
                    raise
                continue
            if not 200 <= response.status_code < 300:
                raise DeliveryError(f"Destination returned HTTP {response.status_code}")
            return
