from datetime import UTC, datetime, timedelta

import pytest

from app.domain.capacity import CapacityWait, Limits, combine, download_cost, measure, verify_space

GIB = 1024**3


def snapshot(download="a", library="a", staging="a", *, available=100 * GIB):
    return {
        "roots": {"download": download, "library": library, "staging": staging},
        "filesystems": {
            key: {"available": available, "total": 200 * GIB}
            for key in (download, library, staging)
        },
        "at": datetime.now(UTC).isoformat(),
    }


def frozen(mode="hardlink"):
    return {
        "destination": {"mode": mode},
        "descriptor": {
            "torrent_bytes": GIB,
            "files": [{"path": "book.m4b"}],
        },
    }


def test_hardlink_reserves_download_bytes_once_and_only_future_metadata():
    download, future = download_cost(frozen(), snapshot())
    assert download == {"a": GIB}
    assert future == {"a": 8 * 1024**2}
    assert combine(download, future) == {"a": GIB + 8 * 1024**2}


def test_copy_reserves_download_and_import_on_their_actual_filesystems():
    download, future = download_cost(frozen("copy"), snapshot(library="b", staging="b"))
    assert download == {"a": GIB}
    assert future == {"b": GIB + 8 * 1024**2}
    assert combine(*download_cost(frozen("copy"), snapshot()))["a"] == 2 * GIB + 8 * 1024**2


@pytest.mark.parametrize("observation", [snapshot(library="b", staging="b"), snapshot(staging="b")])
def test_unsupported_mount_boundaries_cannot_gain_a_reservation(observation):
    with pytest.raises(CapacityWait):
        download_cost(frozen(), observation)


def test_seeding_rename_reserves_a_copy_only_across_filesystems():
    same = frozen()
    same["destination"]["seeding_rename"] = True
    download, future = download_cost(same, snapshot())
    assert future == {"a": 8 * 1024**2}
    moved = frozen()
    moved["destination"]["seeding_rename"] = True
    _, future = download_cost(moved, snapshot(library="b", staging="b"))
    assert future == {"b": GIB + 8 * 1024**2}


def test_space_budget_counts_other_reservations_and_free_space_floor():
    limits = Limits(minimum_free_bytes=5 * GIB, minimum_free_percent=5)
    # 5% of 200 GiB is 10 GiB; 20 free less 6 reserved less 4 new is exactly enough.
    verify_space(snapshot(available=20 * GIB), {"a": 4 * GIB}, {"a": 6 * GIB}, limits)
    with pytest.raises(CapacityWait, match="free disk"):
        verify_space(snapshot(available=20 * GIB), {"a": 4 * GIB + 1}, {"a": 6 * GIB}, limits)
    old = snapshot()
    old["at"] = (datetime.now(UTC) - timedelta(seconds=11)).isoformat()
    with pytest.raises(CapacityWait, match="expired"):
        verify_space(old, {"a": 1}, {}, limits)


def test_measure_uses_missing_directory_ancestor_and_refuses_symlinks(tmp_path):
    root = tmp_path.resolve()
    first = measure({"download": (root, "not-created/yet"), "library": (root, "")})
    assert first["roots"]["download"] == first["roots"]["library"]
    assert len(first["filesystems"]) == 1
    (root / "alias").symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        measure({"download": (root, "alias")})


@pytest.mark.parametrize("mode", ["copy", "hardlink"])
def test_selected_pack_reserves_selected_payload_and_only_selected_import_overhead(mode):
    scope = frozen(mode)
    scope["descriptor"] = {
        "torrent_bytes": 100 * GIB,
        "files": [
            {"path": "Pack/Book.m4b", "size_bytes": GIB},
            {"path": "Pack/Other.m4b", "size_bytes": 99 * GIB},
        ],
    }
    scope["selected_paths"] = ["Pack/Book.m4b"]
    download, future = download_cost(scope, snapshot())
    assert download == {"a": GIB}
    assert future == {"a": 8 * 1024**2 + (GIB if mode == "copy" else 0)}
    verify_space(snapshot(available=15 * GIB), combine(download, future), {}, Limits())
    scope["selected_paths"] = ["Pack/Unknown.m4b"]
    with pytest.raises(CapacityWait):
        download_cost(scope, snapshot())
