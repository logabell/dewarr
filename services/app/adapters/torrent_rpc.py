"""Bounded JSON transports shared by the additional torrent clients."""

import asyncio
import json

import httpx

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.qbittorrent import MAX_RESPONSE, QbitState, absolute_path, hash_value


class RpcTransport:
    name = "Torrent client"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def post(self, payload, *, mutating=False, handshake=False):
        try:
            async with (
                asyncio.timeout(45),
                self.client.stream("POST", self.rpc_url, json=payload) as response,
            ):
                if handshake and response.status_code == 409:
                    token = response.headers.get("X-Transmission-Session-Id")
                    if not token or len(token) > 512:
                        raise AdapterError(
                            FailureKind.PARSER, "Invalid Transmission session handshake."
                        )
                    self.client.headers["X-Transmission-Session-Id"] = token
                    return None
                if response.status_code in {401, 403}:
                    raise AdapterError(
                        FailureKind.AUTHENTICATION, f"{self.name} rejected authentication."
                    )
                if response.status_code != 200:
                    raise AdapterError(
                        FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE,
                        f"{self.name} did not confirm the RPC operation.",
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise ValueError("Oversized response")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("Invalid response")
                return result
        except (ValueError, TypeError) as exc:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.PARSER,
                f"{self.name} returned invalid RPC evidence.",
            ) from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE,
                f"{self.name} could not be reached; reconcile uncertain submissions.",
            ) from exc


def verify_untagged(states: list[QbitState], *, hashes, save_path, category, **kwargs):
    """Deluge has one category label, not an independent attempt tag.

    Its frozen, unique selection folder is the second identity factor. A transfer
    in a generic folder is never adopted, even if its hash happens to match.
    """
    path = absolute_path(save_path)
    import re

    if not re.search(r"/dewarr-[0-9a-f]{32}$", path):
        raise AdapterError(
            FailureKind.UNCERTAIN, "Deluge requires a unique recorded download folder."
        )
    if not states:
        return None
    if len(states) != 1:
        raise AdapterError(FailureKind.UNCERTAIN, "Multiple transfers match this attempt.")
    state = states[0]
    if (
        not {hash_value(h) for h in hashes}.issubset(state.identities)
        or state.save_path != path
        or state.category != category
        or state.auto_managed
    ):
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "Transfer identity or recorded destination differs; it was not adopted.",
        )
    return state.model_copy(update={"association_verified": True})
