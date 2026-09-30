"""Quality signals and best-shot scores."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import scoring
from ..deps import db, services
from ..jobs.manager import PRIORITY, job_row
from ..services.presenters import _require_csrf

router = APIRouter()


@router.get("/api/quality")
def quality_overview():
    """The best-shot formula (weights per signal) and how much of the library is scored."""
    counts = db.one(
        """SELECT (SELECT COUNT(*) FROM media WHERE deleted_at IS NULL AND status IN ('indexed','stale')) AS media,
                  (SELECT COUNT(*) FROM quality_signals) AS signals,
                  (SELECT COUNT(*) FROM quality_scores WHERE formula_version=?) AS scored""",
        (scoring.FORMULA_VERSION,))
    return {"signals_version": scoring.SIGNALS_VERSION, "formula_version": scoring.FORMULA_VERSION,
            "weights": scoring.WEIGHTS, "coverage": counts,
            "aesthetic_head": services().quality.head() is not None}


@router.post("/api/quality/run")
def run_quality(request: Request):
    _require_csrf(request)
    return job_row(services().jobs.enqueue("quality_scoring", {}, priority=PRIORITY["normal"], dedupe_key="quality_scoring"))


@router.get("/api/media/{media_id}/quality")
def media_quality(media_id: int):
    if not db.one("SELECT id FROM media WHERE id=?", (media_id,)):
        raise HTTPException(404, "Media not found")
    return services().quality.for_media(media_id)
