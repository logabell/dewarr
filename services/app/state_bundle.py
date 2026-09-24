"""Offline, versioned application-state backups. Media payloads are separate."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings
from app.db.models import Base
from app.recovery import MAINTENANCE_LOCK

SCHEMA = "0065_follows_recovery"
CONFIG_FIELDS = {
    "public_url",
    "cookie_secure",
    "session_hours",
    "web_dist",
    "db_pool_size",
    "hardcover_url",
    "openlibrary_url",
    "import_sources",
    "import_destinations",
    "import_staging_root",
}
MAX_JOURNALS = 10000
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_TOTAL_JOURNAL_BYTES = 256 * 1024 * 1024


class BundleError(RuntimeError):
    pass


class FileDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: int
    backup_id: UUID
    created_at: datetime
    schema_revision: str
    postgres_major: int
    files: dict[str, FileDigest] = Field(max_length=MAX_JOURNALS + 3)


def private_write(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def file_digest(path: Path) -> FileDigest:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise BundleError("Bundle entries must be regular files")
        return FileDigest(
            size=info.st_size, sha256=hashlib.file_digest(stream, "sha256").hexdigest()
        )


def journal_name(name: str) -> bool:
    try:
        return name.endswith(".json") and str(UUID(name[:-5])) == name[:-5]
    except ValueError:
        return False


def validate_bundle(root: Path) -> Manifest:
    if root.is_symlink() or not root.is_dir():
        raise BundleError("Use a regular backup directory")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise BundleError("Invalid backup manifest")
    manifest = Manifest.model_validate_json(manifest_path.read_bytes())
    if manifest.format_version != 1 or manifest.schema_revision != SCHEMA:
        raise BundleError("This command requires a version 1 backup from the same schema revision")
    required = {"database.dump", "app_key", "settings.json"}
    if not required <= manifest.files.keys():
        raise BundleError("The backup is missing required state")
    expected = required | {"manifest.json"}
    journal_bytes = 0
    for name, digest in manifest.files.items():
        if name not in required:
            parts = name.split("/")
            if len(parts) != 2 or parts[0] != "journals" or not journal_name(parts[1]):
                raise BundleError("The manifest contains an unsupported path")
            if digest.size > MAX_JOURNAL_BYTES:
                raise BundleError("A journal exceeds the supported size")
            journal_bytes += digest.size
            expected.add("journals")
            if (root / "journals").is_symlink():
                raise BundleError("Journal directories cannot be symbolic links")
        if file_digest(root / name) != digest:
            raise BundleError("Backup checksum verification failed")
    if journal_bytes > MAX_TOTAL_JOURNAL_BYTES:
        raise BundleError("The backup exceeds the supported journal budget")
    if {p.name for p in root.iterdir()} != expected:
        raise BundleError("The backup contains undeclared entries")
    if "journals" in expected:
        actual = {"journals/" + p.name for p in (root / "journals").iterdir()}
        if actual != {name for name in manifest.files if name.startswith("journals/")}:
            raise BundleError("The backup contains undeclared journals")
    if manifest.files["app_key"].size > 1024 or manifest.files["settings.json"].size > 1024 * 1024:
        raise BundleError("The backup configuration exceeds the supported size")
    Fernet((root / "app_key").read_bytes().strip())
    config = json.loads((root / "settings.json").read_bytes())
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise BundleError("The backup configuration does not match this version")
    Settings(
        _env_file=None,
        secret_key=None,
        secret_key_file=None,
        **config,
    )
    return manifest


@contextmanager
def offline_connection(url: str):
    with psycopg.connect(url, autocommit=True) as connection:
        if not connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (MAINTENANCE_LOCK,)
        ).fetchone()[0]:
            raise BundleError("Stop the API and workers before offline maintenance")
        try:
            yield connection
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MAINTENANCE_LOCK,))


def postgres_tool(tool: str, url: str, args: list[str]) -> None:
    # Never put credentials in argv or reproduce pg_* stderr, which can contain private data.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    options = conninfo_to_dict(url)
    supported = {
        "host",
        "hostaddr",
        "port",
        "user",
        "password",
        "dbname",
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "sslcrldir",
        "options",
        "connect_timeout",
        "target_session_attrs",
        "channel_binding",
        "application_name",
    }
    if set(options) - supported:
        raise BundleError("The PostgreSQL URL contains options unsupported by offline tooling")
    env_names = {
        "dbname": "PGDATABASE",
        "application_name": "PGAPPNAME",
        "target_session_attrs": "PGTARGETSESSIONATTRS",
        "channel_binding": "PGCHANNELBINDING",
    }
    for name, value in options.items():
        env[env_names.get(name, "PG" + name.upper())] = value
    try:
        subprocess.run([tool, "--no-password", *args], env=env, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        raise BundleError(
            f"{tool} failed; check CLI version, connection and database privileges"
        ) from None


def verify_key(connection, key: bytes) -> None:
    cipher = Fernet(key)
    # Every encrypted column is included, including private provider/list/source references.
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name.startswith("encrypted_"):
                with (
                    connection.transaction(),
                    connection.cursor(name="verify_backup_key") as cursor,
                ):
                    cursor.execute(
                        sql.SQL("SELECT {} FROM {} WHERE {} IS NOT NULL").format(
                            sql.Identifier(column.name),
                            sql.Identifier(table.name),
                            sql.Identifier(column.name),
                        )
                    )
                    for (value,) in cursor:
                        cipher.decrypt(value.encode())


def backup(settings: Settings, root: Path) -> Manifest:
    key = settings.encryption_key()
    root = root.absolute()
    # Exclusive creation refuses an existing bundle; manifest is written last as completion marker.
    root.mkdir(mode=0o700)
    with offline_connection(settings.psycopg_url) as connection:
        schema = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if schema != SCHEMA:
            raise BundleError("Migrate to this command's schema before taking a supported backup")
        from app.importing.storage import apply_storage

        mounted = connection.execute(
            "SELECT destinations, staging_root, sources FROM import_storage_settings WHERE id = 1"
        ).fetchone()
        if mounted:
            settings = apply_storage(settings, mounted[0], mounted[1], mounted[2])
        if (
            settings.import_staging_root is None
            and connection.execute("SELECT EXISTS(SELECT 1 FROM import_entries)").fetchone()[0]
        ):
            raise BundleError("Import history exists; configure its journal root before backup")
        if connection.execute(
            "SELECT EXISTS(SELECT 1 FROM book_queue.procrastinate_workers "
            "WHERE last_heartbeat > now() - interval '60 seconds')"
        ).fetchone()[0]:
            raise BundleError("Stop workers and wait for their heartbeat to expire before backup")
        verify_key(connection, key)
        private_write(root / "database.dump", b"")
        postgres_tool(
            "pg_dump",
            settings.psycopg_url,
            ["--format=custom", "--file", str(root / "database.dump")],
        )
        private_write(root / "app_key", key + b"\n")
        config = settings.model_dump(mode="json", include=CONFIG_FIELDS)
        config["web_dist"] = str(settings.web_dist.absolute())
        private_write(root / "settings.json", (json.dumps(config, indent=2) + "\n").encode())
        files = {
            name: file_digest(root / name) for name in ("database.dump", "app_key", "settings.json")
        }
        staging = settings.import_staging_root
        if staging is not None:
            if staging.is_symlink() or not staging.is_dir():
                raise BundleError(
                    "The configured journal root must be an available regular directory"
                )
            total = count = 0
            for path in staging.iterdir():
                if not path.name.endswith(".json"):
                    # Staged media and temporary publication files are separate evidence.
                    continue
                if not journal_name(path.name):
                    raise BundleError("The journal root contains an unrecognized JSON entry")
                digest = file_digest(path)
                total += digest.size
                count += 1
                if (
                    digest.size > MAX_JOURNAL_BYTES
                    or total > MAX_TOTAL_JOURNAL_BYTES
                    or count > MAX_JOURNALS
                ):
                    raise BundleError("The journal root exceeds the supported backup budget")
                (root / "journals").mkdir(mode=0o700, exist_ok=True)
                private_write(root / "journals" / path.name, path.read_bytes())
                files["journals/" + path.name] = file_digest(root / "journals" / path.name)
        manifest = Manifest(
            format_version=1,
            backup_id=uuid4(),
            created_at=datetime.now(UTC),
            schema_revision=schema,
            postgres_major=connection.info.server_version // 10000,
            files=files,
        )
        private_write(root / "manifest.json", (manifest.model_dump_json(indent=2) + "\n").encode())
    return validate_bundle(root)


def restore(
    settings: Settings, bundle: Path, target_name: str, operator: str, output: Path
) -> Manifest:
    manifest = validate_bundle(bundle)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", target_name):
        raise BundleError("Use a new database name with lowercase letters, numbers and underscores")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,100}", operator):
        raise BundleError("Specify an existing active administrator username")
    output = output.absolute()
    output.mkdir(mode=0o700)
    admin_url = make_conninfo(settings.psycopg_url, dbname="postgres")
    target_url = make_conninfo(settings.psycopg_url, dbname=target_name)
    # The owning role needs CREATEDB. Existing databases are never dropped or reused.
    with psycopg.connect(admin_url, autocommit=True) as admin:
        if admin.info.server_version // 10000 != manifest.postgres_major:
            raise BundleError(
                "Restore rehearsal currently requires the same PostgreSQL major version"
            )
        if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_name,)).fetchone():
            raise BundleError("The target database already exists; choose a new name")
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target_name))
        )
        admin.execute(
            sql.SQL("ALTER DATABASE {} SET book_search.restore_pending = 'true'").format(
                sql.Identifier(target_name)
            )
        )
    with offline_connection(target_url) as connection:
        postgres_tool(
            "pg_restore",
            target_url,
            [
                "--dbname",
                target_name,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--single-transaction",
                str(bundle.absolute() / "database.dump"),
            ],
        )
        if connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] != SCHEMA:
            raise BundleError("The restored schema does not match the manifest")
        key = (bundle / "app_key").read_bytes().strip()
        verify_key(connection, key)
        actor = connection.execute(
            "SELECT id FROM users WHERE username = %s AND active AND role = 'admin'",
            (operator.lower(),),
        ).fetchone()
        if not actor:
            raise BundleError(
                "The chosen recovery operator is not an active administrator in this backup"
            )
        with connection.transaction():
            connection.execute("DELETE FROM login_sessions")
            connection.execute("UPDATE restore_checkpoints SET active = false WHERE active")
            checkpoint_id = uuid4()
            connection.execute(
                "INSERT INTO restore_checkpoints(id, operator_id, backup_id, active, snapshot) "
                "VALUES (%s, %s, %s, true, %s)",
                (
                    checkpoint_id,
                    actor[0],
                    manifest.backup_id,
                    Jsonb(
                        {
                            "schema_revision": manifest.schema_revision,
                            "backup_created_at": manifest.created_at.isoformat(),
                            "journal_count": sum(
                                name.startswith("journals/") for name in manifest.files
                            ),
                            "reconciliation": "pending",
                        }
                    ),
                ),
            )
            from app.recovery_queue import SEAL_SQL

            connection.execute(
                SEAL_SQL.replace("%", "%%").replace(":checkpoint_id", "%(checkpoint_id)s"),
                {"checkpoint_id": checkpoint_id},
            )

        private_write(output / "app_key", key + b"\n")
        config = json.loads((bundle / "settings.json").read_bytes())
        config.update(
            {
                "database_url": make_url(settings.database_url.get_secret_value())
                .set(database=target_name)
                .render_as_string(hide_password=False),
                "secret_key_file": str(output / "app_key"),
                "recovery_mode": True,
                "download_dispatch_enabled": False,
            }
        )
        # dotenv parses these quotes as data. Never source this file in a shell.
        lines = []
        for name, value in sorted(config.items()):
            if value is None:
                continue
            encoded = value if isinstance(value, str) else json.dumps(value)
            encoded = encoded.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"BOOK_{name.upper()}='{encoded}'")
        private_write(output / "restore.env", ("\n".join(lines) + "\n").encode())
        private_write(
            output / "manifest.json", (manifest.model_dump_json(indent=2) + "\n").encode()
        )
        for name in manifest.files:
            if name.startswith("journals/"):
                (output / "journals").mkdir(mode=0o700, exist_ok=True)
                private_write(output / name, (bundle / name).read_bytes())
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("backup", help="Stop API/workers first; media files are separate")
    take.add_argument("directory", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("directory", type=Path)
    recover = commands.add_parser(
        "restore", help="Create a new database in operator-only recovery review"
    )
    recover.add_argument("directory", type=Path)
    recover.add_argument("--database", required=True)
    recover.add_argument("--operator", required=True)
    recover.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            manifest = validate_bundle(args.directory)
        elif args.command == "backup":
            manifest = backup(get_settings(), args.directory)
        else:
            manifest = restore(
                get_settings(), args.directory, args.database, args.operator, args.output
            )
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, BundleError)
            else "Check private configuration, file access and database privileges"
        )
        parser.exit(
            1,
            f"State operation failed: {message}. "
            "Incomplete targets are retained; nothing is resumed.\n",
        )
    print(f"{args.command.capitalize()} complete: backup {manifest.backup_id}")
    if args.command == "restore":
        print(
            "Restored state is paused. Start only the API with the generated restore.env "
            "for operator review."
        )


if __name__ == "__main__":
    main()
