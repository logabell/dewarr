import pytest

from app.jobs import pace, tasks


def test_normal_runs_every_scheduled_time():
    assert all(pace.is_due(minute * 60, 60, "normal") for minute in range(100))


@pytest.mark.parametrize(("name", "factor"), [("relaxed", 5), ("light", 20), ("minimal", 50)])
def test_slower_paces_run_one_scheduled_time_in_each_group(name, factor):
    for every in (10, 60, 300):
        runs = [step for step in range(factor * 4) if pace.is_due(step * every, every, name)]
        assert runs == [0, factor, factor * 2, factor * 3]


def test_an_unknown_pace_falls_back_to_normal():
    assert pace.is_due(61, 60, "turbo")


async def test_a_job_that_is_not_due_skips_its_work(monkeypatch):
    calls = []

    async def reconcile_requests():
        calls.append("reconcile")

    monkeypatch.setattr("app.domain.acquisition.reconcile_requests", reconcile_requests)
    pace.remember("light")
    try:
        await tasks.reconcile_acquisition.func(timestamp=300)  # Run 1 of each 20: skipped.
        assert calls == []
        await tasks.reconcile_acquisition.func(timestamp=0)  # Run 0: due.
        assert calls == ["reconcile"]
    finally:
        pace.remember("normal")


async def test_scheduled_work_continues_when_the_setting_cannot_be_read(monkeypatch):
    def broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(pace, "session_factory", broken)
    pace.remember("normal")
    monkeypatch.setitem(pace._cache, "read_at", float("-inf"))
    assert await pace.current() == "normal"
