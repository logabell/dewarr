import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event

from app.db.models import BookList, ListObservation, ListSubscription, Work
from tests.integration.test_discovery import login_member

pytestmark = pytest.mark.integration


async def test_observation_page_batches_identity_and_redacts_hidden_roots(
    client, admin, database, tmp_path
):
    reader_id = await login_member(client)
    now = datetime.now(UTC)
    roots, origins = [], []
    async with database() as db, db.begin():
        item = BookList(owner_id=reader_id, name="Large observed shelf")
        db.add(item)
        await db.flush()
        subscription = ListSubscription(list_id=item.id, encrypted_config="unused")
        db.add(subscription)
        await db.flush()
        for n in range(101):
            root = Work(
                title=f"Canonical {n}",
                description="x" * 50_000,
                catalog_public=False,
                catalog_owner_id=UUID(admin["id"]) if n == 3 else reader_id,
            )
            db.add(root)
            await db.flush()
            origin = Work(title=f"Original {n}", redirect_to=root.id, description="y" * 50_000)
            db.add(origin)
            await db.flush()
            db.add(
                ListObservation(
                    subscription_id=subscription.id,
                    external_id=str(n),
                    work_id=origin.id,
                    snapshot={
                        "title": f"Observed {n}",
                        "authors": ["Author"],
                        "identity_changed": n == 5,
                    },
                    excluded=n == 5,
                    present=n != 6,
                    last_seen_at=now,
                    created_at=now + timedelta(seconds=n),
                )
            )
            roots.append(root.id)
            origins.append(origin.id)
    calls, loaded = [], []
    engine = database.kw["bind"].sync_engine

    def count(*args):
        calls.append(1)

    def capture(row, context):
        loaded.append({k: v for k, v in vars(row).items() if not k.startswith("_")})

    event.listen(engine, "before_cursor_execute", count)
    event.listen(Work, "load", capture)
    started = time.perf_counter()
    try:
        response = await client.get(
            f"/api/lists/{item.id}/subscription/observations", params={"limit": 100}
        )
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", count)
        event.remove(Work, "load", capture)
    metrics = {
        "queries": len(calls),
        "work_bytes": len(json.dumps(loaded, default=str)),
        "elapsed_ms": elapsed * 1000,
    }
    (tmp_path / "observation-metrics.json").write_text(json.dumps(metrics, indent=2))
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 101 and len(page["items"]) == 100
    for n, row in enumerate(page["items"]):
        assert row["title"] == f"Observed {n}"
        assert row["work_id"] == (None if n == 3 else str(roots[n]))
        assert row["catalog_title"] == (None if n == 3 else f"Canonical {n}")
    assert page["items"][5]["excluded"] and page["items"][5]["identity_changed"]
    assert not page["items"][6]["present"]
    last = (
        await client.get(f"/api/lists/{item.id}/subscription/observations", params={"offset": 100})
    ).json()
    assert [row["external_id"] for row in last["items"]] == ["100"]
    empty = (
        await client.get(f"/api/lists/{item.id}/subscription/observations", params={"offset": 101})
    ).json()
    assert empty["items"] == [] and empty["total"] == 101
    async with database() as db, db.begin():
        (await db.get(Work, roots[0])).redirect_to = origins[0]
    broken = await client.get(
        f"/api/lists/{item.id}/subscription/observations", params={"limit": 1}
    )
    assert broken.status_code == 409 and "identity" in broken.text
    assert metrics["queries"] < 15, "Observation page resolves each book separately"
    assert metrics["work_bytes"] < 10000, "Observation page hydrates full book metadata"
