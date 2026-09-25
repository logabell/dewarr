import asyncio
import json
import os
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import ImportStorageRoute, get_settings
from app.db.models import Integration, RestoreCheckpoint, User
from app.db.session import database as database_dependency
from app.main import create_app
from app.recovery import MAINTENANCE_LOCK, runtime_lease
from app.security import encrypt_secrets
from app.state_bundle import BundleError, backup, offline_connection, restore, validate_bundle

pytestmark = pytest.mark.integration


async def test_restore_preserves_evidence_invalidates_sessions_and_fences_effects(
    client,
    admin,
    database,
    tmp_path,
):
    settings = get_settings()
    staging = tmp_path / "staging"
    staging.mkdir()
    journal = staging / (str(uuid4()) + ".json")
    journal.write_text('{"stage_name":"kept-for-review","published":false}')
    media = staging / "original.epub"
    media.write_bytes(b"synthetic media payload remains in place")
    protected = tmp_path / "journals"
    protected.mkdir(mode=0o700)
    extra_journal = protected / ("combine-" + str(uuid4()) + ".json")
    extra_journal.write_text('{"state":"published"}')
    settings = settings.model_copy(
        update={
            "import_staging_root": staging,
            "import_storage_routes": {
                "audio": ImportStorageRoute(
                    staging_root=tmp_path / "offline-nas", journal_root=protected
                ),
                "ebooks": ImportStorageRoute(
                    staging_root=tmp_path / "another-share", journal_root=protected
                ),
            },
        }
    )
    async with database() as db:
        db.add(
            Integration(
                kind="hardcover",
                name="Preserved secret",
                owner_id=UUID(admin["id"]),
                base_url="https://api.hardcover.app",
                encrypted_secrets=encrypt_secrets({"token": "synthetic-test-secret"}),
            )
        )
        actor = await db.get(User, UUID(admin["id"]))
        db.add(
            User(
                username="second",
                display_name="Other administrator",
                password_hash=actor.password_hash,
                role="admin",
            )
        )
        await db.commit()
    operation = await client.post(
        "/api/system/probe", headers={"Idempotency-Key": "restore-evidence"}
    )
    assert operation.status_code == 202
    old_session = client.cookies.get("book_session")
    bundle = tmp_path / "backup"
    manifest = await asyncio.to_thread(backup, settings, bundle)
    assert validate_bundle(bundle) == manifest
    assert (bundle / "journals" / journal.name).read_bytes() == journal.read_bytes()
    assert (bundle / "journals" / extra_journal.name).read_bytes() == extra_journal.read_bytes()
    assert not (bundle / media.name).exists()
    source_inode = media.stat().st_ino
    target_name = "book_restore_" + uuid4().hex[:12] + "_test"
    target_url = make_conninfo(settings.psycopg_url, dbname=target_name)
    output = tmp_path / "restored"
    target_engine = None
    try:
        await asyncio.to_thread(restore, settings, bundle, target_name, "admin", output)
        with psycopg.connect(target_url) as connection:
            assert connection.execute("SELECT count(*) FROM login_sessions").fetchone()[0] == 0
            boundary = connection.execute(
                "SELECT job_id_through,job_count FROM recovery_queue_fences"
            ).fetchone()
            assert boundary and boundary[0] >= 1 and boundary[1] >= 1
            assert (
                connection.execute("SELECT approval_version FROM recovery_queue_fences").fetchone()[
                    0
                ]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM recovery_queue_subjects "
                    "WHERE kind='operation' AND subject_id=%s",
                    (UUID(operation.json()["id"]),),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT backup_id FROM restore_checkpoints WHERE active"
                ).fetchone()[0]
                == manifest.backup_id
            )
            assert (
                connection.execute(
                    "SELECT current_setting('book_search.restore_pending')"
                ).fetchone()[0]
                == "true"
            )
            assert (
                connection.execute(
                    "SELECT id::text FROM operations WHERE idempotency_key = 'restore-evidence'"
                ).fetchone()[0]
                == operation.json()["id"]
            )
            assert (
                connection.execute("SELECT count(*) FROM book_queue.procrastinate_jobs").fetchone()[
                    0
                ]
                == 1
            )
            encrypted = connection.execute("SELECT encrypted_secrets FROM integrations").fetchone()[
                0
            ]
            assert json.loads(
                Fernet((output / "app_key").read_bytes().strip()).decrypt(encrypted.encode())
            ) == {"token": "synthetic-test-secret"}
        assert media.stat().st_ino == source_inode
        assert media.read_bytes() == b"synthetic media payload remains in place"
        assert (output / "journals" / journal.name).read_bytes() == journal.read_bytes()
        assert (output / "journals" / extra_journal.name).read_bytes() == extra_journal.read_bytes()
        assert (await client.get("/api/auth/me")).status_code == 200  # Source untouched.
        async with database() as db:
            assert await db.scalar(select(RestoreCheckpoint.id)) is None
        # Exercise the actual entrypoints with only the generated file, deliberately overriding
        # its environment flag to prove that the persistent fence is independently enforced.
        env = {key: value for key, value in os.environ.items() if not key.startswith("BOOK_")}
        env.update(BOOK_ENV_FILE=str(output / "restore.env"), BOOK_RECOVERY_MODE="false")
        worker = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "python", "-m", "app.jobs.worker"],
            env=env,
            capture_output=True,
            timeout=20,
        )
        assert worker.returncode != 0
        assert b"requires reconciliation" in worker.stderr
        # The checkpoint also fences a completed restore independently of the database GUC.
        with psycopg.connect(target_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("ALTER DATABASE {} RESET book_search.restore_pending").format(
                    sql.Identifier(target_name)
                )
            )
        worker = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "python", "-m", "app.jobs.worker"],
            env=env,
            capture_output=True,
            timeout=20,
        )
        assert worker.returncode != 0 and b"requires reconciliation" in worker.stderr
        startup = await asyncio.to_thread(
            subprocess.run,
            [
                "uv",
                "run",
                "python",
                "-c",
                "import asyncio\n"
                "from app.main import create_app, lifespan\n"
                "from app.config import ImportStorageRoute, get_settings\n"
                "async def check():\n"
                " async with lifespan(create_app()):\n"
                "  assert get_settings().recovery_mode\n"
                "asyncio.run(check())",
            ],
            env=env,
            capture_output=True,
            timeout=20,
        )
        assert startup.returncode == 0, startup.stderr.decode()
        downgrade = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "alembic", "downgrade", "0040_list_comparisons"],
            env=env,
            capture_output=True,
            timeout=20,
        )
        assert downgrade.returncode != 0
        assert b"Restored approval boundaries require a pre-upgrade backup" in downgrade.stderr
        from sqlalchemy.engine import make_url

        target_engine = create_async_engine(
            make_url(settings.database_url.get_secret_value()).set(database=target_name),
            connect_args={"options": "-csearch_path=public,book_queue"},
        )
        sessions = async_sessionmaker(target_engine, expire_on_commit=False)

        async def restored_database():
            async with sessions() as db:
                yield db

        app = create_app()
        app.dependency_overrides[database_dependency] = restored_database
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as restored_client:
            restored_client.cookies.set("book_session", old_session)
            assert (await restored_client.get("/api/auth/me")).status_code == 401
            second = await restored_client.post(
                "/api/auth/login", json={"username": "second", "password": "a long test password"}
            )
            assert second.status_code == 423
            login = await restored_client.post(
                "/api/auth/login", json={"username": "admin", "password": "a long test password"}
            )
            assert login.status_code == 200 and login.json()["recovery"]
            restored_client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            review = await restored_client.get("/api/recovery")
            assert review.status_code == 200
            assert review.json()["backup_id"] == str(manifest.backup_id)
            assert review.json()["paused"] and not review.json()["resume_available"]
            assert (await restored_client.get("/api/lists")).status_code == 423
            assert (
                await restored_client.post(
                    "/api/system/probe", headers={"Idempotency-Key": "restored-no-dispatch"}
                )
            ).status_code == 423
            observation = await restored_client.post(
                "/api/recovery/scans", headers={"Idempotency-Key": "restored-read-only-scan"}
            )
            assert observation.status_code == 202, observation.text
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.jobs.worker",
                "--recovery",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                for _ in range(100):
                    observed = await restored_client.get(
                        "/api/recovery/scans/" + observation.json()["id"]
                    )
                    if observed.json()["scan"]["state"] in {"completed", "held"}:
                        break
                    await asyncio.sleep(0.1)
                assert observed.json()["scan"]["state"] == "completed", observed.text
            finally:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.communicate(), timeout=10)
            with psycopg.connect(target_url) as connection:
                assert (
                    connection.execute(
                        "SELECT status FROM operations WHERE idempotency_key = 'restore-evidence'"
                    ).fetchone()[0]
                    == "queued"
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM restore_checkpoints WHERE active"
                    ).fetchone()[0]
                    == 1
                )
            assert (await restored_client.post("/api/auth/logout")).status_code == 204
        with pytest.raises(BundleError, match="already exists"):
            await asyncio.to_thread(
                restore, settings, bundle, target_name, "admin", tmp_path / "duplicate"
            )
    finally:
        if target_engine:
            await target_engine.dispose()
        with psycopg.connect(
            make_conninfo(settings.psycopg_url, dbname="postgres"), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_name))
            )


