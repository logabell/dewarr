from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.request_quotas import QuotaExceeded, Rules, check_windows

NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def charge(days, medium="audio", size=0):
    return SimpleNamespace(
        admitted_at=NOW - timedelta(days=days),
        medium=medium,
        size_bytes=size,
        size_at=NOW - timedelta(days=days),
    )


@pytest.mark.parametrize("window,days", [("day", 1), ("week", 7), ("month", 30)])
def test_rolling_boundary_is_exclusive(window, days):
    rules = Rules(windows=[{"window": window, "books": 1}])
    check_windows(rules, [charge(days)], "audio", NOW)
    with pytest.raises(QuotaExceeded) as error:
        check_windows(rules, [charge(days - 0.1)], "audio", NOW)
    assert error.value.retry_at == NOW + timedelta(days=0.1)


def test_separate_media_and_combined_limits():
    separate = Rules(windows=[{"medium": "audio", "books": 1}, {"medium": "ebook", "books": 2}])
    check_windows(separate, [charge(0.5)], "ebook", NOW)
    combined = Rules(windows=[{"medium": "combined", "books": 1}])
    with pytest.raises(QuotaExceeded):
        check_windows(combined, [charge(0.5)], "ebook", NOW)


def test_capacity_time_waits_for_every_window_and_enough_bytes():
    rules = Rules(windows=[{"window": "day", "books": 1}, {"window": "week", "size_bytes": 100}])
    with pytest.raises(QuotaExceeded) as error:
        check_windows(rules, [charge(0.5, size=40), charge(2, size=50)], "audio", NOW, size=70)
    assert error.value.retry_at == NOW + timedelta(days=6.5)


def test_impossible_limits_do_not_invent_a_reset_time():
    with pytest.raises(QuotaExceeded) as error:
        check_windows(Rules(windows=[{"books": 0}]), [], "audio", NOW)
    assert error.value.retry_at is None
    with pytest.raises(ValidationError):
        Rules(windows=[{"books": 1}, {"books": 2}])
