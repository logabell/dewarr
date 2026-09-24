import asyncio
import errno
import socket
import ssl

import httpx
import pytest

from app.adapters.mam_transport import route_error
from app.domain.mam_diagnostics import probe_egress


async def test_ip_probe_fallback_is_credential_free_and_validates_ip():
    calls = []

    def handler(request):
        calls.append(request)
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, text="not an IP" if len(calls) == 1 else "2001:db8::1\n")

    result = await probe_egress(transport=httpx.MockTransport(handler))
    assert result.ip == "2001:db8::1" and result.error is None
    assert len(calls) == 2


async def test_ip_probe_rejects_large_or_redirected_responses():
    for response in [httpx.Response(200, text="x" * 300), httpx.Response(302)]:
        result = await probe_egress(
            transport=httpx.MockTransport(lambda _, response=response: response)
        )
        assert result.ip is None and result.error


async def test_proxy_probe_never_falls_back_direct_or_leaks_credentials():
    calls = []

    async def proxy(reader, writer):
        calls.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 407 private-error\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await probe_egress(f"http://127.0.0.1:{port}", "private-user", "secret")
    assert len(calls) == 3
    assert all(b"CONNECT " in call and b"Proxy-Authorization: Basic" in call for call in calls)
    assert result.ip is None
    assert "rejected the HTTPS tunnel" in result.error
    assert "private" not in result.error and "secret" not in result.error


async def test_ip_probe_tries_third_service_and_accepts_json():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ip": "203.0.113.10"})

    result = await probe_egress(transport=httpx.MockTransport(handler))
    assert result.ip == "203.0.113.10"
    assert calls == ["icanhazip.com", "api.ipify.org", "ifconfig.me"]


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (socket.gaierror(-2, "private-host"), "same Docker network"),
        (ssl.SSLError("private-cert"), "http://gluetun:8888"),
        (ConnectionRefusedError(errno.ECONNREFUSED, "private-proxy"), "refused the connection"),
    ],
)
def test_network_errors_are_actionable_and_redacted(cause, expected):
    error = httpx.ConnectError("private-user:private-password")
    error.__cause__ = cause
    message = route_error(error, proxy=True)
    assert expected in message
    assert "private" not in message


async def test_ip_probe_reports_dns_failure_without_raw_exception():
    def handler(request):
        raise httpx.ConnectError("private-password") from socket.gaierror(-2, "private-host")

    result = await probe_egress(transport=httpx.MockTransport(handler))
    assert "hostname could not be resolved" in result.error
    assert "private" not in result.error
