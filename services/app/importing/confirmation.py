"""Share ABS discovery work for a bounded batch of already published imports."""

from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select

from app.db.models import ImportDestination, ImportEntry, Integration, Library, Operation
from app.db.session import session_factory
from app.importing.publication import PublicationError
from app.security import decrypt_secrets


class DiscoveryBatch:
    """A candidate-only view; final item reads still go directly to the backend.

    Only paths needed by this batch are retained. Missing paths disable sharing
    for older backend contracts, which keep the ordinary streaming discovery.
    """

    def __init__(self, adapter, library_id, paths):
        self.adapter, self.library_id, self.paths = adapter, library_id, set(paths)
        self.rows = None
        self.scope = None
        self.target_path = None
        self.ambiguous = set()
        self.scanned = self.unsupported = False

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        # The enclosing batch owns the actual client's lifetime.
        pass

    async def authorize(self):
        capabilities, scope = await self.adapter.authorize()
        if self.scope != scope:
            self.rows = None
        self.scope = scope
        return capabilities, scope

    async def scan(self, library_id):
        if library_id != self.library_id or self.unsupported:
            return await self.adapter.scan(library_id)
        if not self.scanned:
            self.scanned = True
            self.rows = None
            await self.adapter.scan(library_id)

    async def page(self, library_id, page):
        if library_id != self.library_id or self.unsupported:
            return await self.adapter.page(library_id, page)
        if self.rows is None:
            matches, counts, offset, expected = [], {}, 0, None
            ambiguous = set()
            while True:
                rows, total = await self.adapter.page(library_id, offset)
                if expected is not None and total != expected:
                    raise PublicationError(
                        "Library inventory changed during confirmation; retry detection"
                    )
                if any(not isinstance(row.get("path"), str) for row in rows):
                    self.unsupported = True
                    return await self.adapter.page(library_id, page)
                for row in rows:
                    if row["path"] in self.paths:
                        counts[row["path"]] = counts.get(row["path"], 0) + 1
                        if counts[row["path"]] > 2:
                            ambiguous.add(row["path"])
                            continue
                        matches.append(row)
                if (offset + 1) * self.page_size >= total:
                    break
                if not rows:
                    raise PublicationError("Library did not provide complete pagination")
                expected, offset = total, offset + 1
            self.rows = matches
            self.ambiguous = ambiguous
        if self.target_path in self.ambiguous:
            raise PublicationError("Library reports duplicate items for this import folder")
        rows = (
            [row for row in self.rows if row["path"] == self.target_path]
            if self.target_path
            else self.rows
        )
        start = page * self.page_size
        return rows[start : start + self.page_size], len(rows)


async def execute_batch(operation_ids):
    from app.importing import execution

    identifiers = [UUID(value) for value in operation_ids]
    if not identifiers or len(identifiers) > 20:
        raise ValueError("Confirmation batches require 1 to 20 operations")
    async with session_factory()() as db:
        rows = (
            await db.execute(
                select(ImportEntry, Library, Integration)
                .join(Operation, Operation.id == ImportEntry.operation_id)
                .join(ImportDestination, ImportDestination.id == ImportEntry.destination_id)
                .join(Library, Library.id == ImportDestination.library_id)
                .join(Integration, Integration.id == Library.integration_id)
                .where(
                    Operation.id.in_(identifiers),
                    Operation.kind == "organization.publish",
                    ImportEntry.state == "awaiting-library",
                    ImportEntry.published_at.is_not(None),
                )
            )
        ).all()
        if not rows:
            return
        library, integration = rows[0][1:]
        if integration.kind != "audiobookshelf" or any(row[1].id != library.id for row in rows):
            raise ValueError("Confirmation batches must address one Audiobookshelf library")
        url, secret = integration.base_url, decrypt_secrets(integration.encrypted_secrets)["token"]
        paths = {
            entry.operation_id: str(
                PurePosixPath(entry.configuration["destination"]["backend_path"])
                / entry.specification["folder"]
            )
            for entry, _, _ in rows
        }
        selected = {entry.operation_id for entry, _, _ in rows}
        external_library = library.external_id
    async with execution.Audiobookshelf(url, secret) as adapter:
        discovery = DiscoveryBatch(adapter, external_library, paths.values())

        def client_factory(current_url, current_secret):
            if current_url != url or current_secret != secret:
                raise PublicationError(
                    "Library connection changed during confirmation; retry detection"
                )
            return discovery

        for identifier in identifiers:
            if identifier in selected:
                discovery.target_path = paths[identifier]
                await execution.execute(identifier, client_factory=client_factory)
