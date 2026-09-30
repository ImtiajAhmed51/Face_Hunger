"""Duplicates API routes."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .. import dino as dino_mod
from .. import dino_duplicates as dino_dup_mod
from ..deps import db, models, services
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
    space = services().visual_space()
    groups = dino_dup_mod.find_duplicate_groups(
        db,
        space,
        similarity_threshold=sim,
        limit=limit,
        media_row_fn=_media_row,
    )
    # Coverage of the model new vectors go into (falls back to the space in use).
    target = services().vectors.register(models.dino.spec) if models.dino.installed else space
    coverage = target.coverage() if target is not None else {"filled": 0, "total": 0}
    dino_total, dino_filled = coverage["total"], coverage["filled"]
    return {
        "groups": groups,
        "threshold": sim,
        "dino": dino_mod.status(),
        "embedding_total": dino_total,
        "embedding_filled": dino_filled,
        "embedding_remaining": max(0, dino_total - dino_filled),
        "embedding_model": space.key if space is not None else None,
        "group_count": len(groups),
        "item_count": sum(len(g["items"]) for g in groups),
    }


@router.post("/api/duplicates/backfill")
def backfill_dino_embeddings(request: Request, body: BackfillBody = BackfillBody()):
    """Incrementally compute DINOv2 embeddings for media missing them."""
    _require_csrf(request)
    limit = max(1, min(int(body.limit or 40), 200))
    embedder = models.embedder("visual")
    space = services().vectors.register(embedder.spec) if embedder else None
    result = dino_dup_mod.backfill_embeddings(db, space, embedder, limit=limit)
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
