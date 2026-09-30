"""Libraries API routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..deps import cluster, config, db
from ..scanner import authorized_root
from ..schemas import ConfirmBody, LibraryBody, LibraryPatchBody
from ..services.presenters import _require_csrf

router = APIRouter()

@router.get("/api/libraries")
def list_libraries():
    rows = db.all("SELECT * FROM libraries ORDER BY id")
    items = []
    for r in rows:
        count = int((db.one(
            "SELECT COUNT(*) AS c FROM media WHERE library_id=? AND deleted_at IS NULL",
            (r["id"],),
        ) or {}).get("c") or 0)
        ignored = r.get("ignored") or "[]"
        if isinstance(ignored, str):
            ignored = json.loads(ignored)
        items.append({
            "id": r["id"],
            "name": r["name"],
            "path": r["path"],
            "ignored": ignored,
            "media_count": count,
        })
    return {"items": items}


@router.post("/api/libraries")
def add_library(body: LibraryBody, request: Request):
    _require_csrf(request)
    path = Path(body.path).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(400, f"Path is not a directory: {path}")
    try:
        authorized_root(str(path), config.roots)
    except Exception as exc:
        raise HTTPException(403, str(exc)) from exc
    name = path.name or str(path)
    ignored = body.ignored or []
    try:
        with db.connect() as conn:
            lid = conn.execute(
                "INSERT INTO libraries(path, name, ignored) VALUES (?,?,?)",
                (str(path), name, json.dumps(ignored)),
            ).lastrowid
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(400, "Library path already registered") from exc
        raise
    return {
        "id": lid,
        "name": name,
        "path": str(path),
        "ignored": ignored,
        "media_count": 0,
    }


@router.patch("/api/libraries/{library_id}")
def patch_library(library_id: int, body: LibraryPatchBody, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not row:
        raise HTTPException(404, "Library not found")
    with db.connect() as conn:
        conn.execute(
            "UPDATE libraries SET ignored=? WHERE id=?",
            (json.dumps(body.ignored), library_id),
        )
    return {
        "id": library_id,
        "name": row["name"],
        "path": row["path"],
        "ignored": body.ignored,
        "media_count": int((db.one(
            "SELECT COUNT(*) AS c FROM media WHERE library_id=? AND deleted_at IS NULL",
            (library_id,),
        ) or {}).get("c") or 0),
    }


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
    return {"ok": True}
