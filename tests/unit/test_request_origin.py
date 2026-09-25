import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import require_origin
from app.config import Settings


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
        "app.api.dependencies.get_settings", lambda: Settings(_env_file=None, public_url=public_url)
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
        lambda: Settings(_env_file=None, public_url="https://books.example"),
    )
    with pytest.raises(HTTPException) as error:
        require_origin(request_for("http://nas.local:8000", origin))
    assert error.value.status_code == 403


def test_rejects_duplicate_origin_headers(monkeypatch):
    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: Settings(_env_file=None, public_url="http://nas.local:8000"),
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
        lambda: Settings(_env_file=None, public_url="https://books.example"),
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


@pytest.mark.parametrize(
    "origin,extra,reason",
    [
        (None, [], "missing"),
        ("https://books.example", [(b"origin", b"https://books.example")], "duplicate"),
        ("https://user:private-password@books.example", [], "malformed"),
        ("https://other.example", [], "mismatch"),
    ],
)
def test_origin_diagnostics_are_bounded_and_do_not_log_raw_headers(
    monkeypatch, caplog, origin, extra, reason
):
    from app.api import dependencies

    monkeypatch.setattr(dependencies, "_origin_log_times", {})
    monkeypatch.setattr(
        dependencies,
        "get_settings",
        lambda: Settings(_env_file=None, public_url="https://books.example"),
    )
    req = request_for("http://dewarr:8000", origin, extra)
    req.state.request_id = "test-request-id"
    for _ in range(2):
        with pytest.raises(HTTPException):
            require_origin(req)
    assert caplog.text.count("Origin rejected") == 1
    assert f"reason={reason}" in caplog.text
    assert "request_id=test-request-id" in caplog.text
    assert "private-password" not in caplog.text


def test_configured_proxy_does_not_authorize_forwarded_origin(monkeypatch):
    monkeypatch.setattr(
        "app.api.dependencies.get_settings",
        lambda: Settings(
            _env_file=None, public_url="https://books.example", trusted_proxy_ips=["127.0.0.1"]
        ),
    )
    with pytest.raises(HTTPException) as error:
        require_origin(
            request_for(
                "http://dewarr:8000",
                "https://evil.example",
                [(b"x-forwarded-host", b"evil.example"), (b"x-forwarded-proto", b"https")],
            )
        )
    assert "Recreate the Docker container" in error.value.detail
