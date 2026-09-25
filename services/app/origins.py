"""Shared browser-origin parsing for configuration and request validation."""

import ipaddress
import re
from urllib.parse import urlsplit

Origin = tuple[str, str, int]


def parse_origin(value: str, *, configuration: bool = False) -> Origin | None:
    if len(value) > 2048 or any(
        ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value
    ):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ({"", "/"} if configuration else {""})
            or "?" in value
            or "#" in value
            or parts.netloc.endswith(":")
        ):
            return None
        host = parts.hostname
        if ":" in host:
            if "%" in host:
                return None
            host = str(ipaddress.IPv6Address(host))
        else:
            if "[" in parts.netloc or "]" in parts.netloc:
                return None
            host = host.encode("idna").decode("ascii").lower()
            if len(host) > 253 or not re.fullmatch(r"[a-z0-9_.-]+", host):
                return None
        port = parts.port
        if port == 0:
            return None
        return (
            parts.scheme,
            host,
            port if port is not None else (443 if parts.scheme == "https" else 80),
        )
    except (ValueError, UnicodeError):
        return None


def format_origin(origin: Origin) -> str:
    scheme, host, port = origin
    host = f"[{host}]" if ":" in host else host
    suffix = "" if port == (443 if scheme == "https" else 80) else f":{port}"
    return f"{scheme}://{host}{suffix}"


def configured_origin(value: str) -> str:
    origin = parse_origin(value, configuration=True)
    if origin is None:
        raise ValueError(
            "Use an HTTP(S) origin with a valid host and port, without credentials, "
            "whitespace, a path, query or fragment"
        )
    return format_origin(origin)