async def test_runtime_and_offline_maintenance_exclude_each_other(database):
    settings = get_settings()
    async with runtime_lease():
        with pytest.raises(BundleError, match="Stop the API"):
            with offline_connection(settings.psycopg_url):
                pytest.fail("Exclusive lock unexpectedly acquired")
    with offline_connection(settings.psycopg_url):
        with pytest.raises(RuntimeError, match="Offline maintenance"):
            async with runtime_lease():
                pytest.fail("Runtime started during maintenance")
    async with runtime_lease():
        pass


async def test_backup_rejects_a_valid_but_wrong_key_before_dump(client, admin, database, tmp_path):
    from cryptography.fernet import InvalidToken
    from pydantic import SecretStr

    async with database() as db:
        db.add(
            Integration(
                kind="hardcover",
                name="Key binding",
                owner_id=UUID(admin["id"]),
                base_url="https://api.hardcover.app",
                encrypted_secrets=encrypt_secrets({"token": "private"}),
            )
        )
        await db.commit()
    settings = get_settings().model_copy(
        update={"secret_key": SecretStr(Fernet.generate_key().decode())}
    )
    with pytest.raises(InvalidToken):
        await asyncio.to_thread(backup, settings, tmp_path / "backup")
    assert not (tmp_path / "backup" / "manifest.json").exists()
    assert not (tmp_path / "backup" / "database.dump").exists()


