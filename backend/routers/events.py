"""Events / moments: list, detail, edits with undo tokens, detection."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from ..deps import db, services
from ..jobs.manager import job_row
from ..schemas import EventDetectBody, EventMergeBody, EventMoveBody, EventRenameBody, EventSplitBody
from ..services.presenters import _display_name, _page, _require_csrf

router = APIRouter()


def _event_row(row: dict) -> dict:
    ids = json.loads(row.get("people") or "[]")
    people = db.all(f"SELECT id, name FROM people WHERE id IN ({','.join('?' * len(ids))})", tuple(ids)) if ids else []
    return {"id": row["id"], "name": row["name"], "start_at": row["start_at"], "end_at": row["end_at"],
            "cover_media_id": row["cover_media_id"], "lat": row["lat"], "lon": row["lon"],
            "item_count": row["item_count"], "user_edited": bool(row["user_edited"]),
            "people": [{"id": p["id"], "display_name": _display_name(p["name"], p["id"])} for p in people]}


@router.get("/api/events")
def list_events(page: int = 1, limit: int = 60, q: str = ""):
    page, limit = max(1, page), max(1, min(limit, 200))
    where, params = "item_count > 0", []
    if q.strip():
        where += " AND name LIKE ?"
        params.append(f"%{q.strip()}%")
    total = db.one(f"SELECT COUNT(*) c FROM events WHERE {where}", tuple(params))["c"]
    rows = db.all(f"SELECT * FROM events WHERE {where} ORDER BY start_at DESC, id DESC LIMIT ? OFFSET ?",
                  (*params, limit, (page - 1) * limit))
    return _page([_event_row(r) for r in rows], total, page, limit)


@router.get("/api/events/{event_id}")
def get_event(event_id: int):
    row = db.one("SELECT * FROM events WHERE id=?", (event_id,))
    if not row:
        raise HTTPException(404, "Event not found")
    return _event_row(row)


@router.post("/api/events/detect")
def detect_events(request: Request, body: EventDetectBody | None = None):
    _require_csrf(request)
    services().schedule_events(full=bool(body and body.full))
    return job_row(db.one("SELECT * FROM jobs WHERE kind='event_detection' ORDER BY id DESC LIMIT 1"))


@router.patch("/api/events/{event_id}")
def rename_event(event_id: int, body: EventRenameBody, request: Request):
    _require_csrf(request)
    if not db.one("SELECT id FROM events WHERE id=?", (event_id,)):
        raise HTTPException(404, "Event not found")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    token = services().events.rename(event_id, name)
    return {**get_event(event_id), "undo_token": token}


@router.post("/api/events/merge")
def merge_events(body: EventMergeBody, request: Request):
    _require_csrf(request)
    ids = list(dict.fromkeys(body.event_ids))
    if len(ids) < 2:
        raise HTTPException(400, "Pick at least two events")
    found = db.all(f"SELECT id FROM events WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    if len(found) != len(ids):
        raise HTTPException(404, "Event not found")
    target, token = services().events.merge(ids)
    return {**get_event(target), "undo_token": token}


@router.post("/api/events/{event_id}/split")
def split_event(event_id: int, body: EventSplitBody, request: Request):
    _require_csrf(request)
    try:
        new_id, token = services().events.split(event_id, body.media_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"event": get_event(event_id), "new_event": get_event(new_id), "undo_token": token}


@router.post("/api/events/{event_id}/move")
def move_to_event(event_id: int, body: EventMoveBody, request: Request):
    _require_csrf(request)
    if not db.one("SELECT id FROM events WHERE id=?", (event_id,)):
        raise HTTPException(404, "Event not found")
    if not body.media_ids:
        raise HTTPException(400, "media_ids required")
    token = services().events.move(list(dict.fromkeys(body.media_ids)), event_id)
    return {**get_event(event_id), "undo_token": token}


@router.post("/api/events/undo/{token}")
def undo_event_edit(token: int, request: Request):
    _require_csrf(request)
    try:
        return services().events.undo(token)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
