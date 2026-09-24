import pytest

from app.config import Settings


@pytest.mark.parametrize("override,enabled", [(None, True), ("false", False), ("true", True)])
def test_download_dispatch_defaults_on_and_respects_environment(monkeypatch, override, enabled):
    monkeypatch.delenv("BOOK_DOWNLOAD_DISPATCH_ENABLED", raising=False)
    if override is not None:
        monkeypatch.setenv("BOOK_DOWNLOAD_DISPATCH_ENABLED", override)

    assert Settings(_env_file=None).download_dispatch_enabled is enabled
