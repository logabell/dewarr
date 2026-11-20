"""Shared Prowlarr artifact resolution for manual and automatic acquisition."""

from fastapi import HTTPException

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.prowlarr import MagnetRedirect, ProwlarrArtifact
from app.adapters.qbittorrent import magnet_hashes
from app.domain.downloaders import resolve_metadata
from app.domain.prowlarr_network import prowlarr_call
from app.domain.source_artifacts import persist_artifact


async def resolve_prowlarr(
    user_id,
    release,
    reference,
    *,
    expected_generation,
    downloader_id=None,
    downloader_generation=None,
):
    """Resolve and persist one Prowlarr artifact.

    Inspect magnets via the selected qBittorrent."""
    result, generation = await prowlarr_call(
        user_id,
        "resolve",
        (release, reference),
        expected_generation=expected_generation,
    )

    expected_downloader = None
    if isinstance(result, MagnetRedirect):
        try:
            magnet_hashes(result.magnet)
        except ValueError:
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "Prowlarr returned an invalid magnet link.",
            ) from None

        if downloader_id is None:
            raise HTTPException(
                422,
                "This Prowlarr result redirects to a magnet. "
                "Select a qBittorrent downloader to inspect it.",
            )

        content, resolved_generation = await resolve_metadata(
            user_id,
            downloader_id,
            result.magnet,
            expected_generation=downloader_generation,
        )
        result = ProwlarrArtifact(release, content)
        expected_downloader = (downloader_id, resolved_generation)

    if not isinstance(result, ProwlarrArtifact):
        raise AdapterError(
            FailureKind.PARSER,
            "Prowlarr returned an unexpected artifact response.",
        )

    identifier = await persist_artifact(
        user_id,
        result.release.source_id,
        result,
        generation,
        "prowlarr",
        expected_downloader=expected_downloader,
    )
    return identifier, result.release
