"""Storage dashboard, clutter cleanup and the side-by-side model upgrade flow."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import services
from ..schemas import MediaBatchBody, StorageCleanupBody, UpgradeCompareBody, UpgradeStartBody, UpgradeSwitchBody
from ..services.presenters import _require_csrf
from ..services.storage import CATEGORIES

router = APIRouter()


@router.get("/api/storage")
def storage_dashboard():
    return services().storage.dashboard()


@router.get("/api/storage/{category}")
def storage_candidates(category: str, limit: int = 500):
    if category not in CATEGORIES:
        raise HTTPException(404, "Unknown category")
    items = services().storage.candidates(category, max(1, min(limit, 2000)))
    return {"category": category, "items": items, "count": len(items), "bytes": sum(i["size"] for i in items)}


@router.post("/api/storage/estimate")
def storage_estimate(body: MediaBatchBody, request: Request):
    _require_csrf(request)
    return services().storage.estimate(body.media_ids)


@router.post("/api/storage/cleanup")
def storage_cleanup(body: StorageCleanupBody, request: Request):
    """Soft-delete the chosen items (undoable). With free_space their originals move to the bin."""
    _require_csrf(request)
    try:
        return services().storage.cleanup(body.media_ids, free_space=body.free_space)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/models/upgrade")
def upgrade_status():
    return services().storage.upgrade_status()


def _guard(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/models/upgrade")
def upgrade_start(body: UpgradeStartBody, request: Request):
    """Build the new model's index beside the current one (resumable; search keeps using the current model)."""
    _require_csrf(request)
    return _guard(lambda: services().storage.start_upgrade(body.to_key))


@router.post("/api/models/upgrade/compare")
def upgrade_compare(body: UpgradeCompareBody, request: Request):
    _require_csrf(request)
    return _guard(lambda: services().storage.compare(similar_media_id=body.similar_media_id, text=body.text, limit=body.limit))


@router.post("/api/models/upgrade/switch")
def upgrade_switch(request: Request, body: UpgradeSwitchBody | None = None):
    _require_csrf(request)
    return _guard(lambda: services().storage.switch(force=bool(body and body.force)))


@router.post("/api/models/upgrade/rollback")
def upgrade_rollback(request: Request):
    """Back to the previous model. Its index was never modified, and the new one is kept."""
    _require_csrf(request)
    return _guard(lambda: services().storage.rollback())


@router.post("/api/models/upgrade/finish")
def upgrade_finish(request: Request):
    _require_csrf(request)
    return services().storage.finish()
