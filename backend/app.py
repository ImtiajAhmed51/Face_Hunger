"""FastAPI application factory."""

from __future__ import annotations

import gzip
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Config
from .ops.logging import RequestIdMiddleware
from .routers import (
    assistant,
    cleanup,
    dashboard,
    diagnostics,
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
    lock,
    media,
    models,
    packages,
    people,
    plugins,
    quality,
    review,
    search,
    settings,
    sharing,
    storage,
    video,
)
from .security import SecurityMiddleware
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
    plugins.router,
    lock.router,
    diagnostics.router,
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
    app.add_middleware(SecurityMiddleware, services=services)
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


class CompressedStatic(StaticFiles):
    """Hashed build assets: gzip once (cached in memory by path and mtime) and cache forever in the browser."""

    _cache: dict = {}

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code != 200 or not isinstance(response, FileResponse):
            return response
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        accepts = dict(scope.get("headers") or {}).get(b"accept-encoding", b"").decode("latin-1")
        file = Path(response.path)
        if "gzip" not in accepts or file.suffix not in (".js", ".css", ".svg", ".json") or scope.get("method") != "GET" \
                or any(k == b"range" for k, _ in scope.get("headers") or []):
            return response
        stat = file.stat()
        key = (str(file), stat.st_mtime_ns)
        body = self._cache.get(key)
        if body is None:
            body = gzip.compress(file.read_bytes(), compresslevel=6, mtime=0)
            self._cache[key] = body
        return Response(body, media_type=response.media_type, headers={
            "Content-Encoding": "gzip", "Vary": "Accept-Encoding", "Cache-Control": response.headers["Cache-Control"],
            "ETag": response.headers.get("etag", "")})


def _mount_frontend(app: FastAPI, config: Config) -> None:
    """Static frontend + SPA fallback."""
    frontend_dir = config.frontend_dir
    if not frontend_dir.is_dir():
        return
    assets_dir = frontend_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", CompressedStatic(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        # Never serve anything outside the built frontend, whatever the URL decodes to.
        root = frontend_dir.resolve()
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_relative_to(root) and candidate.is_file():
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
