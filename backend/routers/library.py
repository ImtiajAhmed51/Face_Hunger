"""Albums, favorites, smart collections, people merge/split, memories and the audit log."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from ..deps import db, services
from ..schemas import AlbumBody, AlbumItemsBody, CollectionBody, FavoriteBody, MediaBatchBody, SplitBody
from ..services.library import UndoError
from ..services.presenters import _require_csrf

router = APIRouter()


def _lib():
    return services().library


@router.get("/api/albums")
def list_albums():
    return {"items": _lib().albums()}


@router.post("/api/albums")
def create_album(body: AlbumBody, request: Request):
    _require_csrf(request)
    if not body.name.strip():
        raise HTTPException(400, "name required")
    return _lib().create_album(body.name.strip(), body.media_ids)


@router.get("/api/albums/{album_id}")
def get_album(album_id: int):
    row = next((a for a in _lib().albums() if a["id"] == album_id), None)
    if row is None:
        raise HTTPException(404, "Album not found")
    return row


@router.patch("/api/albums/{album_id}")
def rename_album(album_id: int, body: AlbumBody, request: Request):
    _require_csrf(request)
    try:
        return _lib().rename_album(album_id, body.name.strip())
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/albums/{album_id}")
def delete_album(album_id: int, request: Request):
    _require_csrf(request)
    try:
        return _lib().delete_album(album_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/albums/{album_id}/items")
def add_album_items(album_id: int, body: AlbumItemsBody, request: Request):
    _require_csrf(request)
    try:
        return _lib().add_to_album(album_id, body.media_ids)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/albums/{album_id}/items/remove")
def remove_album_items(album_id: int, body: AlbumItemsBody, request: Request):
    _require_csrf(request)
    try:
        return _lib().remove_from_album(album_id, body.media_ids)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/favorites")
def set_favorites(body: FavoriteBody, request: Request):
    _require_csrf(request)
    return _lib().set_favorite(body.media_ids, body.favorite)


@router.post("/api/batch/delete")
def batch_delete(body: MediaBatchBody, request: Request):
    """Soft-delete many items in one audited, undoable action (originals stay on disk)."""
    _require_csrf(request)
    return _lib().soft_delete(body.media_ids, True)


@router.post("/api/batch/restore")
def batch_restore(body: MediaBatchBody, request: Request):
    _require_csrf(request)
    return _lib().soft_delete(body.media_ids, False)


@router.get("/api/collections")
def list_collections():
    return {"items": _lib().collections()}


@router.post("/api/collections")
def create_collection(body: CollectionBody, request: Request):
    """A smart collection is a saved hybrid search shown as an album that updates itself."""
    _require_csrf(request)
    if not body.name.strip():
        raise HTTPException(400, "name required")
    query = body.query.model_dump(exclude={"page", "limit"}, exclude_none=True)
    if not query.get("expansions"):
        query.pop("expansions", None)
    with db.connect() as conn:
        sid = conn.execute("INSERT INTO saved_searches(name, query, is_collection) VALUES (?,?,1)",
                           (body.name.strip(), json.dumps(query))).lastrowid
    return {"id": sid, "name": body.name.strip(), "query": query}


@router.get("/api/collections/{collection_id}/items")
def collection_items(collection_id: int, page: int = 1, limit: int = 60):
    try:
        return _lib().collection_items(collection_id, max(1, page), max(1, min(limit, 200)))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: int, request: Request):
    _require_csrf(request)
    with db.connect() as conn:
        if not conn.execute("DELETE FROM saved_searches WHERE id=? AND is_collection=1", (collection_id,)).rowcount:
            raise HTTPException(404, "Collection not found")
    return {"ok": True}


@router.get("/api/people/{person_id}/merge-preview")
def merge_preview(person_id: int, target_id: int):
    if not db.one("SELECT id FROM people WHERE id=?", (person_id,)) or not db.one("SELECT id FROM people WHERE id=?", (target_id,)):
        raise HTTPException(404, "Person not found")
    return _lib().merge_preview(person_id, target_id)


@router.post("/api/people/{person_id}/split")
def split_person(person_id: int, body: SplitBody, request: Request):
    _require_csrf(request)
    try:
        return _lib().split_person(person_id, body.face_ids, body.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/memories")
def memories():
    return {"sections": _lib().memories()}


@router.get("/api/audit")
def audit_log(limit: int = 100, before: int | None = None):
    return {"items": _lib().audit(limit=max(1, min(limit, 500)), before=before)}


@router.post("/api/audit/{entry_id}/undo")
def undo(entry_id: int, request: Request):
    _require_csrf(request)
    try:
        return _lib().undo(entry_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except UndoError as exc:
        raise HTTPException(409, str(exc)) from exc
