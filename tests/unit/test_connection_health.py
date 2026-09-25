from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.domain.connection_health import connection_status, due, effective_status, proxy_snapshot


def test_success_expires_but_failure_is_retained_until_verified():
    now = datetime.now(UTC)
    old = now - timedelta(minutes=11)
    assert effective_status("connected", None, now) == "stale"
    assert effective_status("connected", old, now) == "stale"
    assert effective_status("connected", now, now) == "connected"
    assert effective_status("authentication", old, now) == "authentication"
    assert connection_status(SimpleNamespace(enabled=False)) == "disabled"
    assert due(SimpleNamespace(status="untested", last_checked_at=now), now)


def test_proxy_health_cannot_survive_configuration_change():
    row = SimpleNamespace(generation=2, proxy_health={"generation": 1, "status": "connected"})
    assert proxy_snapshot(row)[0] == "untested"
