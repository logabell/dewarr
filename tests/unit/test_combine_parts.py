import os
from uuid import UUID, uuid4

import pytest

from app.importing import combine
from app.importing.combine import (
    CombineSkipped,
    build_spec,
    cleanup_parts,
    discard,
    mark,
    publish_combined,
    remove_combined,
    restore_parts,
)

OPF = '<?xml version="1.0"?><package><metadata><title>Dark Age</title></metadata></package>'


def library(tmp_path, parts=3):
    root, staging = tmp_path / "library", tmp_path / "staging"
    staging.mkdir(mode=0o700, parents=True)
    for index in range(1, parts + 1):
        folder = root / "Writer" / f"Dark Age (Part {index} of {parts})"
        folder.mkdir(parents=True)
        (folder / f"{index:02}.mp3").write_bytes(b"audio" * index)
        (folder / f"{index:02}b.mp3").write_bytes(b"more" * index)
        (folder / "metadata.opf").write_text(f"<title>Dark Age (Part {index} of {parts})</title>")
    (root / "Writer" / "Dark Age (Part 1 of 3)" / "cover.jpg").write_bytes(b"\xff\xd8jpg\xff\xd9")
    return root, staging


def context(root, staging, parts=3, **changes):
    return {
        "combine_id": uuid4(),
        "root": root,
        "staging": staging,
        "backend_path": "/books",
        "folder": "Writer/Dark Age",
        "sidecars": {"metadata.opf": OPF},
        "parts": [
            {
                "index": index,
                "total": parts,
                "asset_id": UUID(int=index),
                "item_id": f"item-{index}",
                "source": f"Writer/Dark Age (Part {index} of {parts})",
                "audio": {
                    f"/books/Writer/Dark Age (Part {index} of {parts})/{index:02}.mp3": 5 * index,
                    f"/books/Writer/Dark Age (Part {index} of {parts})/{index:02}b.mp3": 4 * index,
                },
            }
            for index in range(1, parts + 1)
        ],
        **changes,
    }


def inode(path):
    return os.stat(path).st_ino


def test_parts_become_disc_folders_of_hard_links(tmp_path):
    root, staging = library(tmp_path)
    spec = build_spec(context(root, staging))
    publish_combined(spec)
    book = root / "Writer" / "Dark Age"
    assert sorted(path.name for path in book.iterdir()) == [
        "Disc 1",
        "Disc 2",
        "Disc 3",
        "cover.jpg",
        "metadata.opf",
    ]
    assert (book / "metadata.opf").read_text() == OPF
    original = root / "Writer" / "Dark Age (Part 2 of 3)" / "02.mp3"
    assert inode(book / "Disc 2" / "02.mp3") == inode(original)
    # Part metadata would make the scanner read the book as one part again.
    assert not (book / "Disc 2" / "metadata.opf").exists()
    assert sorted(path.name for path in (book / "Disc 1").iterdir()) == [
        "01.mp3",
        "01b.mp3",
        "cover.jpg",
    ]
    assert spec.expected_audio() == {
        "/books/Writer/Dark Age/Disc 1/01.mp3": 5,
        "/books/Writer/Dark Age/Disc 1/01b.mp3": 4,
        "/books/Writer/Dark Age/Disc 2/02.mp3": 10,
        "/books/Writer/Dark Age/Disc 2/02b.mp3": 8,
        "/books/Writer/Dark Age/Disc 3/03.mp3": 15,
        "/books/Writer/Dark Age/Disc 3/03b.mp3": 12,
    }
    # Nothing is removed until the combined book is confirmed.
    assert original.exists()
    with pytest.raises(combine.CombineError):
        cleanup_parts(spec)


def test_cleanup_and_separate_round_trip(tmp_path):
    root, staging = library(tmp_path)
    spec = build_spec(context(root, staging))
    originals = {
        index: inode(root / "Writer" / f"Dark Age (Part {index} of 3)" / f"{index:02}.mp3")
        for index in (1, 2, 3)
    }
    publish_combined(spec)
    mark(spec, "handed-over")
    assert cleanup_parts(spec) == []
    assert sorted(path.name for path in (root / "Writer").iterdir()) == ["Dark Age"]
    # Part metadata waits in private staging, not in the library.
    archived = staging / f"combine-archive-{spec.combine_id.hex}" / "Disc 2" / "metadata.opf"
    assert archived.read_text() == "<title>Dark Age (Part 2 of 3)</title>"
    mark(spec, "done")

    restore_parts(spec)
    for index in (1, 2, 3):
        folder = root / "Writer" / f"Dark Age (Part {index} of 3)"
        assert inode(folder / f"{index:02}.mp3") == originals[index]
        assert (
            folder / "metadata.opf"
        ).read_text() == f"<title>Dark Age (Part {index} of 3)</title>"
    assert (root / "Writer" / "Dark Age (Part 1 of 3)" / "cover.jpg").exists()
    with pytest.raises(combine.CombineError):
        remove_combined(spec)
    mark(spec, "separated")
    assert remove_combined(spec)
    assert sorted(path.name for path in (root / "Writer").iterdir()) == [
        "Dark Age (Part 1 of 3)",
        "Dark Age (Part 2 of 3)",
        "Dark Age (Part 3 of 3)",
    ]
    assert not (staging / f"combine-archive-{spec.combine_id.hex}").exists()


