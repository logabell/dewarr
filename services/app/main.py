import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import (
    acquisition_preferences,
    acquisition_selections,
    application_release,
    audiobookbay,
    auth,
    automatic_imports,
    automatic_selection,
    book_sources,
    capacity,
    catalog,
    catalog_grouping,
    community_lists,
    configuration_deletion,
    connection_health,
    destinations,
    discovery,
    discovery_collections,
    download_attempts,
    download_reviews,
    downloaders,
    follows,
    identity,
    import_runs,
    imports,
    inspection_groupings,
    inspection_matches,
    integrations,
    library,
    library_combine,
    library_folders,
    library_review,
    list_comparisons,
    list_csv,
    list_discovery,
    list_policies,
    list_requests,
    list_subscriptions,
    list_writeback,
    list_writeback_review,
    lists,
    metadata,
    notifications,
    oidc,
    operations,
    organization,
    plex,
    prowlarr,
    reading_accounts,
    recovery,
    release_profiles,
    releases,
    request_quotas,
    requests,
    series,
    series_discovery,
    series_requests,
    setup,
    slskd,
    source_artifacts,
    sources,
)
from app.config import get_settings
from app.db.session import get_engine, session_factory
from app.diagnostics import auth_configuration
from app.jobs.queue import get_queue
from app.recovery import active_restore, restore_pending, runtime_lease


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.encryption_key()
    configuration = auth_configuration(settings)
    logging.getLogger(__name__).info(
        "Authentication configuration public_url=%s cookie_secure=%s proxy_identity_mode=%s",
        configuration["public_url"],
        configuration["cookie_secure"],
        configuration["proxy_identity_mode"],
    )
    for warning in configuration["warnings"]:
        logging.getLogger(__name__).warning("%s", warning)
    try:
        async with runtime_lease():
            async with session_factory()() as db:
                if await restore_pending(db):
                    if not await active_restore(db):
                        raise RuntimeError(
                            "Restore is incomplete; finish the offline restore procedure"
                        )
                    settings.recovery_mode = True
            async with get_queue().open_async():
                yield
    finally:
        await get_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Dewarr", version="0.3.0", lifespan=lifespan)
    app.include_router(application_release.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exception: RequestValidationError):
        # Validation failures must not echo passwords, tokens or private URLs.
        errors = [
            {key: error[key] for key in ("loc", "msg", "type")} for error in exception.errors()
        ]
        return JSONResponse({"detail": errors}, 422)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/api/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready", tags=["health"])
    async def readiness():
        try:
            async with get_engine().connect() as connection:
                ready = await connection.scalar(
                    text(
                        "SELECT to_regclass('public.operations') IS NOT NULL "
                        "AND to_regclass('book_queue.procrastinate_jobs') IS NOT NULL"
                    )
                )
            if ready:
                return {"status": "ready"}
        except SQLAlchemyError:
            pass
        return JSONResponse(
            {"status": "unavailable", "action": "Check database and migrations"}, 503
        )

    app.include_router(connection_health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(oidc.router, prefix="/api")
    app.include_router(plex.router, prefix="/api")
    app.include_router(setup.router, prefix="/api")
    app.include_router(recovery.router, prefix="/api")
    app.include_router(operations.router, prefix="/api")
    app.include_router(catalog.router, prefix="/api")
    app.include_router(catalog_grouping.router, prefix="/api")
    app.include_router(discovery.router, prefix="/api")
    app.include_router(discovery_collections.router, prefix="/api")
    app.include_router(series_discovery.router, prefix="/api")
    app.include_router(community_lists.router, prefix="/api")
    app.include_router(series.router, prefix="/api")
    app.include_router(series_requests.router, prefix="/api")
    app.include_router(lists.router, prefix="/api")
    app.include_router(follows.router, prefix="/api")
    app.include_router(list_subscriptions.router, prefix="/api")
    app.include_router(list_writeback.router, prefix="/api")
    app.include_router(list_writeback_review.router, prefix="/api")
    app.include_router(list_comparisons.router, prefix="/api")
    app.include_router(list_csv.router, prefix="/api")
    app.include_router(list_discovery.router, prefix="/api")
    app.include_router(reading_accounts.router, prefix="/api")
    app.include_router(list_requests.router, prefix="/api")
    app.include_router(list_policies.router, prefix="/api")
    app.include_router(integrations.router, prefix="/api")
    app.include_router(library.router, prefix="/api")
    app.include_router(library_review.router, prefix="/api")
    app.include_router(library_combine.router, prefix="/api")
    app.include_router(metadata.router, prefix="/api")
    app.include_router(identity.router, prefix="/api")
    app.include_router(requests.router, prefix="/api")
    app.include_router(download_attempts.router, prefix="/api")
    from app.api import download_recovery

    app.include_router(download_recovery.router, prefix="/api")
    app.include_router(download_reviews.router, prefix="/api")
    app.include_router(organization.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    app.include_router(inspection_groupings.router, prefix="/api")
    app.include_router(inspection_matches.router, prefix="/api")
    app.include_router(import_runs.router, prefix="/api")
    app.include_router(destinations.router, prefix="/api")
    app.include_router(configuration_deletion.router, prefix="/api")
    app.include_router(library_folders.router, prefix="/api")
    app.include_router(automatic_imports.router, prefix="/api")
    app.include_router(automatic_selection.router, prefix="/api")
    app.include_router(capacity.router, prefix="/api")
    app.include_router(request_quotas.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(book_sources.router, prefix="/api")
    app.include_router(release_profiles.router, prefix="/api")
    app.include_router(releases.router, prefix="/api")
    app.include_router(prowlarr.router, prefix="/api")
    app.include_router(audiobookbay.router, prefix="/api")
    app.include_router(slskd.router, prefix="/api")
    app.include_router(downloaders.router, prefix="/api")
    app.include_router(source_artifacts.router, prefix="/api")
    app.include_router(acquisition_preferences.router, prefix="/api")
    app.include_router(acquisition_selections.router, prefix="/api")
    dist: Path = get_settings().web_dist
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.startswith("api/") or not (dist / "index.html").is_file():
            return JSONResponse({"detail": "Not found"}, 404)
        return FileResponse(dist / "index.html")

    return app


app = create_app()
