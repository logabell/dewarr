import pytest

from app.config import Settings


@pytest.mark.parametrize("override,enabled", [(None, True), ("false", False), ("true", True)])
def test_download_dispatch_defaults_on_and_respects_environment(monkeypatch, override, enabled):
    monkeypatch.delenv("BOOK_DOWNLOAD_DISPATCH_ENABLED", raising=False)
    if override is not None:
        monkeypatch.setenv("BOOK_DOWNLOAD_DISPATCH_ENABLED", override)

    assert Settings(_env_file=None).download_dispatch_enabled is enabled


@pytest.mark.parametrize("field", ["public_url", "plex_api_origin", "plex_auth_origin"])
@pytest.mark.parametrize(
    "value",
    [
        "https://books.example\n",
        " https://books.example",
        "https://books.example:bad",
        "https://books.example:99999",
        "https://books.example:0",
        "https://books.example:",
        "https://@books.example",
        "https://:password@books.example",
        "https://books.example?",
        "https://books.example#",
        "https://books.example\\evil",
        "https://books.example/path",
        "https://[fe80::1%25eth0]",
        "https://books%2eexample",
        "https://books.example//",
    ],
)
def test_rejects_unusable_configured_origins(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "value,expected",
    [
        ("HTTPS://BOOKS.EXAMPLE:443/", "https://books.example"),
        ("https://bücher.example/", "https://xn--bcher-kva.example"),
        ("http://[0:0:0:0:0:0:0:1]:8000/", "http://[::1]:8000"),
        ("http://nas.local:8788", "http://nas.local:8788"),
    ],
)
def test_configured_origins_are_canonical(value, expected):
    from app.origins import parse_origin

    settings = Settings(_env_file=None, public_url=value)
    assert settings.public_url == expected
    assert settings.public_origin == parse_origin(expected)


@pytest.mark.parametrize("values", [["*"], ["0.0.0.0/0"], ["::/0"], ["bad"], ["10.0.0.2/24"]])
def test_proxy_networks_fail_closed(values):
    with pytest.raises(ValueError):
        Settings(_env_file=None, trusted_proxy_ips=values)


def test_proxy_networks_load_from_json_environment(monkeypatch):
    monkeypatch.setenv("BOOK_TRUSTED_PROXY_IPS", '["192.168.2.1", "2001:db8::/64"]')
    settings = Settings(_env_file=None)
    assert settings.trusted_proxy_ips == ["192.168.2.1/32", "2001:db8::/64"]


def test_rejects_non_browser_ip_literal():
    with pytest.raises(ValueError):
        Settings(_env_file=None, public_url="https://[v1.a]")
