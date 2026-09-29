# ruff: noqa: F401, F811
"""Exercise a real inspected MAM audiobook whose tags include a catalog series label."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.db.models import WorkMetadataSource
from tests.abs_import_fixture import ScanningBackend
from tests.integration import test_single_file_acquisition as single
from tests.integration.test_single_file_acquisition import (
    destination_route,
    ready_route,
    resolution_provider,
    review_account,
)

pytestmark = pytest.mark.integration


async def test_catalog_series_annotation_imports_without_an_existing_audio_edition(
    client,
    admin,
    database,
    ready_route,
    monkeypatch,
    review_account,
    resolution_provider,
):
    original_scan = ScanningBackend.scan

    def scan_with_series(backend):
        original_scan(backend)
        for item in backend.items.values():
            item["media"]["metadata"]["series"] = [{"name": "The Harbor Saga", "sequence": "1"}]

    monkeypatch.setattr(ScanningBackend, "scan", scan_with_series)
    original_audio = single.audio
    original_prepare = single.prepare_audio_route

    def tagged_audio(path, **kwargs):
        kwargs["title"] = "First Harbor (Harbor #1) (Unabridged)"
        return original_audio(path, **kwargs)

    async def prepare(*args, **kwargs):
        result = await original_prepare(*args, **kwargs)
        async with database() as db, db.begin():
            db.add(
                WorkMetadataSource(
                    work_id=UUID(args[3]),
                    provider="hardcover",
                    external_id="series-file-test",
                    accepted=True,
                    fetched_at=datetime.now(UTC),
                    snapshot={
                        "title": "First Harbor",
                        "authors": ["Alex Morgan"],
                        "series": [{"name": "The Harbor Saga", "position": "1"}],
                    },
                )
            )
        return result

    monkeypatch.setattr(single, "audio", tagged_audio)
    monkeypatch.setattr(single, "prepare_audio_route", prepare)
    await single.test_single_epub_download_to_confirmed_library_keeps_neighbor_private(
        client,
        admin,
        database,
        ready_route,
        monkeypatch,
        "",
        "automatic-linked-audio",
        review_account,
        resolution_provider,
    )
