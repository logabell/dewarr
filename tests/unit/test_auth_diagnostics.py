import json

from app.config import Settings
from app.diagnostics import auth_configuration


def test_diagnostics_expose_only_selected_configuration():
    settings = Settings(
        _env_file=None,
        public_url="HTTPS://BOOKS.example/",
        cookie_secure=False,
        proxy_token="private-token",
        trusted_proxy_ips=["10.0.0.2"],
        database_url="postgresql+psycopg://private:password@example/db",
    )
    result = auth_configuration(settings)
    assert result["public_url"] == "https://books.example"
    assert result["proxy_identity_mode"] == "token"
    assert len(result["warnings"]) == 2
    assert "private" not in json.dumps(result)
    assert "password" not in json.dumps(result)
    assert set(result) == {
        "installed_version",
        "public_url",
        "cookie_secure",
        "proxy_identity_mode",
        "trusted_proxy_ips",
        "warnings",
    }
