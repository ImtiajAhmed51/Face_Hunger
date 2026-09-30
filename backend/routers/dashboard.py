"""Dashboard API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import db, engine, worker
from ..services.presenters import (
    _cleanup_counts,
    _job_row,
    _media_row,
    _person_row,
    _require_csrf,
)

router = APIRouter()

@router.get("/api/dashboard")
def dashboard():
    photos = int((db.one("SELECT COUNT(*) AS c FROM media WHERE kind='photo' AND deleted_at IS NULL") or {}).get("c") or 0)
    videos = int((db.one("SELECT COUNT(*) AS c FROM media WHERE kind='video' AND deleted_at IS NULL") or {}).get("c") or 0)
    faces = int((db.one(
        "SELECT COUNT(*) AS c FROM faces f JOIN media m ON m.id=f.media_id WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL"
    ) or {}).get("c") or 0)
    people = int((db.one("SELECT COUNT(*) AS c FROM people WHERE face_count > 0") or {}).get("c") or 0)
    # One item per person (or track) per media — matches /api/review dedupe
    review_count = int((db.one(
        """SELECT COUNT(*) AS c FROM (
             SELECT 1 FROM faces f JOIN media m ON m.id=f.media_id
             WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
               AND f.review_state='unreviewed'
             GROUP BY f.media_id,
               CASE
                 WHEN f.person_id IS NOT NULL THEN 'p:' || f.person_id
                 WHEN f.track_id IS NOT NULL THEN 't:' || f.track_id
                 ELSE 'f:' || f.id
               END
           )"""
    ) or {}).get("c") or 0)

    recent_people_rows = db.all(
        "SELECT * FROM people WHERE face_count > 0 ORDER BY id DESC LIMIT 8"
    )
    recent_media_rows = db.all(
        "SELECT * FROM media WHERE deleted_at IS NULL ORDER BY COALESCE(captured_at, indexed_at) DESC, id DESC LIMIT 12"
    )
    return {
        "photos": photos,
        "videos": videos,
        "faces": faces,
        "people": people,
        "review_count": review_count,
        "cleanup": _cleanup_counts(),
        "recent_people": [_person_row(r) for r in recent_people_rows],
        "recent_media": [_media_row(r) for r in recent_media_rows],
        "job": _job_row(worker.latest()),
        "engine": engine.status(),
    }


@router.get("/api/engine")
def get_engine():
    return engine.status()


@router.post("/api/engine/load")
def load_engine(request: Request):
    _require_csrf(request)
    try:
        return engine.load()
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
