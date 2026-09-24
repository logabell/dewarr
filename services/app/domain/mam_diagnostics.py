"""Credential-free public egress probes, independent of the rotating MAM session."""

import asyncio
import json
from ipaddress import ip_address

import httpx
from pydantic import BaseModel

from app.adapters.mam_transport import route_error

IP_SERVICES = ("https://icanhazip.com", "https://api.ipify.org", "https://ifconfig.me/ip")


def public_ip(body):
    text = body.decode().strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("ip", "clientIp", "origin"):
            if isinstance(payload.get(key), str):
                try:
                    return str(ip_address(payload[key].strip()))
                except ValueError:
                    continue
    return str(ip_address(text))


class EgressResult(BaseModel):
    ip: str | None = None
    error: str | None = None


async def probe_egress(proxy_url=None, username=None, password=None, *, transport=None):
    proxy = (
        httpx.Proxy(proxy_url, auth=(username or "", password or ""))
        if proxy_url and (username or password)
        else proxy_url
    )
    failures = []
    try:
        async with (
            asyncio.timeout(18),
            httpx.AsyncClient(
                proxy=proxy,
                trust_env=False,
                follow_redirects=False,
                timeout=5,
                transport=transport,
            ) as client,
        ):
            for url in IP_SERVICES:
                try:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 256:
                                raise ValueError("Invalid IP response")
                    return EgressResult(ip=public_ip(body))
                except (httpx.HTTPError, ValueError, UnicodeError) as error:
                    reason = route_error(error, proxy=bool(proxy_url))
                    if reason not in failures:
                        failures.append(reason)
    except (httpx.HTTPError, TimeoutError) as error:
        failures.append(route_error(error, proxy=bool(proxy_url)))
    return EgressResult(error="Public IP lookup failed. " + " ".join(failures))
