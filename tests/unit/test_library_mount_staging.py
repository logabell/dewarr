# ruff: noqa: F811
import json
import os

import pytest

from app.importing.cancel_files import cancel_files
from app.importing.filesystem import identity
from app.importing.layout import STAGING_NAME
from app.importing.publication import PublicationSpec, publish_item
from app.importing.recovery import journal_census
from tests.filesystem_fixtures import group_mapped_media  # noqa: F401
from tests.unit.test_import_publication import specification  # noqa: F401


@pytest.mark.parametrize("mode", ["copy", "hardlink"])
def test_group_mapped_share_can_verify_and_import(specification, group_mapped_media, mode):
    from app.importing.publication import probe_destination, probe_download_folder

    spec = specification.model_copy(
        update={"mode": mode, "journal_root": specification.staging_root.parent / "journals"}
    )
    spec.journal_root.mkdir(mode=0o700)
    group_mapped_media(spec.source_root, spec.destination_root, spec.staging_root)
    original = spec.source_root / "pack/book.epub"
    spec.files[0].identity = identity(original.stat())
    before = original.read_bytes(), original.stat().st_mode
    mask = os.umask(0o002)
    try:
        # Both automatic setup and a selected-file check need the same access
        # as a real publication, including reopening media owned by the NAS UID.
        for report in (
            probe_download_folder(
                spec.source_root,
                "pack",
                spec.destination_root,
                spec.staging_root,
                journal_root=spec.journal_root,
            ),
            probe_destination(
                spec.source_root,
                spec.source_relative,
                spec.files[0],
                spec.destination_root,
                spec.staging_root,
                journal_root=spec.journal_root,
            ),
        ):
            assert report["copy"] and report["hardlink"]
        assert not list(spec.staging_root.iterdir())
        assert publish_item(spec)["state"] == "published"
    finally:
        os.umask(mask)
    assert (spec.destination_root / spec.folder / "First Harbor.epub").read_bytes() == before[0]
    assert (original.read_bytes(), original.stat().st_mode) == before
    assert all(path.stat().st_mode & 0o077 == 0 for path in spec.journal_root.iterdir())


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("mode", ["hardlink", "copy"])
@pytest.mark.parametrize("finish", ["resume", "cancel"])
def test_private_child_staging_survives_interruption(specification, mode, finish, protected):
    stage = specification.destination_root / STAGING_NAME
    stage.mkdir(mode=0o700)
    journals = stage.parent.parent / "control" if protected else None
    if journals:
        journals.mkdir(mode=0o700)
        stage.chmod(0o777)  # A NAS media mount need not provide private directory modes.
        (stage / "untrusted.json").write_text("not a control journal")
    spec = PublicationSpec.model_validate(
        {
            **specification.model_dump(),
            "staging_root": stage,
            "journal_root": journals,
            "mode": mode,
        }
    )
    original = spec.source_root / "pack/book.epub"
    before = original.read_bytes(), original.stat().st_ino, original.stat().st_mtime_ns

    def interrupt(phase):
        if phase == "prepared":
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        publish_item(spec, checkpoint=interrupt)
    assert list(spec.destination_root.iterdir()) == [stage]
    assert any(
        item.get("entry_id") == str(spec.entry_id) for item in journal_census(stage, journals)
    )
    journal = (journals or stage) / f"{spec.entry_id}.json"
    receipt = json.loads(journal.read_text())
    assert receipt["state"] == "prepared"
    if finish == "resume":
        receipt = publish_item(spec)
        assert receipt["state"] == "published"
        published = spec.destination_root / spec.folder / "First Harbor.epub"
        assert published.read_bytes() == before[0]
        assert (published.stat().st_ino == before[1]) == (mode == "hardlink")
        assert publish_item(spec) == receipt
    else:
        assert cancel_files(spec)["state"] == "cancelled"
        assert list(spec.destination_root.iterdir()) == [stage]
    if journals:
        assert not list(stage.glob("lock-*"))
        assert (stage / "untrusted.json").read_text() == "not a control journal"
        assert all(path.stat().st_mode & 0o077 == 0 for path in journals.iterdir())
    assert not list(stage.glob("item-*"))
    assert (original.read_bytes(), original.stat().st_ino, original.stat().st_mtime_ns) == before


@pytest.mark.parametrize("folder", [STAGING_NAME, f"{STAGING_NAME}/item", STAGING_NAME.upper()])
def test_publication_cannot_target_reserved_staging_namespace(specification, folder):
    with pytest.raises(ValueError, match="reserved"):
        PublicationSpec.model_validate({**specification.model_dump(), "folder": folder})


@pytest.mark.parametrize(
    "child", ["stage", "nested/.book-search-staging", ".book-search-staging/sub"]
)
def test_only_exact_managed_child_can_overlap_library(specification, child):
    with pytest.raises(ValueError, match="overlap"):
        PublicationSpec.model_validate(
            {**specification.model_dump(), "staging_root": specification.destination_root / child}
        )


@pytest.mark.parametrize("mode", ["copy", "hardlink"])
def test_shared_group_permissions_preserve_source_modes(specification, mode):
    import os
    import stat

    spec = specification.model_copy(update={"mode": mode})
    original = spec.source_root / "pack/book.epub"
    original.chmod(0o640)
    mask = os.umask(0o002)
    try:
        publish_item(spec)
    finally:
        os.umask(mask)
    book = spec.destination_root / spec.folder
    assert stat.S_IMODE(book.stat().st_mode) == 0o775
    assert stat.S_IMODE((book / "metadata.opf").stat().st_mode) == 0o664
    assert stat.S_IMODE((book / "First Harbor.epub").stat().st_mode) == (
        0o640 if mode == "hardlink" else 0o664
    )
    assert stat.S_IMODE(original.stat().st_mode) == 0o640


def test_shared_media_uses_protected_entry_locks(specification):
    from app.importing.publication import PublicationBusy, entry_lock, private_staging

    stage = specification.staging_root
    stage.chmod(0o777)
    control = stage.parent / "control"
    control.mkdir(mode=0o700)
    with private_staging(stage, control) as first, private_staging(stage, control) as second:
        with entry_lock(first, specification.entry_id):
            with pytest.raises(PublicationBusy):
                with entry_lock(second, specification.entry_id):
                    pytest.fail("The same import acquired two concurrent locks")
        with entry_lock(second, specification.entry_id):
            pass
    assert not list(stage.glob("lock-*"))
    assert len(list(control.glob("lock-*"))) == 1


@pytest.mark.parametrize("name", ["collection", "Reading Room", "shelf-42"])
def test_location_validation_accepts_shared_root_with_any_name(name):
    from pathlib import Path

    from app.importing.layout import check_library_layout

    root = Path("/") / name
    check_library_layout(
        {"ebook": root, "audio": root}, {"incoming": Path("/transfers")}, root / STAGING_NAME
    )


@pytest.mark.parametrize("conflict", ["download", "staging", "staging-download"])
def test_layout_conflict_identifies_the_two_actual_locations(conflict):
    from pathlib import Path

    from app.importing.layout import check_library_layout

    root, source, staging = Path("/shelf-42"), Path("/transfers"), Path("/shelf-42") / STAGING_NAME
    if conflict == "download":
        source = root / "incoming"
    elif conflict == "staging":
        staging = root / "old-incoming"
    else:
        staging = source / "temporary"
    with pytest.raises(ValueError) as error:
        check_library_layout({"my-books": root}, {"my-downloads": source}, staging)
    assert str(source if conflict != "staging" else root) in str(error.value)
    assert str(root if conflict == "download" else staging) in str(error.value)