async def test_failed_restore_keeps_database_fenced_before_operator_creation(
    client,
    admin,
    database,
    tmp_path,
    monkeypatch,
):
    settings = get_settings()
    bundle = tmp_path / "backup"
    await asyncio.to_thread(backup, settings, bundle)
    target_name = "book_restore_" + uuid4().hex[:12] + "_test"
    import app.state_bundle as state_bundle

    original = state_bundle.postgres_tool

    def interrupted(tool, url, args):
        original(tool, url, args)
        raise RuntimeError("Simulated interruption after committed pg_restore")

    monkeypatch.setattr(state_bundle, "postgres_tool", interrupted)
    try:
        with pytest.raises(RuntimeError, match="Simulated"):
            await asyncio.to_thread(
                restore, settings, bundle, target_name, "admin", tmp_path / "output"
            )
        with psycopg.connect(make_conninfo(settings.psycopg_url, dbname=target_name)) as connection:
            assert (
                connection.execute(
                    "SELECT current_setting('book_search.restore_pending')"
                ).fetchone()[0]
                == "true"
            )
            assert connection.execute("SELECT count(*) FROM restore_checkpoints").fetchone()[0] == 0
            assert connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (MAINTENANCE_LOCK,)
            ).fetchone()[0]
        from sqlalchemy.engine import make_url

        env = {key: value for key, value in os.environ.items() if not key.startswith("BOOK_")}
        env.update(
            BOOK_ENV_FILE="",
            BOOK_RECOVERY_MODE="false",
            BOOK_DATABASE_URL=make_url(settings.database_url.get_secret_value())
            .set(database=target_name)
            .render_as_string(hide_password=False),
            BOOK_SECRET_KEY=settings.encryption_key().decode(),
        )
        worker = await asyncio.to_thread(
            subprocess.run,
            ["uv", "run", "python", "-m", "app.jobs.worker"],
            env=env,
            capture_output=True,
            timeout=20,
        )
        assert worker.returncode != 0 and b"requires reconciliation" in worker.stderr
        startup = await asyncio.to_thread(
            subprocess.run,
            [
                "uv",
                "run",
                "python",
                "-c",
                "import asyncio\nfrom app.main import create_app, lifespan\n"
                "async def check():\n async with lifespan(create_app()):\n  pass\n"
                "asyncio.run(check())",
            ],
            env=env,
            capture_output=True,
            timeout=20,
        )
        assert startup.returncode != 0 and b"Restore is incomplete" in startup.stderr
    finally:
        with psycopg.connect(
            make_conninfo(settings.psycopg_url, dbname="postgres"), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target_name))
            )
