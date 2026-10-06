"""Optional local VLM: status, load/unload, query rewriting, album generation, captions.

Nothing here imports the model code; that only happens inside the assistant service once
the user has enabled the VLM in Settings.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import db, services
from ..jobs.manager import PRIORITY, job_row
from ..schemas import AlbumGenerateBody, CaptionBody, RewriteBody
from ..services.presenters import _require_csrf

router = APIRouter()


@router.get("/api/vlm")
def vlm_status():
    return services().assistant.status()


@router.post("/api/vlm/load")
def vlm_load(request: Request):
    _require_csrf(request)
    assistant = services().assistant
    model = assistant.model()
    if model is None:
        raise HTTPException(409, "Enable the local VLM in Settings and install it first" if not assistant.enabled or not assistant.installed else "Unavailable")
    try:
        model.load()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return assistant.status()


@router.post("/api/vlm/unload")
def vlm_unload(request: Request):
    _require_csrf(request)
    services().assistant.unload()
    return services().assistant.status()


@router.post("/api/search/rewrite")
def rewrite(body: RewriteBody, request: Request):
    """Filters from the rule-based parser, plus visual expansions when the VLM is enabled.
    The response always lists the resulting filters so the user can edit them."""
    _require_csrf(request)
    return services().assistant.rewrite(body.query, use_model=body.use_model)


@router.post("/api/albums/generate")
def generate_album(body: AlbumGenerateBody, request: Request):
    """"Best moments with my family last summer" -> an ordinary, editable album (works without the VLM)."""
    _require_csrf(request)
    if not body.prompt.strip():
        raise HTTPException(400, "prompt required")
    return job_row(services().jobs.enqueue("album_generate", body.model_dump(), priority=PRIORITY["normal"]))


@router.post("/api/vlm/caption")
def caption(body: CaptionBody, request: Request):
    _require_csrf(request)
    if services().assistant.model() is None:
        raise HTTPException(409, "Enable the local VLM in Settings (and install it) to generate captions")
    return job_row(services().jobs.enqueue("caption_backfill", body.model_dump(), priority=PRIORITY["background"]))


@router.get("/api/media/{media_id}/caption")
def media_caption(media_id: int):
    import json

    row = db.one("SELECT * FROM media_captions WHERE media_id=?", (media_id,))
    if not row:
        return {"caption": None, "tags": [], "model": None}
    return {"caption": row["caption"], "tags": json.loads(row["tags"]), "model": row["model"]}
