"""A client-added wrapper folder (qBittorrent "Subfolder" layout) is not a different transfer."""

from types import SimpleNamespace

import pytest

from app.domain.download_attempts import (
    inspection_path,
    layout_wrapper,
    observed_wrapper,
    transfer_stage,
)

NAME = "Book - Author.m4b"
SIZE = 492_070_294


def selection(files=None, *, relative="", name=None):
    files = files or {NAME: SIZE}
    return SimpleNamespace(
        frozen={
            "descriptor": {
                "name": name or NAME,
                "torrent_bytes": sum(files.values()),
                "files": [{"path": p, "size_bytes": s} for p, s in files.items()],
            },
            "mapping": {"relative_path": relative},
        }
    )


def client_state(files, *, total=None, completed=True):
    return SimpleNamespace(
        state="stalledUP",
        completed=completed,
        reported_complete=completed,
        total_bytes=sum(files.values()) if total is None else total,
        files=[
            SimpleNamespace(relative_path=p, size_bytes=s, priority=1, complete=completed)
            for p, s in files.items()
        ],
    )


@pytest.mark.parametrize(
    "expected,actual,wrapper",
    [
        ({NAME: 5}, {f"Book - Author/{NAME}": 5}, "Book - Author"),
        ({"a.mp3": 1, "b.mp3": 2}, {"W/a.mp3": 1, "W/b.mp3": 2}, "W"),
        ({"sub/x.mp3": 1}, {"W/sub/x.mp3": 1}, "W"),
        ({"Book/a.mp3": 1}, {"Book/Book/a.mp3": 1}, "Book"),
        # identical or empty: nothing was wrapped
        ({NAME: 5}, {NAME: 5}, None),
        ({}, {}, None),
        ({NAME: 5}, {}, None),
        ({}, {"W/x": 1}, None),
        # not the same files
        ({NAME: 5}, {f"W/{NAME}": 6}, None),
        ({NAME: 5}, {"W/Other.m4b": 5}, None),
        ({NAME: 5}, {f"W/{NAME}": 5, "W/extra.nfo": 1}, None),
        ({"a": 1, "b": 2}, {"W/a": 1}, None),
        # more than one new leading folder, or a file left outside the wrapper
        ({"a": 1, "b": 2}, {"W/a": 1, "V/b": 2}, None),
        ({"a": 1, "b": 2}, {"W/a": 1, "b": 2}, None),
        ({NAME: 5}, {f"W/V/{NAME}": 5}, None),
        # wrapper names that could point elsewhere are never trusted
        ({"a": 1}, {"../a": 1}, None),
        ({"a": 1}, {"./a": 1}, None),
        ({"a": 1}, {"/a": 1}, None),
        ({"a": 1}, {"W\\X/a": 1}, None),
    ],
)
def test_layout_wrapper_requires_exactly_one_added_leading_folder(expected, actual, wrapper):
    assert layout_wrapper(expected, actual) == wrapper


def test_wrapped_single_file_download_is_the_inspected_torrent():
    chosen = selection()
    wrapped = {f"Book - Author/{NAME}": SIZE}
    stage, message = transfer_stage(chosen, client_state(wrapped))
    assert stage == "complete", message
    assert transfer_stage(chosen, client_state({NAME: SIZE}))[0] == "complete"


@pytest.mark.parametrize(
    "files,total",
    [
        ({f"Book - Author/{NAME}": SIZE - 1}, None),  # truncated or different file
        ({"Book - Author/Other.m4b": SIZE}, None),  # renamed file
        ({f"Book - Author/{NAME}": SIZE, "Book - Author/x.nfo": 3}, None),  # extra file
        ({f"Book - Author/{NAME}": SIZE}, SIZE + 1),  # client reports another total
    ],
)
def test_wrapper_does_not_hide_real_differences(files, total):
    stage, message = transfer_stage(selection(), client_state(files, total=total))
    assert stage == "held" and "differ" in message


def test_wrapped_transfer_still_waits_for_completion():
    wrapped = client_state({f"Book - Author/{NAME}": SIZE}, completed=False)
    assert transfer_stage(selection(), wrapped)[0] == "downloading"


@pytest.mark.parametrize(
    "relative,wrapper,expected",
    [
        ("", None, NAME),
        ("", "Book - Author", f"Book - Author/{NAME}"),
        ("dl/books", "Book - Author", f"dl/books/Book - Author/{NAME}"),
        (".", None, NAME),
        ("dl", None, f"dl/{NAME}"),
    ],
)
def test_inspection_path_includes_the_observed_wrapper(relative, wrapper, expected):
    assert inspection_path(selection(relative=relative), wrapper) == expected


def test_inspection_path_for_a_rooted_torrent_keeps_its_root_inside_a_wrapper():
    chosen = selection({"Pack/a.mp3": 1, "Pack/b.mp3": 2}, name="Pack")
    assert inspection_path(chosen) == "Pack"
    assert inspection_path(chosen, "W") == "W/Pack"


def test_observed_wrapper_reads_the_last_client_observation():
    chosen = selection()
    wrapped = {"files": [{"relative_path": f"Book - Author/{NAME}", "size_bytes": SIZE}]}
    assert observed_wrapper(chosen, wrapped) == "Book - Author"
    assert (
        observed_wrapper(chosen, {"files": [{"relative_path": NAME, "size_bytes": SIZE}]}) is None
    )
    assert observed_wrapper(chosen, {"files": []}) is None
    assert observed_wrapper(chosen, {}) is None
    assert observed_wrapper(chosen, None) is None