@pytest.mark.parametrize("step", ["staged", "published-before-receipt"])
def test_an_interrupted_publish_resumes(tmp_path, step):
    root, staging = library(tmp_path)
    spec = build_spec(context(root, staging))

    def crash(name):
        if name == step:
            raise RuntimeError(name)

    with pytest.raises(RuntimeError):
        publish_combined(spec, checkpoint=crash)
    receipt = publish_combined(spec)
    assert receipt["state"] == "published"
    assert (root / "Writer" / "Dark Age" / "Disc 3" / "03.mp3").exists()


def test_a_taken_folder_name_leaves_the_library_alone(tmp_path):
    root, staging = library(tmp_path)
    (root / "Writer" / "Dark Age").mkdir()
    (root / "Writer" / "Dark Age" / "other.mp3").write_bytes(b"x")
    spec = build_spec(context(root, staging))
    with pytest.raises(CombineSkipped, match="already exists"):
        publish_combined(spec)
    discard(spec)
    assert sorted(path.name for path in (root / "Writer" / "Dark Age").iterdir()) == ["other.mp3"]
    assert not list(staging.glob("combine-*"))


def test_cleanup_keeps_a_part_file_that_changed_after_the_plan(tmp_path):
    root, staging = library(tmp_path)
    spec = build_spec(context(root, staging))
    publish_combined(spec)
    mark(spec, "handed-over")
    replaced = root / "Writer" / "Dark Age (Part 2 of 3)" / "02.mp3"
    replaced.unlink()
    replaced.write_bytes(b"re-encoded")
    assert cleanup_parts(spec) == ["Writer/Dark Age (Part 2 of 3)"]
    assert replaced.read_bytes() == b"re-encoded"
    assert not (root / "Writer" / "Dark Age (Part 1 of 3)").exists()
    assert (root / "Writer" / "Dark Age" / "Disc 2" / "02.mp3").read_bytes() == b"audio" * 2


def test_parts_with_their_own_discs_or_other_files_are_skipped(tmp_path):
    root, staging = library(tmp_path)
    nested = root / "Writer" / "Dark Age (Part 3 of 3)" / "CD 2"
    nested.mkdir()
    (nested / "x.mp3").write_bytes(b"x")
    with pytest.raises(CombineSkipped, match="own disc folders"):
        build_spec(context(root, staging))

    root, staging = library(tmp_path / "other")
    mismatch = context(root, staging)
    mismatch["parts"][1]["audio"] = {"/books/Writer/Dark Age (Part 2 of 3)/elsewhere.mp3": 3}
    with pytest.raises(CombineSkipped, match="don't match what Audiobookshelf reports"):
        build_spec(mismatch)


def test_single_file_parts_become_discs(tmp_path):
    root, staging = tmp_path / "library", tmp_path / "staging"
    staging.mkdir(mode=0o700)
    root.mkdir()
    for index in (1, 2):
        (root / f"Dark Age {index}.m4b").write_bytes(b"m4b" * index)
    spec = build_spec(
        {
            **context(root, staging, parts=2),
            "folder": "Dark Age",
            "parts": [
                {
                    "index": index,
                    "total": 2,
                    "asset_id": UUID(int=index),
                    "item_id": f"item-{index}",
                    "source": f"Dark Age {index}.m4b",
                    "audio": {f"/books/Dark Age {index}.m4b": 3 * index},
                }
                for index in (1, 2)
            ],
        }
    )
    publish_combined(spec)
    mark(spec, "handed-over")
    assert cleanup_parts(spec) == []
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*.m4b")) == [
        "Dark Age/Disc 1/Dark Age 1.m4b",
        "Dark Age/Disc 2/Dark Age 2.m4b",
    ]
    mark(spec, "done")
    restore_parts(spec)
    mark(spec, "separated")
    assert remove_combined(spec)
    assert sorted(path.name for path in root.iterdir()) == ["Dark Age 1.m4b", "Dark Age 2.m4b"]
