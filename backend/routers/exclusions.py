"""Exclusions API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import cluster, db
from ..schemas import ExclusionBody
from ..services.presenters import _display_name, _require_csrf

router = APIRouter()

@router.get("/api/exclusions")
def list_exclusions():
    rows = db.all(
        """SELECT e.person_id, e.media_id, p.name, m.name AS media_name
           FROM exclusions e
           JOIN people p ON p.id = e.person_id
           JOIN media m ON m.id = e.media_id
           ORDER BY e.person_id, e.media_id"""
    )
    return {
        "items": [
            {
                "person_id": r["person_id"],
                "media_id": r["media_id"],
                "display_name": _display_name(r["name"], r["person_id"]),
                "name": r["media_name"],
            }
            for r in rows
        ]
    }


@router.post("/api/exclusions")
def add_exclusion(body: ExclusionBody, request: Request):
    _require_csrf(request)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO exclusions(person_id, media_id) VALUES (?,?)",
            (body.person_id, body.media_id),
        )
    cluster.invalidate()
    return {"ok": True}


@router.delete("/api/exclusions")
async def remove_exclusion(request: Request):
    _require_csrf(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    person_id = body.get("person_id")
    media_id = body.get("media_id")
    if person_id is None or media_id is None:
        raise HTTPException(400, "person_id and media_id required")
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM exclusions WHERE person_id=? AND media_id=?",
            (person_id, media_id),
        )
    cluster.invalidate()
    return {"ok": True}
