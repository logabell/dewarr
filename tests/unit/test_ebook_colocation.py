import errno
import os
from uuid import uuid4

import pytest

from app.importing.colocate import (
    place_companion,
    source_evidence,
    target_folder_identity,
    verify_companion,
)
from app.importing.publication import PublicationError


def placement(tmp_path):
    source, target, stage = (tmp_path / name for name in ("ebooks", "audio", "stage"))
    source.mkdir()
    target.mkdir()
    stage.mkdir(mode=0o700)
    (target / "Narration").mkdir()
    (source / "Book.epub").write_bytes(b"complete ebook contents")
    config = {
        "source": {"root_path": str(source)},
        "target": {"root_path": str(target), "staging_path": str(stage)},
        "source_relative": "Book.epub",
        "target_relative": "Narration/Book.epub",
    }
    config["target_folder_identity"] = target_folder_identity(config)
    return config, source / "Book.epub", target / "Narration/Book.epub"


@pytest.mark.parametrize("copy", [False, True])
def test_ebook_placement_links_or_copies_and_resumes_after_publication(tmp_path, monkeypatch, copy):
    config, original, target = placement(tmp_path)
    if copy:
        original_link = os.link

        def cross_device(source, *args, **kwargs):
            if source == "Book.epub":
                raise OSError(errno.EXDEV, "different filesystems")
            return original_link(source, *args, **kwargs)

        monkeypatch.setattr(os, "link", cross_device)
    expected = source_evidence(config)
    identifier = uuid4()

    def interrupted(stage):
        if stage == "published":
            raise RuntimeError("crashed before database receipt")

    with pytest.raises(RuntimeError, match="crashed"):
        place_companion(identifier, config, expected, checkpoint=interrupted)
    receipt = place_companion(identifier, config, expected)
    assert target.read_bytes() == original.read_bytes()
    assert (target.stat().st_ino == original.stat().st_ino) is not copy
    assert verify_companion(config, receipt)["sha256"] == expected["sha256"]
    target.write_bytes(b"changed")
    with pytest.raises(PublicationError, match="changed"):
        verify_companion(config, receipt)


def test_ebook_placement_never_replaces_conflicting_files_or_follows_links(tmp_path):
    config, original, target = placement(tmp_path)
    expected = source_evidence(config)
    target.write_bytes(b"someone else's ebook")
    with pytest.raises(PublicationError, match="left untouched"):
        place_companion(uuid4(), config, expected)
    assert target.read_bytes() == b"someone else's ebook"
    target.unlink()
    target.symlink_to(original)
    with pytest.raises(OSError):
        place_companion(uuid4(), config, expected)
    assert original.read_bytes() == b"complete ebook contents"


def test_replaced_audiobook_folder_invalidates_placement_intent(tmp_path):
    config, _, target = placement(tmp_path)
    expected = source_evidence(config)
    target.parent.rename(target.parent.with_name("Moved"))
    target.parent.mkdir()
    with pytest.raises(PublicationError, match="folder changed"):
        place_companion(uuid4(), config, expected)
    assert not target.exists()


def test_shared_placement_deadline_stops_before_publication_and_resumes(tmp_path, monkeypatch):
    from app.importing import colocate

    config, _, target = placement(tmp_path)
    now = [0.0]
    monkeypatch.setattr(colocate.time, "monotonic", lambda: now[0])
    expected = source_evidence(config, deadline=10)
    identifier = uuid4()

    def exhaust_budget(stage):
        if stage == "staged":
            now[0] = 11

    with pytest.raises(PublicationError, match="time budget"):
        place_companion(identifier, config, expected, deadline=10, checkpoint=exhaust_budget)
    assert not target.exists()
    receipt = place_companion(identifier, config, expected, deadline=20)
    assert verify_companion(config, receipt, deadline=20)["sha256"] == expected["sha256"]


def test_source_hash_respects_expired_page_budget(tmp_path):
    config, _, target = placement(tmp_path)
    with pytest.raises(PublicationError, match="time budget"):
        source_evidence(config, deadline=0)
    assert not target.exists()
