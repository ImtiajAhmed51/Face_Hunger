"""Libraries API routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..deps import cluster, config, db, services
from ..netlib import LibraryOffline, probe
from ..scanner import authorized_root
from ..schemas import ConfirmBody, LibraryBody, LibraryPatchBody, LibraryRelinkBody, LibraryScanBody
from ..services.presenters import _job_row, _require_csrf
from ..services.shares import ShareError
from .fs import fs_guard

router = APIRouter()

@router.get("/api/libraries")
def list_libraries():
    return {"items": services().libraries.list()}


@router.post("/api/libraries")
def add_library(body: LibraryBody, request: Request):
    _require_csrf(request)
    path = Path(body.path).expanduser()
    online, reason = probe(str(path))
    if not online:
        raise HTTPException(400, f"That folder cannot be opened ({reason}). If it is on a share, connect the share first.")
    path = path.resolve()
    try:
        authorized_root(str(path), config.roots)
    except Exception:
        # Adding a library is the user's consent for that folder (same rule as the folder chooser).
        fs_guard(request)
        services().libraries.allow(path)
    name = (body.name or "").strip()[:120] or path.name or str(path)
    ignored = body.ignored or []
    for existing in db.all("SELECT path, name FROM libraries"):
        other = Path(existing["path"])
        if path == other or path.is_relative_to(other) or other.is_relative_to(path):
            raise HTTPException(400, f"This folder overlaps the library \"{existing['name']}\". Use Edit folders on that library instead.")
    with db.connect() as conn:
        lid = conn.execute("INSERT INTO libraries(path, name, ignored) VALUES (?,?,?)", (str(path), name, json.dumps(ignored))).lastrowid
    row = services().libraries.identify(lid)
    services().watcher.refresh()
    return services().libraries.describe(row)


def _manage(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, "Library not found") from exc
    except LibraryOffline as exc:
        raise HTTPException(409, {"offline": True, "message": str(exc)}) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/api/libraries/{library_id}")
def patch_library(library_id: int, body: LibraryPatchBody, request: Request):
    _require_csrf(request)
    settings = {k: v for k, v in body.model_dump().items() if k in ("scan_videos", "poll_minutes", "concurrency", "pause_on_battery") and v is not None}
    return _manage(lambda: services().libraries.update(library_id, name=body.name, ignored=body.ignored, settings=settings, paused=body.paused))


@router.post("/api/libraries/{library_id}/scan")
def scan_library(library_id: int, request: Request, body: LibraryScanBody | None = None):
    """Scan now. Only new and changed files are read; an offline library answers 409 and changes nothing."""
    _require_csrf(request)
    body = body or LibraryScanBody()
    return _manage(lambda: _job_row(services().worker.start(library_id, force=body.force, quick=body.quick)))


@router.post("/api/libraries/{library_id}/relink")
def relink_library(library_id: int, body: LibraryRelinkBody, request: Request):
    """Point the library at a new folder without re-indexing. Without ``apply`` it only reports how well a sample matches."""
    fs_guard(request)
    return _manage(lambda: services().libraries.relink(library_id, body.path, apply=body.apply))


@router.post("/api/libraries/{library_id}/reconnect")
def reconnect_library(library_id: int, request: Request):
    """Try again now: re-mount the share if we know it (with a remembered password), then re-check."""
    fs_guard(request)
    row = db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not row:
        raise HTTPException(404, "Library not found")
    error = None
    if row.get("status") == "offline" and row.get("share_server") and row.get("share_name") and not services().shares.find(row["share_server"], row["share_name"]):
        try:
            services().shares.connect(protocol=row.get("share_protocol") or "smb", server=row["share_server"], share=row["share_name"], guest=False)
        except ShareError as exc:
            error = exc.to_dict()
    report = services().libraries.tick()
    fresh = services().libraries.describe(db.one("SELECT * FROM libraries WHERE id=?", (library_id,)))
    return {"library": fresh, "online": library_id in report["online"], "error": error}


@router.get("/api/libraries/{library_id}/health")
def library_health(library_id: int):
    return _manage(lambda: services().libraries.health(library_id))


@router.post("/api/libraries/{library_id}/test")
def test_library(library_id: int, request: Request):
    _require_csrf(request)
    return _manage(lambda: services().libraries.test_connection(library_id))


@router.delete("/api/libraries/{library_id}")
def delete_library(library_id: int, body: ConfirmBody, request: Request):
    _require_csrf(request)
    if body.confirm != "REMOVE LIBRARY":
        raise HTTPException(400, "confirm must be 'REMOVE LIBRARY'")
    row = db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not row:
        raise HTTPException(404, "Library not found")
    with db.connect() as conn:
        # cascade deletes media/faces via FK
        conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    cluster.invalidate()
    services().watcher.refresh()
    services().libraries.refresh_protection()
    services().libraries.offline_ids.discard(library_id)
    return {"ok": True}
