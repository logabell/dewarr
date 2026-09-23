"""Channel adapters. Errors returned to the UI never include response bodies or URLs."""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("Invalid destination")
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise ValueError("Personal destinations must use public addresses")


class DeliveryError(Exception):
    pass


async def send(kind: str, config: dict, payload: dict, *, private_allowed: bool):
    settings = ChannelSecrets.model_validate(config)
    validate_config(kind, settings)
    if not private_allowed and kind != "apprise":
        await check_public_destination(settings.url)
    events = payload["events"]
    title = events[0]["title"] if len(events) == 1 else f"Dewarr: {len(events)} updates"
    body = "\n\n".join(f"{event['message']}\n{event['url']}" for event in events)
    if kind == "apprise":
        import apprise

        # Apprise diagnostics can include credentialed URLs. Keep them out of app logs.
        quiet_apprise()
        notifier = apprise.Apprise()
        notifier.add(settings.urls)
        if not private_allowed:
            for service in notifier:
                # Cloud services often encode API identifiers in the Apprise URL's
                # host position. Validate their actual fixed endpoint instead.
                endpoint = getattr(service, "notify_url", None)
                if getattr(service, "mode", None) == "cloud":
                    endpoint = getattr(service, "cloud_notify_url", endpoint)
                await check_public_destination(endpoint or service.request_url)
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
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        response = (
            await client.post(settings.url, headers=headers, json=data)
            if data is not None
            else await client.post(settings.url, headers=headers, content=body.encode())
        )
        if not 200 <= response.status_code < 300:
            raise DeliveryError(f"Destination returned HTTP {response.status_code}")
