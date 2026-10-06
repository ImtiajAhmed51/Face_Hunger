"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .ops.logging import RequestIdMiddleware
from .routers import (
    assistant,
    cleanup,
    dashboard,
    duplicates,
    edits,
    events,
    exclusions,
    export,
    faces,
    geo,
    health,
    jobs,
    libraries,
    library,
    media,
    models,
    packages,
    people,
    quality,
    review,
    search,
    settings,
    sharing,
    storage,
    video,
)
from .services.container import Services, set_current

# Registration order matters: static media paths (e.g. /api/media/soft-originals)
# must precede /api/media/{media_id}, matching the original single-module layout.
ROUTERS = (
    dashboard.router,
    people.router,
    faces.router,
    media.router,
    review.router,
    exclusions.router,
    search.router,
    cleanup.router,
    duplicates.router,
    libraries.router,
    jobs.router,
    settings.router,
    export.router,
    models.router,
    health.router,
    geo.router,
    quality.router,
    events.router,
    video.router,
    library.router,
    edits.router,
    sharing.router,
    packages.router,
    assistant.router,
    storage.router,
)


def create_app(config: Optional[Config] = None, services: Optional[Services] = None) -> FastAPI:
    config = config or (services.config if services else Config())
    services = services or Services(config)
    set_current(services)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: nothing heavy — engine and models stay lazy; the job runner and
        # watcher are light threads.
        services.start_background()
        yield
        services.close()

    app = FastAPI(
        title="Face Hunger",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8765", "http://localhost:8765", "http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in ROUTERS:
        app.include_router(router)
    _mount_frontend(app, config)
    return app


def _mount_frontend(app: FastAPI, config: Config) -> None:
    """Static frontend + SPA fallback."""
    frontend_dir = config.frontend_dir
    if not frontend_dir.is_dir():
        return
    assets_dir = frontend_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = frontend_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = frontend_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(404, "Frontend not built")


def iter_routes(routes):
    """Flatten app routes, descending into lazily included routers (FastAPI >= 0.140)."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from iter_routes(included.routes)
        else:
            yield route
