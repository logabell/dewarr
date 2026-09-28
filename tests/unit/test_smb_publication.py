"""SMB operation boundaries that local POSIX filesystem journeys cannot exercise."""
# ruff: noqa: F811

import pytest

from app.importing import publication
from tests.filesystem_fixtures import smb_open_children  # noqa: F401
from tests.unit.test_import_publication import specification  # noqa: F401
from tests.unit.test_setup_probe_files import roots  # noqa: F401


@pytest.mark.usefixtures("smb_open_children")
@pytest.mark.parametrize("mode", ["copy", "hardlink"])
def test_real_import_closes_children_before_publication(specification, mode):
    spec = specification.model_copy(update={"mode": mode})
    before = (spec.source_root / "pack/book.epub").read_bytes()
    result = publication.publish_item(spec)
    assert result["state"] == "published"
    assert (spec.destination_root / spec.folder / "First Harbor.epub").read_bytes() == before
    assert (spec.source_root / "pack/book.epub").read_bytes() == before
