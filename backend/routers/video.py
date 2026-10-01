"""Video intelligence: tracks, keyframes, moments, person moments and clip export."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..deps import db, services
from ..jobs.manager import PRIORITY, job_row
from ..schemas import MomentSearchBody, PersonClipsBody
from ..services.presenters import _require_csrf

router = APIRouter()


def _video(media_id: int) -> dict:
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row or row["kind"] != "video":
        raise HTTPException(404, "Video not found")
    return row


@router.get("/api/media/{media_id}/video")
def video_detail(media_id: int):
    """Face tracks, per-person segments (for scrub-bar markers and timestamp chips) and keyframes."""
    _video(media_id)
    return services().video.detail(media_id)


@router.get("/api/keyframes/{keyframe_id}/image")
def keyframe_image(keyframe_id: int):
    path = services().video.keyframe_path(keyframe_id)
    if not path.is_file():
        raise HTTPException(404, "Keyframe not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/search/moments")
def search_moments(body: MomentSearchBody, request: Request):
    """Text -> video moments (SigLIP 2 over scene keyframes)."""
    _require_csrf(request)
    if not body.text.strip():
        raise HTTPException(400, "text required")
    return services().video.search_moments(body.text.strip(), limit=body.limit, people=body.people or None)


@router.get("/api/people/{person_id}/moments")
def person_moments(person_id: int):
    """Every video this person appears in, with timestamped segments."""
    if not db.one("SELECT id FROM people WHERE id=?", (person_id,)):
        raise HTTPException(404, "Person not found")
    items = services().video.person_moments(person_id)
    return {"items": items, "total": len(items)}


@router.get("/api/media/{media_id}/clip")
def clip(media_id: int, start: float = Query(..., ge=0), end: float = Query(..., gt=0), precise: bool = False):
    """Download [start, end] of a video. Stream copy unless ``precise``; the original is only read."""
    row = _video(media_id)
    if end <= start or end - start > 3600:
        raise HTTPException(400, "end must be after start (max 1 hour)")
    target = services().config.data_dir / "exports" / f"clip-{media_id}-{start:.2f}-{end:.2f}{'-p' if precise else ''}.mp4"
    try:
        services().video.clip(Path(row["path"]), start, end, target, precise=precise)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, str(exc)) from exc
    stem = re.sub(r"[^\w .-]+", "_", Path(row["name"]).stem)
    return FileResponse(target, media_type="video/mp4", filename=f"{stem} {start:.0f}-{end:.0f}s.mp4")


@router.post("/api/media/{media_id}/person-clips")
def person_clips(media_id: int, body: PersonClipsBody, request: Request):
    """Zip of every segment where the person appears in this video (+ manifest)."""
    _require_csrf(request)
    _video(media_id)
    try:
        target = services().video.person_clips_zip(media_id, body.person_id, precise=body.precise)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(target, media_type="application/zip", filename=target.name)


@router.post("/api/video/analyze")
def analyze_videos(request: Request, media_id: Optional[int] = None):
    _require_csrf(request)
    if media_id is not None:
        with db.connect() as conn:
            conn.execute("DELETE FROM video_analysis WHERE media_id=?", (media_id,))
    return job_row(services().jobs.enqueue("video_analysis", {}, priority=PRIORITY["normal"], dedupe_key="video_analysis"))
