"""Non-destructive edits: crop, rotate/flip, rating, labels, flags, history and revert."""

from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException, Query, Request, Response

from .. import edits as model
from ..deps import db, services
from ..schemas import EditBatchBody, EditPatchBody, EditRevertBody
from ..services.presenters import _require_csrf

router = APIRouter()


def _exists(media_id: int) -> None:
    if not db.one("SELECT id FROM media WHERE id=?", (media_id,)):
        raise HTTPException(404, "Media not found")


@router.get("/api/media/{media_id}/edits")
def get_edits(media_id: int):
    _exists(media_id)
    return services().edits.detail(media_id)


@router.patch("/api/media/{media_id}/edits")
def patch_edits(media_id: int, body: EditPatchBody, request: Request):
    """Change only the fields sent. The original file is never modified."""
    _require_csrf(request)
    _exists(media_id)
    try:
        return services().edits.update(media_id, body.model_dump(exclude_unset=True))
    except model.EditError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/media/{media_id}/edits/revert")
def revert_edits(media_id: int, request: Request, body: EditRevertBody | None = None):
    """Without a body: back to the original view. With history_id: undo that step."""
    _require_csrf(request)
    _exists(media_id)
    try:
        return services().edits.revert(media_id, body.history_id if body else None)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/edits/batch")
def batch_edits(body: EditBatchBody, request: Request):
    _require_csrf(request)
    patch = body.model_dump(exclude_unset=True, exclude={"media_ids"})
    try:
        return services().edits.batch(body.media_ids, patch)
    except model.EditError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/media/{media_id}/rendered")
def rendered(media_id: int, max_side: int = Query(2560, ge=64, le=12000), download: bool = False):
    """The photo with its edits applied, as a new JPEG (metadata-free). Used by exports."""
    try:
        image = services().edits.rendered(media_id, max_side=max_side)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=92)
    headers = {"Cache-Control": "private, no-cache"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="media-{media_id}-edited.jpg"'
    return Response(buf.getvalue(), media_type="image/jpeg", headers=headers)
