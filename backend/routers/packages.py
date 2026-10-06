"""Encrypted .fhpack packages: export, inspect, import."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..deps import services
from ..jobs.manager import PRIORITY, job_row
from ..ops.fhpack import PackageError, WrongPassphrase
from ..schemas import PackageExportBody, PackageImportBody, PackageInspectBody
from ..services.presenters import _require_csrf

router = APIRouter()


@router.get("/api/packages")
def list_packages():
    s = services()
    jobs = s.db.all("SELECT * FROM jobs WHERE kind IN ('package_export','package_import') ORDER BY id DESC LIMIT 6")
    for row in jobs:
        row["payload"] = "{}"  # never echo scope details or secret tokens
    return {"items": s.packages.list(), "jobs": [job_row(r) for r in jobs]}


@router.post("/api/packages/export")
def export_package(body: PackageExportBody, request: Request):
    """Queue an encrypted export. The passphrase is held in memory only, never stored."""
    _require_csrf(request)
    s = services()
    if len(body.passphrase) < 8:
        raise HTTPException(400, "Use a passphrase of at least 8 characters")
    try:
        if not s.packages._scope_media(body.scope):
            raise HTTPException(400, "Nothing to export")
    except (PackageError, KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = {"scope": body.scope, "include_media": body.include_media, "include_thumbnails": body.include_thumbnails,
               "secret": s.packages.hold(body.passphrase)}
    return job_row({**s.jobs.enqueue("package_export", payload, priority=PRIORITY["normal"]), "payload": "{}"})


@router.get("/api/packages/files/{name}")
def download_package(name: str):
    try:
        path = services().packages.resolve(name)
    except PackageError as exc:
        raise HTTPException(404, str(exc)) from exc
    if path.parent != services().packages.folder():
        raise HTTPException(404, "Package file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.post("/api/packages/inspect")
def inspect_package(body: PackageInspectBody, request: Request):
    """Check the passphrase and read what a package contains, without importing anything."""
    _require_csrf(request)
    try:
        return services().packages.inspect(services().packages.resolve(body.source), body.passphrase)
    except WrongPassphrase as exc:
        raise HTTPException(403, str(exc)) from exc
    except PackageError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/packages/import")
def import_package(body: PackageImportBody, request: Request):
    """Verify the whole package, then merge it. Conflicts: skip | keep_both (default) | overwrite."""
    _require_csrf(request)
    s = services()
    try:
        s.packages.inspect(s.packages.resolve(body.source), body.passphrase)  # fail fast on a wrong passphrase
    except WrongPassphrase as exc:
        raise HTTPException(403, str(exc)) from exc
    except PackageError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = {"source": body.source, "conflict": body.conflict, "secret": s.packages.hold(body.passphrase)}
    return job_row({**s.jobs.enqueue("package_import", payload, priority=PRIORITY["normal"]), "payload": "{}"})
