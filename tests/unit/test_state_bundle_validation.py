import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from app import state_bundle
from app.config import Settings
from app.state_bundle import (
    CONFIG_FIELDS,
    SCHEMA,
    BundleError,
    Manifest,
    file_digest,
    journal_name,
    postgres_tool,
    private_write,
    validate_bundle,
)


def test_supported_bundle_schema_tracks_the_single_migration_head():
    migrations = ScriptDirectory(str(Path(state_bundle.__file__).parent / "db" / "migrations"))
    assert migrations.get_heads() == [SCHEMA]


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "backup"
    root.mkdir(mode=0o700)
    private_write(root / "app_key", Fernet.generate_key())
    private_write(root / "database.dump", b"synthetic archive")
    private_write(
        root / "settings.json",
        json.dumps(Settings().model_dump(mode="json", include=CONFIG_FIELDS)).encode(),
    )
    manifest = Manifest(
        format_version=1,
        backup_id=uuid4(),
        created_at=datetime.now(UTC),
        schema_revision=SCHEMA,
        postgres_major=18,
        files={
            name: file_digest(root / name) for name in ("app_key", "database.dump", "settings.json")
        },
    )
    private_write(root / "manifest.json", manifest.model_dump_json().encode())
    return root


def rewrite(root, change):
    data = json.loads((root / "manifest.json").read_text())
    change(data)
    (root / "manifest.json").write_text(json.dumps(data))


def test_complete_bundle_checks_permissions_and_never_clobbers(bundle):
    assert validate_bundle(bundle).schema_revision == SCHEMA
    assert bundle.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in bundle.iterdir())
    with pytest.raises(FileExistsError):
        private_write(bundle / "app_key", b"replacement")


@pytest.mark.parametrize("entry", ["app_key", "database.dump", "settings.json"])
def test_corrupt_and_missing_files_are_rejected(bundle, entry):
    (bundle / entry).write_bytes(b"corrupt")
    with pytest.raises(BundleError, match="checksum"):
        validate_bundle(bundle)
    (bundle / entry).unlink()
    with pytest.raises(FileNotFoundError):
        validate_bundle(bundle)


@pytest.mark.parametrize(
    "name", ["../escape", "journals/../../escape", "/absolute", "journals/not-a-uuid.json"]
)
def test_path_traversal_rejected_before_open(bundle, name):
    rewrite(bundle, lambda data: data["files"].update({name: data["files"]["app_key"]}))
    with pytest.raises(BundleError, match="unsupported path"):
        validate_bundle(bundle)


def test_symlinks_and_undeclared_entries_rejected(bundle, tmp_path):
    outside = tmp_path / "external"
    outside.write_bytes((bundle / "app_key").read_bytes())
    (bundle / "app_key").unlink()
    (bundle / "app_key").symlink_to(outside)
    with pytest.raises(OSError):
        validate_bundle(bundle)
    (bundle / "app_key").unlink()
    private_write(bundle / "app_key", outside.read_bytes())
    (bundle / "extra").write_bytes(b"extra")
    with pytest.raises(BundleError, match="undeclared"):
        validate_bundle(bundle)


def test_other_versions_are_not_silently_restored(bundle):
    rewrite(bundle, lambda data: data.update(format_version=2))
    with pytest.raises(BundleError, match="version 1"):
        validate_bundle(bundle)


def test_previous_schema_is_not_silently_restored(bundle):
    rewrite(bundle, lambda data: data.update(schema_revision="0066_notifications_follows"))
    with pytest.raises(BundleError, match="same schema revision"):
        validate_bundle(bundle)


def test_verification_does_not_load_unrelated_installation_key_files(bundle, monkeypatch):
    monkeypatch.setenv("BOOK_SECRET_KEY_FILE", "/missing/unrelated-key")
    assert validate_bundle(bundle).schema_revision == SCHEMA


def test_journal_directory_links_and_undeclared_journals_are_rejected(bundle, tmp_path):
    name = str(uuid4()) + ".json"
    directory = tmp_path / "outside-journals"
    directory.mkdir()
    private_write(directory / name, b"{}")
    rewrite(
        bundle,
        lambda data: data["files"].update(
            {"journals/" + name: file_digest(directory / name).model_dump()}
        ),
    )
    (bundle / "journals").symlink_to(directory, target_is_directory=True)
    with pytest.raises(BundleError, match="symbolic links"):
        validate_bundle(bundle)
    (bundle / "journals").unlink()
    directory.rename(bundle / "journals")
    assert validate_bundle(bundle)
    (bundle / "journals" / "extra").write_bytes(b"extra")
    with pytest.raises(BundleError, match="undeclared journals"):
        validate_bundle(bundle)


def test_pg_connection_policies_are_preserved(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(kwargs))
    postgres_tool(
        "pg_dump",
        "dbname=test application_name=backup "
        "target_session_attrs=read-write channel_binding=require",
        [],
    )
    env = calls[0]["env"]
    assert env["PGAPPNAME"] == "backup"
    assert env["PGTARGETSESSIONATTRS"] == "read-write"
    assert env["PGCHANNELBINDING"] == "require"


def test_named_pipe_does_not_block_digest(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(BundleError, match="regular"):
        file_digest(path)


def test_pg_tools_keep_credentials_out_of_argv_and_errors(monkeypatch):
    calls = []

    def failed(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.CalledProcessError(
            1, args, stderr=b"sensitive provider and database details"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    monkeypatch.setenv("PGHOST", "unrelated-host")
    with pytest.raises(BundleError) as error:
        postgres_tool(
            "pg_dump", "postgresql://user:password@localhost:55438/example", ["--format=custom"]
        )
    assert "password" not in str(error.value) and "sensitive" not in str(error.value)
    args, kwargs = calls[0]
    assert "password" not in " ".join(args).replace("--no-password", "")
    assert kwargs["env"]["PGPASSWORD"] == "password"
    assert kwargs["env"]["PGHOST"] == "localhost"
    assert kwargs["env"]["PGDATABASE"] == "example"


def test_journal_names_are_canonical():
    assert journal_name(str(uuid4()) + ".json")
    assert not journal_name("../receipt.json")
