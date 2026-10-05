"""Queue one Soulseek folder on the connected slskd downloader."""

from dataclasses import dataclass
from uuid import UUID

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.slskd import SlskdClient
from app.db.session import session_factory
from app.domain.slskd_connection import integration
from app.security import decrypt_secrets

OFFLINE = "Connect and test Soulseek before downloading this folder"


@dataclass(frozen=True)
class TransferRoute:
    downloader_id: UUID
    generation: int
    base_url: str


async def _credentials(*, require_enabled=True, expected_route=None):
    async with session_factory()() as db:
        row = await integration(db)
        if not row or (require_enabled and not row.enabled):
            return None
        route = TransferRoute(row.id, row.credential_generation, row.base_url)
        if expected_route is not None and route != expected_route:
            return None
        api_key = decrypt_secrets(row.encrypted_secrets).get("api_key")
        if not api_key:
            return None
        return route, api_key, row.status


async def queue_folder(release, attempt_id: str) -> TransferRoute:
    found = await _credentials()
    if not found or found[2] != "connected":
        raise AdapterError(FailureKind.UNSUPPORTED, OFFLINE)
    route, api_key, _status = found
    async with SlskdClient(route.base_url, api_key) as client:
        await client.enqueue(release, attempt_id=attempt_id)
    return route


async def cancel_folder(username: str, attempt_id: str, *, route: TransferRoute) -> None:
    # Revoking new downloads must not prevent rolling back an unaccepted batch.
    # Cleanup belongs only to the exact client that accepted that batch.
    found = await _credentials(require_enabled=False, expected_route=route)
    if not found:
        return
    current, api_key, _status = found
    try:
        async with SlskdClient(current.base_url, api_key) as client:
            await client.cancel(username, attempt_id)
    except AdapterError:
        return
