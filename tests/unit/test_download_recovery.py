from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain.download_recovery import RecoveryConfiguration, RecoveryPolicy, observe, policy_for
from app.domain.release_blocklist import release_keys


def state(**changes):
    return SimpleNamespace(
        association_verified=True,
        completed=False,
        state="downloading",
        progress=0.2,
        seeders=0,
        **changes,
    )


def test_stall_clock_is_durable_and_uncertainty_resets_it():
    now = datetime.now(UTC)
    policy = RecoveryPolicy(stall_hours=24)
    health, reason = observe(None, state(), policy, now)
    assert reason is None
    assert observe(health, state(), policy, now + timedelta(hours=23))[1] is None
    assert "No seeders" in observe(health, state(), policy, now + timedelta(hours=24))[1]
    assert observe(health, None, policy, now + timedelta(hours=25)) == ({}, None)


@pytest.mark.parametrize(
    "status",
    [
        "pausedDL",
        "stoppedDL",
        "queuedDL",
        "checkingDL",
        "0",
        "1",
        "2",
        "3",
        "5",
        "Paused",
        "Queued",
        "Checking",
        "Allocating",
        "Moving",
    ],
)
def test_user_paused_or_queued_transfer_is_not_stalled(status):
    now = datetime.now(UTC)
    item = state()
    health, _ = observe(None, item, RecoveryPolicy(), now)
    item.state = status
    assert observe(health, item, RecoveryPolicy(), now + timedelta(days=2)) == ({}, None)


def test_progress_and_seed_recovery_reset_independent_clocks():
    now = datetime.now(UTC)
    item = state()
    health, _ = observe(None, item, RecoveryPolicy(), now)
    item.seeders, item.progress = 5, 0.7
    health, reason = observe(health, item, RecoveryPolicy(), now + timedelta(hours=23))
    assert reason is None and "zero_seeders_since" not in health
    assert observe(health, item, RecoveryPolicy(), now + timedelta(hours=25))[1] is None
    assert "No progress" in observe(health, item, RecoveryPolicy(), now + timedelta(hours=47))[1]


@pytest.mark.parametrize("status", ["failed", "missingFiles", "error"])
def test_permanent_errors_wait_for_configured_grace(status):
    now = datetime.now(UTC)
    item = state()
    item.state = status
    health, reason = observe(None, item, RecoveryPolicy(), now)
    assert reason is None
    assert (
        observe(health, item, RecoveryPolicy(), now + timedelta(minutes=5))[1]
        == "Downloader reported " + status
    )
    item.completed = True
    assert observe(health, item, RecoveryPolicy(), now + timedelta(days=2)) == ({}, None)


def test_mam_stalls_default_off_and_source_override_wins():
    config = RecoveryConfiguration()
    selection = SimpleNamespace(frozen={"release": {"source": "mam"}})
    assert policy_for(config, selection).stall_hours is None
    config.sources["mam"] = RecoveryPolicy(stall_hours=168)
    assert policy_for(config, selection).stall_hours == 168
    assert config.attempt_cap == 3


def test_indexer_scoped_release_keys_and_cross_source_hashes():
    source, keys = release_keys(
        {"source": "prowlarr", "indexer_id": "1", "source_id": "abc"}, {"infohash_v1": "a" * 40}
    )
    _, other = release_keys({"source": "prowlarr", "indexer_id": "2", "source_id": "abc"})
    assert source == "prowlarr:1" and not set(keys).intersection(other)
    _, same = release_keys({"source": "mam", "source_id": "xyz"}, {"infohash_v1": "A" * 40})
    assert set(keys).intersection(same) == {"infohash_v1:" + "a" * 40}
