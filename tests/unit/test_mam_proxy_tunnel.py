"""Exercise real HTTPX CONNECT + TLS, rather than bypassing proxy mounts with a mock."""

import asyncio
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.adapters.mam import MAMClient
from app.domain.mam_diagnostics import probe_egress


@pytest.mark.parametrize("authenticated_proxy", [False, True])
async def test_http_proxy_tunnels_https_for_cookie_and_credential_free_ip(
    tmp_path, monkeypatch, authenticated_proxy
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MAM proxy test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("mam.invalid"), x509.DNSName("icanhazip.com")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_path, key_path)
    client_tls = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(
        httpx._transports.default, "create_ssl_context", lambda **kwargs: client_tls
    )
    # Explicit routing must still work in the presence of hostile environment defaults.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    tunnels, requests, errors = [], [], []
    finished = asyncio.Event()

    async def proxy(reader, writer):
        try:
            tunnels.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(server_tls)
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            body = (
                b'{"uid": 1, "username": "fixture"}'
                if b"/jsonLoad.php" in request
                else b"203.0.113.42\n"
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception as error:
            errors.append(error)
        finally:
            writer.close()
            await writer.wait_closed()
            if len(requests) == 2:
                finished.set()

    username = "proxy-user" if authenticated_proxy else None
    password = "proxy-password" if authenticated_proxy else None
    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
        proxy_url = f"http://localhost:{server.sockets[0].getsockname()[1]}"
        async with MAMClient(
            "https://mam.invalid",
            "fixture-session",
            proxy_url=proxy_url,
            proxy_username=username,
            proxy_password=password,
        ) as client:
            await client.test()
        result = await probe_egress(proxy_url, username, password)
        await asyncio.wait_for(finished.wait(), 5)
    assert result.ip == "203.0.113.42" and not result.error
    assert not errors
    assert len(tunnels) == 2
    assert tunnels[0].startswith(b"CONNECT mam.invalid:443 HTTP/1.1")
    assert tunnels[1].startswith(b"CONNECT icanhazip.com:443 HTTP/1.1")
    assert all(
        (b"Proxy-Authorization: Basic" in tunnel) == authenticated_proxy for tunnel in tunnels
    )
    assert all(b"fixture-session" not in tunnel for tunnel in tunnels)
    assert b"Cookie: mam_id=fixture-session" in requests[0]
    assert b"cookie:" not in requests[1].lower()
    assert all(b"proxy-authorization:" not in request.lower() for request in requests)
