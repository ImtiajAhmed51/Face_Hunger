"""Review API routes."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ..deps import cluster, db
from ..services.presenters import (
    _face_row,
    _page,
    _require_csrf,
    _review_threshold,
    _settings,
)

router = APIRouter()

@router.get("/api/review")
def list_review(
    page: int = 1,
    limit: int = 30,
    person_id: Optional[int] = None,
    deleted: bool = False,
):
    """Review queue: at most one face per person per media (best quality wins).

    Same person appearing many times in one video is shown once.
    Uses a single pass with window + LIMIT so we never materialize the full
    unreviewed set into memory on every page request.
    """
    page = max(1, page)
    limit = max(1, min(limit, 100))
    offset = (page - 1) * limit
    where = ["f.review_state = 'unreviewed'", "m.missing = 0"]
    params: list[Any] = []
    if deleted:
        where.append("f.deleted_at IS NOT NULL")
    else:
        where.append("f.deleted_at IS NULL")
        where.append("m.deleted_at IS NULL")
    if person_id is not None:
        where.append("f.person_id = ?")
        params.append(person_id)
    where_sql = " AND ".join(where)
    # Deduplicate: one representative face per (media, person) or (media, track)
    # or the face itself when neither is set.
    # Partition key is precomputed as an expression that indexes can help filter.
    dedupe_inner = f"""
          SELECT f.*,
            ROW_NUMBER() OVER (
              PARTITION BY f.media_id,
                CASE
                  WHEN f.person_id IS NOT NULL THEN 'p:' || f.person_id
                  WHEN f.track_id IS NOT NULL THEN 't:' || f.track_id
                  ELSE 'f:' || f.id
                END
              ORDER BY COALESCE(f.quality, 0) DESC, f.detection DESC, f.id
            ) AS _rn
          FROM faces f
          JOIN media m ON m.id = f.media_id
          WHERE {where_sql}
    """
    # Count only the deduped rows without pulling full face payloads twice.
    # SQLite evaluates the window once; we avoid SELECT * in the count path.
    total = int((db.one(
        f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM ({dedupe_inner}) ranked WHERE _rn = 1)",
        tuple(params),
    ) or {}).get("c") or 0)
    rows = db.all(
        f"""SELECT * FROM ({dedupe_inner}) ranked
            WHERE _rn = 1
            ORDER BY similarity IS NULL, similarity ASC, id
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    rt = _review_threshold()
    return _page([_face_row(r, rt) for r in rows], total, page, limit)
@router.post("/api/review/dedupe-video")
def dedupe_video_review(request: Request):
    """Collapse review backlog: same person in one media keeps one face unreviewed.

    For every (media_id, person_id) with multiple unreviewed faces, the highest-
    quality face stays in the queue; the rest are marked confirmed.
    Also collapses unassigned tracklet siblings the same way.
    """
    _require_csrf(request)
    with db.connect() as conn:
        cur1 = conn.execute(
            """
            UPDATE faces SET review_state='confirmed'
            WHERE deleted_at IS NULL AND review_state='unreviewed'
              AND person_id IS NOT NULL AND manual=0
              AND id NOT IN (
                SELECT id FROM (
                  SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY media_id, person_id
                    ORDER BY COALESCE(quality, 0) DESC, detection DESC, id
                  ) AS rn
                  FROM faces
                  WHERE deleted_at IS NULL AND review_state='unreviewed'
                    AND person_id IS NOT NULL AND manual=0
                ) ranked WHERE rn = 1
              )
            """
        )
        n_person = cur1.rowcount
        cur2 = conn.execute(
            """
            UPDATE faces SET review_state='confirmed'
            WHERE deleted_at IS NULL AND review_state='unreviewed'
              AND track_id IS NOT NULL AND person_id IS NULL AND manual=0
              AND id NOT IN (
                SELECT id FROM (
                  SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY media_id, track_id
                    ORDER BY COALESCE(quality, 0) DESC, detection DESC, id
                  ) AS rn
                  FROM faces
                  WHERE deleted_at IS NULL AND review_state='unreviewed'
                    AND track_id IS NOT NULL AND person_id IS NULL AND manual=0
                ) ranked WHERE rn = 1
              )
            """
        )
        n_track = cur2.rowcount
    cluster.invalidate()
    remaining = int(
        (db.one(
            """SELECT COUNT(*) AS c FROM faces f JOIN media m ON m.id=f.media_id
               WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
                 AND f.review_state='unreviewed'"""
        ) or {}).get("c") or 0
    )
    return {
        "collapsed_same_person": n_person,
        "collapsed_same_track": n_track,
        "remaining_unreviewed": remaining,
    }


