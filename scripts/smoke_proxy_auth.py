"""Exercise the HTTP upstream leg of HTTPS proxy login against an isolated CI image."""

import json
import urllib.error
import urllib.request
from http.cookies import SimpleCookie

ORIGIN = "https://books.example.com"
UPSTREAM = "http://127.0.0.1:8000"
CREDENTIALS = {"username": "smoke-admin", "password": "isolated smoke test password"}


def request(path, *, host, body=None, cookie="", csrf="", origin=ORIGIN):
    headers = {"Host": host, "Origin": origin, "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(
        UPSTREAM + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read()
        return response.status, response.headers, json.loads(data) if data else None


def session(headers):
    cookies = SimpleCookie()
    cookies.load(headers["Set-Cookie"])
    value = cookies["book_session"]
    assert value["secure"] and value["httponly"], "Expected a Secure, HttpOnly session cookie"
    # The probe stands in for the TLS terminator, forwarding the browser cookie over HTTP.
    return f"book_session={value.value}"


def main():
    host = "books.example.com"
    status, headers, data = request(
        "/api/auth/bootstrap", host=host, body={**CREDENTIALS, "display_name": "Smoke admin"}
    )
    assert status == 201, f"Bootstrap failed: {status}"
    cookie = session(headers)
    assert request("/api/auth/me", host=host, cookie=cookie)[0] == 200
    assert (
        request("/api/auth/logout", host=host, body={}, cookie=cookie, csrf=data["csrf_token"])[0]
        == 204
    )
    for host in ("books.example.com", "dewarr:8000"):
        status, headers, data = request("/api/auth/login", host=host, body=CREDENTIALS)
        assert status == 200, f"Proxy login failed: {status}"
        cookie = session(headers)
        assert request("/api/auth/me", host=host, cookie=cookie)[0] == 200
        assert (
            request(
                "/api/auth/logout",
                host=host,
                body={},
                cookie=cookie,
                csrf=data["csrf_token"],
                origin="https://untrusted.example",
            )[0]
            == 403
        )
        assert (
            request("/api/auth/logout", host=host, body={}, cookie=cookie, csrf=data["csrf_token"])[
                0
            ]
            == 204
        )
        assert request("/api/auth/me", host=host, cookie=cookie)[0] == 401
    print("HTTPS-origin authentication over HTTP upstream passed (preserved and rewritten Host)")


if __name__ == "__main__":
    main()
