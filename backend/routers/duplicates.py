"""Duplicates API routes."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .. import dino as dino_mod
from .. import dino_duplicates as dino_dup_mod
from ..deps import db, media_store
from ..schemas import BackfillBody, IgnoreDuplicateBody
from ..services.presenters import _media_row, _require_csrf, _settings

router = APIRouter()

@router.get("/api/duplicates")
def list_duplicates(
    limit: int = Query(2000, ge=1, le=10000),
    threshold: Optional[float] = Query(None, ge=0.5, le=0.999),
):
    """Exact (content_hash) + near-duplicate (DINOv2 cosine) media groups."""
    settings = _settings()
    sim = float(threshold if threshold is not None else settings.get("dino_similarity_threshold", 0.92))
    groups = dino_dup_mod.find_duplicate_groups(
        db,
        media_store,
        similarity_threshold=sim,
        limit=limit,
        media_row_fn=_media_row,
    )
    dino_total = int((db.one(
        "SELECT COUNT(*) AS c FROM media WHERE deleted_at IS NULL AND missing=0 AND status IN ('indexed','stale')"
    ) or {}).get("c") or 0)
    dino_filled = int((db.one(
        "SELECT COUNT(*) AS c FROM media WHERE deleted_at IS NULL AND missing=0 AND status IN ('indexed','stale') AND dino_offset IS NOT NULL AND dino_sha IS NOT NULL"
    ) or {}).get("c") or 0)
    return {
        "groups": groups,
        "threshold": sim,
        "dino": dino_mod.status(),
        "embedding_total": dino_total,
        "embedding_filled": dino_filled,
        "embedding_remaining": max(0, dino_total - dino_filled),
        "group_count": len(groups),
        "item_count": sum(len(g["items"]) for g in groups),
    }


@router.post("/api/duplicates/backfill")
def backfill_dino_embeddings(request: Request, body: BackfillBody = BackfillBody()):
    """Incrementally compute DINOv2 embeddings for media missing them."""
    _require_csrf(request)
    from .media_processing import frame_at

    limit = max(1, min(int(body.limit or 40), 200))
    result = dino_dup_mod.backfill_embeddings(
        db, media_store, frame_at, limit=limit
    )
    return result

@router.post("/api/duplicates/ignore")
def ignore_duplicate_group(body: IgnoreDuplicateBody, request: Request):
    """Mark a media set as not-a-duplicate. Does not delete or soft-delete files."""
    _require_csrf(request)
    ids = sorted({int(x) for x in (body.media_ids or []) if int(x) > 0})
    if len(ids) < 2:
        raise HTTPException(400, "media_ids must contain at least two media ids")
    key = dino_dup_mod.group_fingerprint(ids)
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO ignored_duplicate_groups (group_key, media_ids)
               VALUES (?, ?)""",
            (key, json.dumps(ids)),
        )
    return {"ok": True, "group_key": key}


@router.delete("/api/duplicates/ignore")
def unignore_duplicate_group(body: IgnoreDuplicateBody, request: Request):
    """Clear a not-a-duplicate ignore so the group can appear in results again."""
    _require_csrf(request)
    ids = sorted({int(x) for x in (body.media_ids or []) if int(x) > 0})
    if len(ids) < 2:
        raise HTTPException(400, "media_ids must contain at least two media ids")
    key = dino_dup_mod.group_fingerprint(ids)
    with db.connect() as conn:
        conn.execute("DELETE FROM ignored_duplicate_groups WHERE group_key=?", (key,))
    return {"ok": True, "group_key": key}
