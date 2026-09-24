import pytest

from app.config import get_settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("public_url", "origin", "host"),
    [
        ("http://localhost:8000", "http://192.168.1.20:8085", "192.168.1.20:8085"),
        ("https://BOOKS.example:443", "https://books.example", "dewarr:8000"),
    ],
)
async def test_setup_login_and_settings_through_browser_origin(
    client, monkeypatch, public_url, origin, host
):
    monkeypatch.setattr(get_settings(), "public_url", public_url)
    client.headers.update({"Origin": origin, "Host": host})
    credentials = {"username": "admin", "password": "a long test password"}
    response = await client.post(
        "/api/auth/bootstrap", json={**credentials, "display_name": "Test admin"}
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await client.post("/api/auth/logout")).status_code == 204
    response = await client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    # NOR-60 was reported while saving the Hardcover connection in Settings.
    response = await client.put(
        "/api/metadata/account", json={"token": "test-hardcover-token", "enabled": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is True
    assert "test-hardcover-token" not in response.text

    user = {
        "username": "reader",
        "display_name": "Reader",
        "role": "viewer",
        "password": "a long reader password",
    }
    response = await client.post(
        "/api/auth/users", json=user, headers={"Origin": "https://untrusted.invalid"}
    )
    assert response.status_code == 403
    assert "PUBLIC_URL" in response.json()["detail"]
    response = await client.post(
        "/api/auth/users", json=user, headers={"X-CSRF-Token": "incorrect"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Refresh this page before trying again"
    response = await client.post("/api/auth/users", json=user)
    assert response.status_code == 201, response.text