@router.post("/api/review/auto-confirm")
def auto_confirm_high_confidence(
    request: Request,
    threshold: Optional[float] = Query(None, description="Override auto_confirm_threshold"),
    aggressive: bool = Query(False, description="Also confirm mid-confidence (uses matching_threshold)"),
):
    """Mark existing unreviewed faces as confirmed when similarity is high enough.

    Default threshold = settings.auto_confirm_threshold (0.52).
    Pass ?aggressive=1 to use matching_threshold so almost all matched faces leave the queue.
    New-person faces (similarity NULL) are never auto-confirmed.
    """
    _require_csrf(request)
    settings = _settings()
    if threshold is not None:
        if not (0.3 <= threshold <= 0.95):
            raise HTTPException(400, "threshold must be between 0.3 and 0.95")
        rt = float(threshold)
    elif aggressive:
        rt = float(settings.get("matching_threshold", 0.48))
    else:
        rt = float(
            settings.get("auto_confirm_threshold")
            or settings.get("review_threshold")
            or 0.52
        )
    min_q = max(0.2, float(settings.get("min_face_quality", 0.35)) * 0.85)
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE faces SET review_state='confirmed'
            WHERE deleted_at IS NULL
              AND review_state='unreviewed'
              AND manual=0
              AND similarity IS NOT NULL
              AND similarity >= ?
              AND COALESCE(quality, 0.5) >= ?
              AND person_id IS NOT NULL
            """,
            (rt, min_q),
        )
        updated = cur.rowcount
        pids = [
            r[0]
            for r in conn.execute(
                """SELECT DISTINCT person_id FROM faces
                   WHERE review_state='confirmed' AND person_id IS NOT NULL
                   AND deleted_at IS NULL"""
            )
        ]
        if pids:
            for i in range(0, len(pids), 128):
                cluster.refresh(conn, pids[i : i + 128])
    cluster.invalidate()
    remaining = int(
        (db.one(
            """SELECT COUNT(*) AS c FROM faces f JOIN media m ON m.id=f.media_id
               WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
                 AND f.review_state='unreviewed'"""
        ) or {}).get("c") or 0
    )
    # Breakdown of what is still in the queue
    breakdown = db.one(
        """SELECT
             SUM(CASE WHEN f.similarity IS NULL THEN 1 ELSE 0 END) AS new_people,
             SUM(CASE WHEN f.similarity IS NOT NULL THEN 1 ELSE 0 END) AS low_confidence,
             SUM(CASE WHEN COALESCE(f.quality, 0.5) < 0.35 THEN 1 ELSE 0 END) AS low_quality
           FROM faces f JOIN media m ON m.id=f.media_id
           WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
             AND f.review_state='unreviewed'"""
    ) or {}
    return {
        "confirmed": updated,
        "threshold_used": rt,
        "min_quality": min_q,
        "remaining_unreviewed": remaining,
        "remaining_new_people": int(breakdown.get("new_people") or 0),
        "remaining_low_confidence": int(breakdown.get("low_confidence") or 0),
        "remaining_low_quality": int(breakdown.get("low_quality") or 0),
    }
