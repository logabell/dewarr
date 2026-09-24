from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import require_origin


def request_for(url: str, origin: str | None, extra_headers=()) -> Request:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    headers = [(b"host", parts.netloc.encode()), *extra_headers]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": parts.scheme,
            "path": "/api/auth/login",
            "query_string": b"",
            "headers": headers,
            "server": (parts.hostname, parts.port or 80),
        }
    )


@pytest.mark.parametrize(
    ("public_url", "request_url", "origin"),
    [
        ("http://localhost:8000", "http://192.168.1.20:8085", "http://192.168.1.20:8085"),
        ("http://localhost:8000", "http://nas.local:8000", "http://nas.local:8000"),
        ("http://localhost:8000", "http://[::1]:8000", "http://[::1]:8000"),
        ("http://localhost:8000", "https://books.example", "https://books.example"),
        ("https://books.example", "http://dewarr:8000", "https://books.example"),
        ("https://BOOKS.example:443", "http://dewarr:8000", "https://books.example"),
        ("http://localhost:8000", "http://NAS.local:80", "http://nas.local"),
    ],
)
def test_accepts_same_origin_or_configured_public_origin(
    monkeypatch, public_url, request_url, origin
):
    monkeypatch.setattr(
        "app.api.dependencies.get_settings", lambda: SimpleNamespace(public_url=public_url)
    )
    require_origin(request_for(request_url, origin))


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "null",
        "https://untrusted.invalid",
        "http://nas.local:8001",
        "https://nas.local:8000",
        "http://nas.local:8000.evil.example",
        "http://nas.local:8000@evil.example",
        "http://user@nas.local:8000",
        "http://nas.local:8000/path",
        "http://nas.local:8000?query=1",
        "http://nas.local:8000#fragment",
        "http://nas.local:bad",
        "http://nas.local:99999",
        "http://[::1",
        "http://nas.local:8000 https://evil.example",
        "http://nas.local:8000\t",
        "http://nas.local:8000/",
        "http://nas.local:8000?",
        "http://nas.local:8000#",
        "http://nas.local:8000\\evil",
    ],
)
def test_rejects_cross_site_missing_and_malformed_origins(monkeypatch, origin):
    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: SimpleNamespace(public_url="https://books.example"),
    )
    with pytest.raises(HTTPException) as error:
        require_origin(request_for("http://nas.local:8000", origin))
    assert error.value.status_code == 403


def test_rejects_duplicate_origin_headers(monkeypatch):
    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: SimpleNamespace(public_url="http://nas.local:8000"),
    )
    with pytest.raises(HTTPException) as error:
        require_origin(
            request_for(
                "http://nas.local:8000",
                "http://nas.local:8000",
                [(b"origin", b"http://nas.local:8000")],
            )
        )
    assert error.value.status_code == 403


def test_untrusted_forwarded_headers_cannot_allow_an_origin(monkeypatch):
    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: SimpleNamespace(public_url="https://books.example"),
    )
    with pytest.raises(HTTPException) as error:
        require_origin(
            request_for(
                "http://nas.local:8000",
                "https://evil.example",
                [
                    (b"x-forwarded-host", b"evil.example"),
                    (b"x-forwarded-proto", b"https"),
                    (b"forwarded", b"host=evil.example;proto=https"),
                    (b"sec-fetch-site", b"same-origin"),
                ],
            )
        )
    assert error.value.status_code == 403
