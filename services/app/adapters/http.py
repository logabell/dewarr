import asyncio
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from app.adapters.contracts import AdapterError, FailureKind, ResponseTooLarge


def configured_url(value: str) -> str:
    """Administrator-configured endpoints may be local; credentials belong in secrets."""
    value = value.strip()
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or "\\" in value
        or any(ord(character) < 33 for character in value)
        or any(part in {".", ".."} for part in unquote(parts.path).replace("\\", "/").split("/"))
    ):
        raise ValueError("Use an HTTP(S) server URL without credentials, query or fragment")
    try:
        _ = parts.port
    except ValueError as error:
        raise ValueError("Enter a valid server port") from error
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class JsonEndpoint:
    def __init__(self, base_url: str, token: str | None = None, *, transport=None):
        self.response_headers = {}
        self.client = httpx.AsyncClient(
            base_url=configured_url(base_url) + "/",
            headers={
                **({"Authorization": f"Bearer {token}"} if token else {}),
                "Accept": "application/json",
                "User-Agent": "BookSearch/0.1 (self-hosted catalog client)",
            },
            timeout=httpx.Timeout(30, connect=10),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json=None,
        empty=False,
        allow_list=False,
        timeout_seconds=45,
        max_bytes=16 * 1024 * 1024,
        response_label="Server response",
    ):
        if callback := getattr(self, "before_request", None):
            await callback()
        self.response_headers = {}
        try:
            async with (
                asyncio.timeout(timeout_seconds),
                self.client.stream(
                    method,
                    path,
                    params=params,
                    json=json,
                ) as response,
            ):
                self.response_headers = dict(response.headers)
                status = response.status_code
                kinds = {
                    401: FailureKind.AUTHENTICATION,
                    403: FailureKind.PERMISSION,
                    404: FailureKind.NOT_FOUND,
                    429: FailureKind.RATE_LIMIT,
                }
                if status in kinds:
                    raise AdapterError(
                        kinds[status],
                        {
                            401: "The server rejected the API token. Update the connection token.",
                            403: "This token cannot access the requested library or operation.",
                            404: "The requested library item or API endpoint was not found.",
                            429: "The server is limiting requests. Sync will wait before retrying.",
                        }[status],
                    )
                if 300 <= status < 400:
                    raise AdapterError(
                        FailureKind.ROUTE, "The server redirected the request. Set its final URL."
                    )
                if not 200 <= status < 300:
                    raise AdapterError(
                        FailureKind.UNAVAILABLE, "The server could not complete the request."
                    )
                if empty:
                    return None
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    received = len(content) + len(chunk)
                    if received > max_bytes:
                        raise ResponseTooLarge(max_bytes, received, response_label)
                    content.extend(chunk)
                import json as json_module

                try:
                    value = json_module.loads(content)
                except (ValueError, UnicodeError) as error:
                    raise AdapterError(
                        FailureKind.PARSER, "The server returned an unreadable API response."
                    ) from error
                if allow_list and isinstance(value, list):
                    return value
                if not isinstance(value, dict):
                    raise AdapterError(
                        FailureKind.PARSER, "The server returned an unexpected API response."
                    )
                return value
        except (httpx.TimeoutException, TimeoutError) as error:
            raise AdapterError(
                FailureKind.TIMEOUT, "The server did not respond in time."
            ) from error
        except httpx.HTTPError as error:
            raise AdapterError(
                FailureKind.ROUTE, "The server could not be reached. Check its URL and network."
            ) from error
