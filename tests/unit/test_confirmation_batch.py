import pytest

from app.importing.confirmation import DiscoveryBatch
from app.importing.publication import PublicationError


async def test_discovery_reuses_pages_invalidates_scope_and_isolates_duplicate_folders():
    class Backend:
        page_size = 2
        reads = scans = 0
        scope = "first"
        rows = [{"id": str(n), "path": "/books/duplicate"} for n in range(10)] + [
            {"id": "good", "path": "/books/good"}
        ]

        async def page(self, library_id, page):
            self.reads += 1
            return self.rows[page * 2 : page * 2 + 2], len(self.rows)

        async def authorize(self):
            return None, self.scope

        async def scan(self, library_id):
            self.scans += 1

    backend = Backend()
    batch = DiscoveryBatch(backend, "library", ["/books/duplicate", "/books/good"])
    await batch.authorize()
    batch.target_path = "/books/good"
    rows, count = await batch.page("library", 0)
    assert count == 1 and rows[0]["id"] == "good" and backend.reads == 6
    await batch.page("library", 0)
    assert backend.reads == 6
    batch.target_path = "/books/duplicate"
    with pytest.raises(PublicationError, match="duplicate"):
        await batch.page("library", 0)
    assert len(batch.rows) == 3  # Many duplicates do not fill the candidate cache.
    backend.scope = "changed"
    await batch.authorize()
    batch.target_path = "/books/good"
    await batch.page("library", 0)
    assert backend.reads == 12
    await batch.scan("library")
    await batch.scan("library")
    await batch.page("library", 0)
    assert backend.scans == 1 and backend.reads == 18
