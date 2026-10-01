"""People API routes."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..deps import cluster, config, db, services
from ..schemas import MergeBody, MoveMediaBody, NameBody
from ..services.exporting import _stream_export
from ..services.presenters import _now, _page, _person_row, _require_csrf

router = APIRouter()

@router.get("/api/people")
def list_people(
    q: str = "",
    page: int = 1,
    limit: int = 48,
    sort: str = "faces",
):
    """sort: faces (default) | photos | videos | name"""
    page = max(1, page)
    limit = max(1, min(limit, 200))
    offset = (page - 1) * limit
    sort_key = (sort or "faces").strip().lower()
    # Subqueries count distinct media of each kind that still have active faces for the person.
    photo_order = """(
        SELECT COUNT(DISTINCT m.id) FROM faces f
        JOIN media m ON m.id = f.media_id
        WHERE f.person_id = people.id AND f.deleted_at IS NULL
          AND m.deleted_at IS NULL AND m.missing = 0 AND m.kind = 'photo'
    ) DESC"""
    video_order = """(
        SELECT COUNT(DISTINCT m.id) FROM faces f
        JOIN media m ON m.id = f.media_id
        WHERE f.person_id = people.id AND f.deleted_at IS NULL
          AND m.deleted_at IS NULL AND m.missing = 0 AND m.kind = 'video'
    ) DESC"""
    if sort_key == "photos":
        order_sql = f"{photo_order}, people.face_count DESC, people.id"
    elif sort_key == "videos":
        order_sql = f"{video_order}, people.face_count DESC, people.id"
    elif sort_key == "name":
        order_sql = "COALESCE(people.name, '') COLLATE NOCASE ASC, people.id"
    else:
        # faces (default)
        order_sql = "people.face_count DESC, people.id"

    params: list[Any] = []
    base_where = "people.face_count > 0"
    if q.strip():
        like = f"%{q.strip()}%"
        base_where += " AND (people.name LIKE ? OR CAST(people.id AS TEXT) LIKE ?)"
        params.extend([like, like])

    total = int((db.one(
        f"SELECT COUNT(*) AS c FROM people WHERE {base_where}",
        tuple(params),
    ) or {}).get("c") or 0)
    rows = db.all(
        f"""SELECT people.* FROM people WHERE {base_where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    return _page([_person_row(r) for r in rows], total, page, limit)


@router.get("/api/people/{person_id}")
def get_person(person_id: int):
    row = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    return _person_row(row)


@router.get("/api/clusters")
def list_clusters(q: str = "", page: int = 1, limit: int = 36, samples: int = 12):
    """People as visual clusters: each item includes sample face ids for thumbnails."""
    page = max(1, page)
    limit = max(1, min(limit, 100))
    samples = max(1, min(samples, 24))
    offset = (page - 1) * limit
    base_where = "face_count > 0"
    params: list[Any] = []
    if q.strip():
        like = f"%{q.strip()}%"
        base_where += " AND (name LIKE ? OR CAST(id AS TEXT) LIKE ?)"
        params.extend([like, like])
    total = int((db.one(f"SELECT COUNT(*) AS c FROM people WHERE {base_where}", tuple(params)) or {}).get("c") or 0)
    people = db.all(
        f"""SELECT * FROM people WHERE {base_where}
            ORDER BY face_count DESC, id
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    items = []
    for row in people:
        person = _person_row(row)
        face_rows = db.all(
            """SELECT f.id FROM faces f
               JOIN media m ON m.id = f.media_id
               WHERE f.person_id = ? AND f.deleted_at IS NULL
                 AND m.deleted_at IS NULL AND m.missing = 0
               ORDER BY CASE f.review_state WHEN 'confirmed' THEN 0 ELSE 1 END,
                        f.quality DESC, f.detection DESC, f.id
               LIMIT ?""",
            (row["id"], samples),
        )
        person["sample_face_ids"] = [r["id"] for r in face_rows]
        items.append(person)
    return _page(items, total, page, limit)


@router.patch("/api/people/{person_id}")
def rename_person(person_id: int, body: NameBody, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    name = body.name.strip() or None
    with db.connect() as conn:
        conn.execute("UPDATE people SET name=? WHERE id=?", (name, person_id))
    cluster.invalidate()
    return _person_row(db.one("SELECT * FROM people WHERE id=?", (person_id,)))



@router.post("/api/people/{person_id}/merge")
def merge_person(person_id: int, body: MergeBody, request: Request):
    _require_csrf(request)
    if person_id == body.target_id:
        raise HTTPException(400, "Cannot merge a person into itself")
    try:
        result = services().library.merge_people(person_id, body.target_id)
    except KeyError as exc:
        raise HTTPException(404, "Person not found") from exc
    return {**_person_row(db.one("SELECT * FROM people WHERE id=?", (body.target_id,))), "audit_id": result["audit_id"]}


@router.delete("/api/people/{person_id}")
def soft_delete_person(person_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "UPDATE faces SET deleted_at=? WHERE person_id=? AND deleted_at IS NULL",
            (now, person_id),
        )
        cluster.refresh(conn, [person_id])
    cluster.invalidate()
    return {"ok": True}


@router.post("/api/people/{person_id}/restore")
def restore_person(person_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    with db.connect() as conn:
        conn.execute("UPDATE faces SET deleted_at=NULL WHERE person_id=?", (person_id,))
        cluster.refresh(conn, [person_id])
    cluster.invalidate()
    return _person_row(db.one("SELECT * FROM people WHERE id=?", (person_id,)))


@router.post("/api/people/{person_id}/export")
def export_person(person_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    media_ids = [
        r["id"]
        for r in db.all(
            """SELECT DISTINCT m.id FROM media m
               JOIN faces f ON f.media_id = m.id
               WHERE f.person_id=? AND f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0""",
            (person_id,),
        )
    ]
    return _stream_export(media_ids, f"person-{person_id}.zip")


@router.post("/api/people/{person_id}/move-media")
def move_person_media(person_id: int, body: MoveMediaBody, request: Request):
    """Physically move original media files for a person to a user folder,
    then remove those media (and their faces) from the index/DB.
    Clustering for any remaining faces of this person is left intact.
    """
    _require_csrf(request)
    person = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not person:
        raise HTTPException(404, "Person not found")

    dest_root = Path(body.destination).expanduser().resolve()
    # Safety: destination must be under an allowed root
    allowed = config.roots
    if not any(dest_root == r or dest_root.is_relative_to(r) for r in allowed):
        raise HTTPException(
            400,
            f"Destination must be under one of the allowed roots: {[str(r) for r in allowed]}",
        )
    dest_root.mkdir(parents=True, exist_ok=True)

    if body.media_ids:
        media_rows = db.all(
            f"""SELECT DISTINCT m.* FROM media m
                JOIN faces f ON f.media_id = m.id
                WHERE f.person_id=? AND f.deleted_at IS NULL AND m.deleted_at IS NULL
                  AND m.missing=0 AND m.id IN ({','.join('?' * len(body.media_ids))})""",
            (person_id, *body.media_ids),
        )
    else:
        media_rows = db.all(
            """SELECT DISTINCT m.* FROM media m
               JOIN faces f ON f.media_id = m.id
               WHERE f.person_id=? AND f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0""",
            (person_id,),
        )

    if not media_rows:
        return {"moved": 0, "failed": [], "destination": str(dest_root)}

    moved = 0
    failed: list[dict] = []
    moved_ids: list[int] = []

    for row in media_rows:
        src = Path(row["path"])
        if not src.is_file():
            failed.append({"id": row["id"], "path": row["path"], "error": "File not found"})
            continue
        # Keep basename; if collision, add numeric suffix
        target = dest_root / src.name
        if target.exists():
            stem, suffix = src.stem, src.suffix
            n = 1
            while target.exists():
                target = dest_root / f"{stem}_{n}{suffix}"
                n += 1
        try:
            shutil.move(str(src), str(target))
            moved += 1
            moved_ids.append(row["id"])
        except OSError as exc:
            failed.append({"id": row["id"], "path": row["path"], "error": str(exc)})

    # Hard-remove moved media from index (faces cascade). Person clustering
    # for any remaining faces is preserved; face_count is refreshed below.
    if moved_ids:
        with db.connect() as conn:
            placeholders = ",".join("?" * len(moved_ids))
            conn.execute(f"DELETE FROM media WHERE id IN ({placeholders})", tuple(moved_ids))
            # Refresh this person's face_count / representative
            remaining = conn.execute(
                """SELECT COUNT(*) AS c FROM faces
                   WHERE person_id=? AND deleted_at IS NULL""",
                (person_id,),
            ).fetchone()
            face_count = int(remaining["c"] if remaining else 0)
            if face_count == 0:
                conn.execute(
                    "UPDATE people SET face_count=0, representative_face_id=NULL WHERE id=?",
                    (person_id,),
                )
            else:
                rep = conn.execute(
                    """SELECT f.id FROM faces f JOIN media m ON m.id=f.media_id
                       WHERE f.person_id=? AND f.deleted_at IS NULL AND m.deleted_at IS NULL
                       ORDER BY CASE m.kind WHEN 'photo' THEN 0 ELSE 1 END,
                                COALESCE(f.quality,0) DESC, f.id LIMIT 1""",
                    (person_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE people SET face_count=?, representative_face_id=? WHERE id=?",
                    (face_count, rep["id"] if rep else None, person_id),
                )
        try:
            cluster.invalidate()
        except Exception:
            pass

    return {
        "moved": moved,
        "failed": failed,
        "destination": str(dest_root),
        "removed_from_index": len(moved_ids),
    }
