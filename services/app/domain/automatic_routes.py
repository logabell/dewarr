"""Shared, explicitly approved routes for unattended list and series acquisitions."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.db.models import ImportDestination
from app.domain.acquisition_selection import verified_probe
from app.domain.automatic_dispatch import approve_route
from app.domain.destination_defaults import configured_destinations, destination_default
from app.domain.downloader_defaults import protocol_default
from app.domain.downloaders import (
    client_protocol,
    connection_or_404,
    mapped_path,
    transfer_connection,
)
from app.importing.destinations import destination_configuration
from app.importing.naming import fingerprint
from app.importing.storage import import_sources


class PolicyRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_id: UUID
    destination_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class AutomaticRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    downloader_id: UUID | None = None
    downloader_generation: int | None = Field(default=None, ge=1)
    routes: dict[Literal["ebook", "audio"], PolicyRoute] = Field(default_factory=dict)
    alternate_downloader_id: UUID | None = None
    alternate_downloader_generation: int | None = Field(default=None, ge=1)
    alternate_routes: dict[Literal["ebook", "audio"], PolicyRoute] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def compact(self, handler):
        values = handler(self)
        if not values.get("alternate_downloader_id"):
            values.pop("alternate_downloader_id", None)
            values.pop("alternate_downloader_generation", None)
            values.pop("alternate_routes", None)
        return values


def selection_clients(routes, medium):
    """Primary route plus the other protocol when that import route is verified."""
    route = routes.routes[medium]
    values = {
        "downloader_id": routes.downloader_id,
        "downloader_generation": routes.downloader_generation,
        "destination_id": route.destination_id,
        "destination_revision": route.destination_revision,
    }
    alternate = routes.alternate_routes.get(medium)
    if routes.alternate_downloader_id and routes.alternate_downloader_generation and alternate:
        values.update(
            alternate_downloader_id=routes.alternate_downloader_id,
            alternate_downloader_generation=routes.alternate_downloader_generation,
            alternate_destination_id=alternate.destination_id,
            alternate_destination_revision=alternate.destination_revision,
        )
    return values


async def inherit(db, user, spec, profile, options):
    """Resolve defaults once for a review; validation never renews saved consent."""
    permitted(user)
    preferences = profile.preferences
    downloader_id = options.downloader_id
    inherited_legacy = False
    if not downloader_id:
        # A saved torrent client stays primary. The Usenet client is the fallback
        # unless it is the only saved downloader.
        downloader_id = (
            preferences.torrent_downloader_id
            or preferences.downloader_id
            or preferences.usenet_downloader_id
        )
        inherited_legacy = bool(
            preferences.downloader_id
            and not preferences.torrent_downloader_id
            and downloader_id == preferences.downloader_id
        )
    if not downloader_id:
        for protocol in ("torrent", "nzb", "soulseek"):
            downloader_id = await protocol_default(db, preferences, protocol)
            if downloader_id:
                break
    if not downloader_id:
        raise HTTPException(
            422,
            "Choose a default downloader for each type in Settings, "
            "or connect a client if none is configured",
        )
    downloader = await transfer_connection(db, downloader_id)
    generation = options.downloader_generation
    if options.downloader_id:
        if generation is None:
            raise HTTPException(422, "Refresh the selected downloader before previewing")
    else:
        if generation is not None:
            raise HTTPException(422, "Choose a downloader before specifying its revision")
        generation = downloader.credential_generation
    media = {spec.mode} if spec.mode in {"ebook", "audio"} else {"ebook", "audio"}
    if set(options.routes) - media:
        raise HTTPException(422, "Choose destinations only for the requested media")
    routes = {}
    origins = {}
    if not options.downloader_id and inherited_legacy:
        origins["downloader_id"] = profile.origins.get("downloader_id", "Saved default")
    for medium in sorted(media):
        if medium in options.routes:
            route = options.routes[medium]
        else:
            field = medium + "_destination_id"
            destination_id = getattr(profile.preferences, field)
            destination = await destination_default(
                db, user, medium, getattr(spec, medium + "_library_id", None), destination_id
            )
            config = await destination_configuration(db, destination)
            route = PolicyRoute(
                destination_id=destination.id, destination_revision=fingerprint(config)
            )
            origins[field] = (
                profile.origins.get(field, "Saved default")
                if destination_id == destination.id
                else "Configured library folder"
            )
        routes[medium] = route
    alternate_id, alternate_generation, alternate_routes = await fallback_routes(
        db, user, spec, profile, downloader, routes
    )
    return AutomaticRoutes(
        downloader_id=downloader_id,
        downloader_generation=generation,
        routes=routes,
        alternate_downloader_id=alternate_id,
        alternate_downloader_generation=alternate_generation,
        alternate_routes=alternate_routes,
    ), origins


async def other_client(db, preferences, primary):
    protocol = client_protocol(primary.kind)
    if protocol == "soulseek":
        return None
    wanted = "nzb" if protocol == "torrent" else "torrent"
    other_id = await protocol_default(db, preferences, wanted)
    if not other_id or other_id == primary.id:
        return None
    try:
        other = await connection_or_404(db, other_id)
    except HTTPException:
        return None
    if not other.enabled or other.status != "connected" or client_protocol(other.kind) != wanted:
        return None
    try:
        mapped_path(other, other.config["save_path"], await import_sources(db))
    except HTTPException:
        return None
    return other


async def verified_destination(db, user, medium, mapping, destination_id):
    destination = await db.scalar(
        configured_destinations(user).where(
            ImportDestination.id == destination_id,
            ImportDestination.medium == medium,
        )
    )
    if destination:
        config = await destination_configuration(db, destination)
        if await verified_probe(db, destination, config, mapping):
            return PolicyRoute(
                destination_id=destination.id, destination_revision=fingerprint(config)
            )
    return None


async def fallback_routes(db, user, spec, profile, primary, routes):
    other = await other_client(db, profile.preferences, primary)
    if not other or not routes:
        return None, None, {}
    sources = await import_sources(db)
    other_mapping = mapped_path(other, other.config["save_path"], sources)
    primary_mapping = mapped_path(primary, primary.config["save_path"], sources)
    same_folder = (
        other_mapping["source_key"] == primary_mapping["source_key"]
        and other_mapping["relative_path"] == primary_mapping["relative_path"]
    )
    alternate = {}
    for medium, route in routes.items():
        chosen = route
        if not same_folder:
            chosen = await verified_destination(
                db,
                user,
                medium,
                other_mapping,
                route.destination_id,
            )
        if chosen and await automatic_import_approved(db, user, chosen):
            alternate[medium] = chosen
    if set(alternate) != set(routes):
        return None, None, {}
    return other.id, other.credential_generation, alternate


async def automatic_import_approved(db, user, route):
    """A fallback must not be stored unless unattended grabs can use it."""
    try:
        await approve_route(db, user.id, route.destination_id, route.destination_revision)
    except HTTPException:
        return False
    return True


def permitted(user):
    from app.domain.permissions import (
        MANAGE_REQUESTS,
        approval_dispatch,
        auto_approves,
        automation_allowed,
        download_authorization,
        has,
    )

    spec = download_authorization.get()
    if (
        spec is not None
        and user
        and user.active
        and user.role != "viewer"
        and auto_approves(user, spec)
    ):
        return
    if (
        approval_dispatch.get()
        and user
        and user.active
        and user.role != "viewer"
        and has(user, MANAGE_REQUESTS)
        and auto_approves(user, spec)
    ):
        return
    if not automation_allowed(user):
        raise HTTPException(403, "An administrator must grant automation permission")


async def resolve(db, user, spec, downloader_id, generation, routes):
    permitted(user)
    if not downloader_id or generation is None:
        raise HTTPException(422, "Choose a tested downloader")
    downloader = await transfer_connection(db, downloader_id)
    if (
        not downloader.enabled
        or downloader.status != "connected"
        or downloader.credential_generation != generation
    ):
        raise HTTPException(409, "Downloader settings changed; test and preview again")
    mapping = mapped_path(downloader, downloader.config["save_path"], await import_sources(db))
    media = {spec.mode} if spec.mode in {"ebook", "audio"} else {"ebook", "audio"}
    if set(routes) != media:
        raise HTTPException(422, "Choose an import destination for each requested medium")
    libraries, approvals = {}, {}
    for medium in sorted(media):
        route = routes[medium]
        approval = await approve_route(
            db, user.id, route.destination_id, route.destination_revision
        )
        destination = await db.get(ImportDestination, route.destination_id)
        config = await destination_configuration(db, destination)
        if destination.medium != medium or not await verified_probe(
            db, destination, config, mapping
        ):
            raise HTTPException(409, "Verify each download-to-library route before activation")
        expected = getattr(spec, medium + "_library_id")
        if expected and expected != destination.library_id:
            raise HTTPException(422, "Destination conflicts with the requested library")
        libraries[medium + "_library_id"] = str(destination.library_id)
        approvals[medium] = approval
    return libraries, approvals
