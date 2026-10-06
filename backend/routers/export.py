"""Export API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..deps import db
from ..schemas import ExportBody
from ..services.exporting import _stream_export
from ..services.presenters import _require_csrf

router = APIRouter()

@router.post("/api/export")
def export_media(body: ExportBody, request: Request):
    _require_csrf(request)
    media_ids = body.media_ids or []
    if not media_ids and body.filters:
        # simple filter support: reuse list_media logic via DB
        kind = body.filters.get("kind")
        where = ["deleted_at IS NULL", "missing = 0"]
        params: list[Any] = []
        if kind in ("photo", "video"):
            where.append("kind = ?")
            params.append(kind)
        rows = db.all(
            f"SELECT id FROM media WHERE {' AND '.join(where)} ORDER BY id LIMIT 5000",
            tuple(params),
        )
        media_ids = [r["id"] for r in rows]
    return _stream_export(media_ids, "face-hunger-export.zip", apply_edits=body.apply_edits)
