"""Safe network diagnostics shared by MAM requests and credential-free IP checks."""

import errno
import socket
import ssl

import httpx


def route_error(error, *, proxy=False):
    # Inspect exception types, never return raw messages (URLs and credentials may appear).
    causes = []
    cause = error
    while cause is not None and all(cause is not item for item in causes):
        causes.append(cause)
        cause = cause.__cause__ or cause.__context__
    if any(isinstance(cause, socket.gaierror) for cause in causes):
        return (
            "The proxy hostname could not be resolved. For a Docker service name such as "
            "gluetun, connect Dewarr and the proxy to the same Docker network."
            if proxy
            else "The destination hostname could not be resolved. Check the server's DNS."
        )
    if any(isinstance(cause, ssl.SSLError) for cause in causes):
        return (
            "TLS negotiation failed. Check certificates and the proxy URL scheme; "
            "Gluetun's HTTP proxy normally uses http://gluetun:8888, even for HTTPS sites."
            if proxy
            else "TLS negotiation failed. Check the server's certificates and clock."
        )
    if any(isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED for cause in causes):
        return (
            "The configured MAM proxy refused the connection. Check that the proxy "
            "is running and its port is reachable from the app server."
            if proxy
            else "The destination refused the connection. Check its URL and port."
        )
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return (
            "The proxy route timed out. Check the proxy's VPN connection and outbound access."
            if proxy
            else "The direct route timed out. Check the server's outbound access."
        )
    if isinstance(error, httpx.ProxyError):
        return (
            "The MAM proxy rejected the HTTPS tunnel. Check proxy authentication "
            "and whether the proxy allows connections to the destination."
        )
    if isinstance(error, httpx.HTTPStatusError):
        return f"The public IP service returned HTTP {error.response.status_code}."
    if isinstance(error, httpx.RemoteProtocolError):
        return "The route closed unexpectedly or returned an invalid HTTP response."
    if isinstance(error, (ValueError, UnicodeError)):
        return "The public IP service did not return a valid IP address."
    return (
        "The configured MAM proxy could not be reached. Check its Docker network, "
        "port and VPN connection."
        if proxy
        else "The direct route could not be reached. Check the server's network."
    )
