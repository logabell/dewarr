from types import SimpleNamespace

from app.jobs.worker import worker_pool_config


def test_worker_pools_cover_registered_queues_without_overlap():
    registered = {
        "system",
        "notifications",
        "imports",
        "inspection",
        "inventory",
        "confirmation",
        "sources",
        "acquisition",
        "lists",
        "metadata",
        "downloads",
        "catalog-cache",
        "builtin",
        "recovery",
    }
    queue = SimpleNamespace(
        tasks={str(index): SimpleNamespace(queue=name) for index, name in enumerate(registered)}
    )

    pools = worker_pool_config(queue)
    assignments = {}

    for name, queues, concurrency in pools:
        assert concurrency >= 1
        for queue_name in queues:
            assert queue_name not in assignments, (
                f"{queue_name} assigned to both {assignments.get(queue_name)} and {name}"
            )
            assignments[queue_name] = name

    assert set(assignments) == registered - {"recovery"}


def test_busy_queues_have_independent_capacity():
    queue = SimpleNamespace(
        tasks={
            name: SimpleNamespace(queue=queue_name)
            for name, queue_name in (
                ("search", "sources"),
                ("acquire", "acquisition"),
                ("list", "lists"),
                ("metadata", "metadata"),
                ("download", "downloads"),
            )
        }
    )

    pools = {name: (queues, concurrency) for name, queues, concurrency in worker_pool_config(queue)}

    assert pools["sources"] == ({"sources"}, 2)
    assert pools["acquisition"] == ({"acquisition"}, 2)
    assert pools["lists"] == ({"lists"}, 1)
    assert pools["metadata"] == ({"metadata"}, 1)
    assert pools["downloads"] == ({"downloads"}, 1)
