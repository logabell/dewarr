import ipaddress
import os
from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.origins import configured_origin, parse_origin


class ImportStorageRoute(BaseModel):
    staging_root: Path
    journal_root: Path | None = None  # None preserves the legacy co-located journal protocol.

    @field_validator("staging_root", "journal_root")
    @classmethod
    def absolute_path(cls, path):
        if path is not None and (
            not path.is_absolute() or path.anchor != "/" or path == Path("/") or ".." in path.parts
        ):
            raise ValueError("Use an absolute storage directory below /")
        return path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOOK_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr("postgresql+psycopg://book:book@localhost:5432/book")
    public_url: str = "http://localhost:8000"
    secret_key: SecretStr | None = None
    secret_key_file: Path | None = None
    cookie_secure: bool = True
    session_hours: int = 168
    web_dist: Path = Path("apps/web/dist")
    build_version: str = ""
    release_repository: str = ""
    recovery_mode: bool = False
    download_dispatch_enabled: bool = True
    db_pool_size: int = 5
    hardcover_url: str = "https://api.hardcover.app"
    openlibrary_url: str = "https://openlibrary.org"
    plex_api_origin: str = "https://plex.tv"
    plex_auth_origin: str = "https://app.plex.tv"
    proxy_token: SecretStr | None = None
    trusted_proxy_ips: list[str] = []
    import_sources: dict[str, Path] = {}
    import_destinations: dict[str, Path] = {}
    import_staging_root: Path | None = None
    import_journal_root: Path = Field(
        default_factory=lambda: Path(".local/import-journals").absolute()
    )
    import_storage_routes: dict[str, ImportStorageRoute] = {}

    @field_validator("import_sources", "import_destinations")
    @classmethod
    def validate_import_sources(cls, sources):
        import re

        for key, path in sources.items():
            if not re.fullmatch(r"[a-z0-9_-]{1,60}", key):
                raise ValueError("Download root keys use lowercase letters, numbers, - and _")
            if (
                not path.is_absolute()
                or path.anchor != "/"
                or str(path) == "/"
                or ".." in path.parts
            ):
                raise ValueError("Download roots must be absolute directories below /")
        return sources

    @field_validator("import_staging_root", "import_journal_root")
    @classmethod
    def validate_staging_root(cls, path):
        if path is not None and (
            not path.is_absolute() or path.anchor != "/" or str(path) == "/" or ".." in path.parts
        ):
            raise ValueError("Use an absolute staging directory below /")
        return path

    @field_validator("release_repository")
    @classmethod
    def validate_release_repository(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", value):
            raise ValueError("Use a GitHub owner/repository name")
        return value

    @field_validator("public_url", "plex_api_origin", "plex_auth_origin")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return configured_origin(value)

    @field_validator("trusted_proxy_ips")
    @classmethod
    def validate_trusted_proxy_ips(cls, values: list[str]) -> list[str]:
        networks = [ipaddress.ip_network(value) for value in values]
        if any(network.prefixlen == 0 for network in networks):
            raise ValueError("Trust specific proxy addresses or networks, not every address")
        return [str(network) for network in networks]

    @cached_property
    def public_origin(self):
        return parse_origin(self.public_url)

    @cached_property
    def proxy_networks(self):
        return tuple(ipaddress.ip_network(value) for value in self.trusted_proxy_ips)

    @model_validator(mode="after")
    def load_secrets(self) -> "Settings":
        if self.secret_key_file:
            self.secret_key = SecretStr(self.secret_key_file.read_text().strip())
        if self.secret_key:
            Fernet(self.secret_key.get_secret_value().encode())
        if urlsplit(self.public_url).scheme == "https":
            for origin in (self.plex_api_origin, self.plex_auth_origin):
                if urlsplit(origin).scheme != "https":
                    raise ValueError("Plex sign-in uses HTTPS")
        return self

    def encryption_key(self) -> bytes:
        if not self.secret_key:
            raise RuntimeError("Configure BOOK_SECRET_KEY or BOOK_SECRET_KEY_FILE before startup")
        return self.secret_key.get_secret_value().encode()

    @property
    def psycopg_url(self) -> str:
        return self.database_url.get_secret_value().replace(
            "postgresql+psycopg://", "postgresql://"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("BOOK_ENV_FILE", ".env") or None)
