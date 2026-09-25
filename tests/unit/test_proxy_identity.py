import pytest
from starlette.requests import Request

from app.api.dependencies import client_host
from app.config import Settings


def request(peer="10.0.0.2", headers=()):
    return Request({"type": "http", "client": (peer, 1234), "headers": list(headers)})


@pytest.mark.parametrize(
    "peer,headers,expected",
    [
        ("10.0.0.2", [(b"x-forwarded-for", b"198.51.100.1")], "198.51.100.1"),
        ("10.0.0.3", [(b"x-forwarded-for", b"198.51.100.1")], "10.0.0.3"),
        ("10.0.0.2", [(b"x-forwarded-for", b"1.1.1.1, 198.51.100.1")], "198.51.100.1"),
        ("10.0.0.2", [(b"x-forwarded-for", b"1.1.1.1, 198.51.100.1, 10.0.0.4")], "198.51.100.1"),
        (
            "10.0.0.2",
            [(b"x-forwarded-for", b"1.1.1.1"), (b"x-forwarded-for", b"198.51.100.1")],
            "198.51.100.1",
        ),
        ("10.0.0.2", [(b"x-real-ip", b"198.51.100.1")], "198.51.100.1"),
        ("10.0.0.2", [(b"x-real-ip", b"198.51.100.1"), (b"x-real-ip", b"1.1.1.1")], "10.0.0.2"),
        ("10.0.0.2", [(b"x-forwarded-for", b"198.51.100.1, unknown")], "10.0.0.2"),
        ("10.0.0.2", [(b"x-forwarded-for", b"unknown, 198.51.100.1")], "10.0.0.2"),
        ("10.0.0.2", [(b"x-forwarded-for", b"198.51.100.1,")], "10.0.0.2"),
        ("10.0.0.2", [], "10.0.0.2"),
        ("::1", [(b"x-forwarded-for", b"2001:db8::5")], "2001:db8::5"),
    ],
)
def test_only_explicit_proxies_can_supply_client_addresses(monkeypatch, peer, headers, expected):
    settings = Settings(
        _env_file=None, proxy_token=None, trusted_proxy_ips=["10.0.0.2", "10.0.0.4", "::1"]
    )
    monkeypatch.setattr("app.api.dependencies.get_settings", lambda: settings)
    assert client_host(request(peer, headers)) == expected


def test_token_mode_cannot_fall_back_to_ip_trust(monkeypatch):
    settings = Settings(_env_file=None, proxy_token="secret", trusted_proxy_ips=["10.0.0.2"])
    monkeypatch.setattr("app.api.dependencies.get_settings", lambda: settings)
    headers = [(b"x-forwarded-for", b"198.51.100.1")]
    assert client_host(request(headers=headers)) == "10.0.0.2"
    assert (
        client_host(request(headers=[*headers, (b"x-dewarr-proxy-token", b"secret")]))
        == "198.51.100.1"
    )
    assert (
        client_host(request(headers=[*headers, (b"x-dewarr-proxy-token", b"secr\xfft")]))
        == "10.0.0.2"
    )


def test_proxy_trust_disabled_by_default(monkeypatch):
    settings = Settings(_env_file=None, proxy_token=None, trusted_proxy_ips=[])
    monkeypatch.setattr("app.api.dependencies.get_settings", lambda: settings)
    assert client_host(request(headers=[(b"x-forwarded-for", b"198.51.100.1")])) == "10.0.0.2"
