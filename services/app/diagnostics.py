"""Operator-only authentication configuration diagnostics; never dump settings/secrets."""

import argparse
import json
from importlib.metadata import version

from app.config import Settings, get_settings


def auth_configuration(settings: Settings) -> dict:
    token_mode = bool(settings.proxy_token and settings.proxy_token.get_secret_value())
    mode = "token" if token_mode else "trusted_ips" if settings.proxy_networks else "peer"
    warnings = []
    if settings.public_origin[0] == "https" and not settings.cookie_secure:
        warnings.append("HTTPS public URL has Secure cookies disabled")
    if mode == "peer":
        warnings.append("Clients behind a proxy share that peer's sign-in budget")
    if token_mode and settings.proxy_networks:
        warnings.append("Proxy token mode takes precedence over trusted proxy addresses")
    return {
        "installed_version": settings.build_version or f"{version('dewarr')}-dev",
        "public_url": settings.public_url,
        "cookie_secure": settings.cookie_secure,
        "proxy_identity_mode": mode,
        "trusted_proxy_ips": settings.trusted_proxy_ips,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="store_true", help="Apply Docker environment aliases")
    args = parser.parse_args()
    try:
        if args.container:
            from app.container import configure_environment

            configure_environment()
        print(json.dumps(auth_configuration(get_settings()), indent=2))
    except (ValueError, OSError, RuntimeError):
        # Pydantic errors may include raw inputs; never echo the exception or full settings.
        print("Unable to load configuration. Check URL, proxy addresses and saved key settings.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
