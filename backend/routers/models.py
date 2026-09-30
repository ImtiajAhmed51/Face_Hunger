"""Model runtime and embedding-store status."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import engine, models, services, vectors
from ..services.presenters import _require_csrf

router = APIRouter()


@router.get("/api/models")
def model_status():
    """Installed ONNX models, the execution provider each one loaded on, and the face engine."""
    return {**models.status(), "face_engine": engine.status()}


@router.post("/api/models/unload")
def unload_models(request: Request):
    _require_csrf(request)
    for model in (models.siglip, models.dino):
        for slot in model.slots():
            slot.unload()
    return model_status()


@router.get("/api/embeddings")
def embedding_status():
    """Every registered embedding space with its coverage of the library."""
    return {"items": vectors.status(),
            "backfill_models": [s.key for s in services().embedding_spaces_for_backfill()]}


@router.post("/api/embeddings/{key}/rebuild-index")
def rebuild_index(key: str, request: Request):
    _require_csrf(request)
    try:
        space = vectors.get(key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return space.sync(force_rebuild=True)
