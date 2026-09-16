"""Face Hunger — local face search server entry point.

Serves the React frontend from frontend/dist and exposes the /api contract.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .clustering import Clustering
from .config import Config
from .db import Database
from .embeddings import EmbeddingStore
from .engine import Engine
from .media_processing import load_image
from .scanner import authorized_root, resolve_inside
from .worker import Worker
from . import threshold_tuning
from . import evaluation as evaluation_mod


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

config = Config()
config.prepare()

db = Database(config.data_dir / "index.sqlite")
store = EmbeddingStore(config.data_dir / "embeddings.bin")
engine = Engine(config)
cluster = Clustering(db, store)
worker = Worker(db, config, engine, store, cluster)

app = FastAPI(title="Face Hunger", version="1.0.0", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _require_csrf(request: Request) -> None:
    if request.headers.get("X-LFS-Request") != "1":
        raise HTTPException(403, "Missing X-LFS-Request header")


def _display_name(name: Optional[str], person_id: Optional[int]) -> str:
    if name and str(name).strip():
        return str(name).strip()
    if person_id is None:
        return "Unknown"
    return f"Person {person_id}"


def _confidence_label(similarity: Optional[float], review_threshold: float) -> str:
    if similarity is None:
        return "unknown"
    if similarity >= review_threshold:
        return "high"
    if similarity >= review_threshold - 0.12:
        return "medium"
    return "low"


def _person_row(row: dict, review_threshold: Optional[float] = None) -> dict:
    pid = row["id"]
    name = row.get("name")
    stats = db.one(
        """
        SELECT
          COUNT(*) AS face_count,
          COUNT(DISTINCT CASE WHEN m.kind='photo' THEN m.id END) AS photo_count,
          COUNT(DISTINCT CASE WHEN m.kind='video' THEN m.id END) AS video_count,
          SUM(CASE WHEN f.review_state='unreviewed' THEN 1 ELSE 0 END) AS unreviewed_count
        FROM faces f
        JOIN media m ON m.id = f.media_id
        WHERE f.person_id = ? AND f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing = 0
        """,
        (pid,),
    ) or {}
    rep = row.get("representative_face_id")
    # Fallback: pick any active face if representative is missing/stale
    if rep is None and int(stats.get("face_count") or 0) > 0:
        fallback = db.one(
            """
            SELECT f.id FROM faces f
            JOIN media m ON m.id = f.media_id
            WHERE f.person_id = ? AND f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing = 0
            ORDER BY CASE f.review_state WHEN 'confirmed' THEN 0 ELSE 1 END, f.detection DESC, f.id
            LIMIT 1
            """,
            (pid,),
        )
        if fallback:
            rep = fallback["id"]
    return {
        "id": pid,
        "name": name,
        "display_name": _display_name(name, pid),
        "face_count": int(stats.get("face_count") or 0),
        "photo_count": int(stats.get("photo_count") or 0),
        "video_count": int(stats.get("video_count") or 0),
        "representative_face_id": rep,
        "unreviewed_count": int(stats.get("unreviewed_count") or 0),
    }


def _media_people(media_id: int) -> list[dict]:
    rows = db.all(
        """
        SELECT DISTINCT p.id, p.name
        FROM faces f
        JOIN people p ON p.id = f.person_id
        WHERE f.media_id = ? AND f.deleted_at IS NULL AND f.person_id IS NOT NULL
          AND f.review_state != 'rejected'
          AND NOT EXISTS (
            SELECT 1 FROM exclusions e WHERE e.media_id = f.media_id AND e.person_id = f.person_id
          )
        ORDER BY p.id
        """,
        (media_id,),
    )
    return [{"id": r["id"], "display_name": _display_name(r["name"], r["id"])} for r in rows]


def _media_row(row: dict) -> dict:
    face_count = db.one(
        "SELECT COUNT(*) AS c FROM faces WHERE media_id=? AND deleted_at IS NULL",
        (row["id"],),
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "captured_at": row.get("captured_at"),
        "width": row.get("width"),
        "height": row.get("height"),
        "duration": row.get("duration"),
        "size": int(row.get("size") or 0),
        "status": row["status"],
        "missing": bool(row.get("missing")),
        "deleted_at": row.get("deleted_at"),
        "face_count": int((face_count or {}).get("c") or 0),
        "people": _media_people(row["id"]),
    }


def _face_row(row: dict, review_threshold: float, media_id: Optional[int] = None) -> dict:
    mid = media_id or row["media_id"]
    person_id = row.get("person_id")
    person = db.one("SELECT name FROM people WHERE id=?", (person_id,)) if person_id else None
    excluded = False
    if person_id is not None:
        excluded = db.one(
            "SELECT 1 AS x FROM exclusions WHERE person_id=? AND media_id=?",
            (person_id, mid),
        ) is not None
    bbox = row["bbox"]
    if isinstance(bbox, str):
        bbox = json.loads(bbox)
    sim = row.get("similarity")
    return {
        "id": row["id"],
        "media_id": mid,
        "person_id": person_id,
        "display_name": _display_name(person["name"] if person else None, person_id),
        "bbox": bbox,
        "timestamp": row.get("timestamp"),
        "detection": row["detection"],
        "similarity": sim,
        "confidence_label": _confidence_label(sim, review_threshold),
        "review_state": row.get("review_state") or "unreviewed",
        "deleted_at": row.get("deleted_at"),
        "excluded": excluded,
    }


def _job_row(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {
        "id": row["id"],
        "library_id": row.get("library_id"),
        "status": row["status"],
        "phase": row.get("phase") or row["status"],
        "total": int(row.get("total") or 0),
        "processed": int(row.get("processed") or 0),
        "faces": int(row.get("faces") or 0),
        "people": int(row.get("people") or 0),
        "skipped": int(row.get("skipped") or 0),
        "failed": int(row.get("failed") or 0),
        "current_file": row.get("current_file"),
        "error": row.get("error"),
    }


def _settings() -> dict:
    return db.settings()


def _review_threshold() -> float:
    return float(_settings().get("review_threshold", 0.62))


def _cleanup_counts() -> dict:
    rt = _review_threshold()
    return {
        "duplicates": int((db.one("SELECT COUNT(*) AS c FROM media WHERE duplicate_count>0 AND deleted_at IS NULL") or {}).get("c") or 0),
        "low_confidence": int((db.one(
            """SELECT COUNT(*) AS c FROM faces f JOIN media m ON m.id=f.media_id
               WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
                 AND f.review_state='unreviewed' AND f.similarity IS NOT NULL AND f.similarity < ?""",
            (rt,),
        ) or {}).get("c") or 0),
        "unreviewed": int((db.one(
            """SELECT COUNT(*) AS c FROM faces f JOIN media m ON m.id=f.media_id
               WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
                 AND f.review_state='unreviewed'""",
        ) or {}).get("c") or 0),
        "failed": int((db.one("SELECT COUNT(*) AS c FROM media WHERE status='failed' AND deleted_at IS NULL") or {}).get("c") or 0),
        "missing": int((db.one("SELECT COUNT(*) AS c FROM media WHERE missing=1 AND deleted_at IS NULL") or {}).get("c") or 0),
        "deleted_faces": int((db.one("SELECT COUNT(*) AS c FROM faces WHERE deleted_at IS NOT NULL") or {}).get("c") or 0),
    }


def _page(items: list, total: int, page: int, limit: int) -> dict:
    return {"items": items, "total": total, "page": page, "limit": limit}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class NameBody(BaseModel):
    name: str


class MergeBody(BaseModel):
    target_id: int


class MoveFacesBody(BaseModel):
    face_ids: list[int]
    target_id: Optional[int] = None
    name: Optional[str] = None


class ReviewBody(BaseModel):
    decision: str  # yes | no


class ConfirmBody(BaseModel):
    confirm: str


class ExclusionBody(BaseModel):
    person_id: int
    media_id: int


class ParseSearchBody(BaseModel):
    query: str


class SeparateBody(BaseModel):
    person_a: int
    person_b: int


class LibraryBody(BaseModel):
    path: str
    ignored: Optional[list[str]] = None


class LibraryPatchBody(BaseModel):
    ignored: list[str]


class IndexBody(BaseModel):
    library_id: int
    force: bool = False
    retry_failed: bool = False


class SettingsPatch(BaseModel):
    matching_threshold: Optional[float] = None
    review_threshold: Optional[float] = None
    detection_size: Optional[int] = None
    video_interval: Optional[float] = None
    theme: Optional[str] = None
    multi_scale: Optional[bool] = None
    adaptive_video: Optional[bool] = None
    min_face_quality: Optional[float] = None
    auto_confirm: Optional[bool] = None
    auto_confirm_threshold: Optional[float] = None


class MaintenanceBody(BaseModel):
    action: str
    confirm: str


class ExportBody(BaseModel):
    media_ids: Optional[list[int]] = None
    filters: Optional[dict] = None


class MoveMediaBody(BaseModel):
    destination: str
    media_ids: Optional[list[int]] = None  # if set, only these; else all media with this person's faces


# ---------------------------------------------------------------------------
# Dashboard / Engine
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard():
    photos = int((db.one("SELECT COUNT(*) AS c FROM media WHERE kind='photo' AND deleted_at IS NULL") or {}).get("c") or 0)
    videos = int((db.one("SELECT COUNT(*) AS c FROM media WHERE kind='video' AND deleted_at IS NULL") or {}).get("c") or 0)
    faces = int((db.one(
        "SELECT COUNT(*) AS c FROM faces f JOIN media m ON m.id=f.media_id WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL"
    ) or {}).get("c") or 0)
    people = int((db.one("SELECT COUNT(*) AS c FROM people WHERE face_count > 0") or {}).get("c") or 0)
    # One item per person (or track) per media — matches /api/review dedupe
    review_count = int((db.one(
        """SELECT COUNT(*) AS c FROM (
             SELECT 1 FROM faces f JOIN media m ON m.id=f.media_id
             WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
               AND f.review_state='unreviewed'
             GROUP BY f.media_id,
               CASE
                 WHEN f.person_id IS NOT NULL THEN 'p:' || f.person_id
                 WHEN f.track_id IS NOT NULL THEN 't:' || f.track_id
                 ELSE 'f:' || f.id
               END
           )"""
    ) or {}).get("c") or 0)

    recent_people_rows = db.all(
        "SELECT * FROM people WHERE face_count > 0 ORDER BY id DESC LIMIT 8"
    )
    recent_media_rows = db.all(
        "SELECT * FROM media WHERE deleted_at IS NULL ORDER BY COALESCE(captured_at, indexed_at) DESC, id DESC LIMIT 12"
    )
    return {
        "photos": photos,
        "videos": videos,
        "faces": faces,
        "people": people,
        "review_count": review_count,
        "cleanup": _cleanup_counts(),
        "recent_people": [_person_row(r) for r in recent_people_rows],
        "recent_media": [_media_row(r) for r in recent_media_rows],
        "job": _job_row(worker.latest()),
        "engine": engine.status(),
    }


@app.get("/api/engine")
def get_engine():
    return engine.status()


@app.post("/api/engine/load")
def load_engine(request: Request):
    _require_csrf(request)
    try:
        return engine.load()
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

@app.get("/api/people")
def list_people(q: str = "", page: int = 1, limit: int = 48, sort: str = "faces"):
    """sort: faces (default) | photos | videos | name — all descending except name (A–Z)."""
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

    base_where = "people.face_count > 0"
    params: list[Any] = []
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


@app.get("/api/people/{person_id}")
def get_person(person_id: int):
    row = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    if not row:
        raise HTTPException(404, "Person not found")
    return _person_row(row)


@app.get("/api/clusters")
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


@app.patch("/api/people/{person_id}")
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


@app.post("/api/people/{person_id}/merge")
def merge_person(person_id: int, body: MergeBody, request: Request):
    _require_csrf(request)
    if person_id == body.target_id:
        raise HTTPException(400, "Cannot merge a person into itself")
    source = db.one("SELECT * FROM people WHERE id=?", (person_id,))
    target = db.one("SELECT * FROM people WHERE id=?", (body.target_id,))
    if not source or not target:
        raise HTTPException(404, "Person not found")
    with db.connect() as conn:
        conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (body.target_id, person_id))
        conn.execute("UPDATE OR IGNORE exclusions SET person_id=? WHERE person_id=?", (body.target_id, person_id))
        conn.execute("DELETE FROM exclusions WHERE person_id=?", (person_id,))
        conn.execute("UPDATE OR IGNORE rejections SET person_id=? WHERE person_id=?", (body.target_id, person_id))
        conn.execute("DELETE FROM rejections WHERE person_id=?", (person_id,))
        # transfer name if target has none
        if not (target.get("name") or "").strip() and (source.get("name") or "").strip():
            conn.execute("UPDATE people SET name=? WHERE id=?", (source["name"], body.target_id))
        conn.execute("DELETE FROM people WHERE id=?", (person_id,))
        cluster.refresh(conn, [body.target_id])
    cluster.invalidate()
    return _person_row(db.one("SELECT * FROM people WHERE id=?", (body.target_id,)))


@app.delete("/api/people/{person_id}")
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


@app.post("/api/people/{person_id}/restore")
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


@app.post("/api/people/{person_id}/export")
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


@app.post("/api/people/{person_id}/move-media")
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
                    """SELECT id FROM faces WHERE person_id=? AND deleted_at IS NULL
                       ORDER BY quality DESC, id LIMIT 1""",
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


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------

@app.post("/api/faces/move")
def move_faces(body: MoveFacesBody, request: Request):
    _require_csrf(request)
    if not body.face_ids:
        raise HTTPException(400, "face_ids required")
    faces = db.all(
        f"SELECT * FROM faces WHERE id IN ({','.join('?' * len(body.face_ids))})",
        tuple(body.face_ids),
    )
    if len(faces) != len(set(body.face_ids)):
        raise HTTPException(404, "One or more faces not found")

    with db.connect() as conn:
        if body.target_id is not None:
            target = conn.execute("SELECT id FROM people WHERE id=?", (body.target_id,)).fetchone()
            if not target:
                raise HTTPException(404, "Target person not found")
            person_id = body.target_id
        else:
            name = (body.name or "").strip() or None
            person_id = conn.execute(
                "INSERT INTO people(name) VALUES (?)", (name,)
            ).lastrowid

        conn.execute(
            f"UPDATE faces SET person_id=?, manual=1, review_state='confirmed' WHERE id IN ({','.join('?' * len(body.face_ids))})",
            (person_id, *body.face_ids),
        )
        # collect affected people for refresh
        old_ids = {f["person_id"] for f in faces if f.get("person_id")}
        cluster.refresh(conn, list(old_ids | {person_id}))
    cluster.invalidate()
    return {"person_id": person_id}


@app.post("/api/faces/{face_id}/review")
def review_face(face_id: int, body: ReviewBody, request: Request):
    _require_csrf(request)
    face = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not face:
        raise HTTPException(404, "Face not found")
    decision = body.decision.lower()
    if decision not in ("yes", "no"):
        raise HTTPException(400, "decision must be 'yes' or 'no'")
    with db.connect() as conn:
        if decision == "yes":
            conn.execute(
                "UPDATE faces SET review_state='confirmed', manual=1 WHERE id=?",
                (face_id,),
            )
        else:
            conn.execute(
                "UPDATE faces SET review_state='rejected', manual=1 WHERE id=?",
                (face_id,),
            )
            if face.get("person_id"):
                conn.execute(
                    "INSERT OR IGNORE INTO rejections(face_id, person_id) VALUES (?,?)",
                    (face_id, face["person_id"]),
                )
                # Hard-negative set: future matching against this person is penalised
                conn.execute(
                    "INSERT OR IGNORE INTO hard_negatives(face_id, person_id, similarity) VALUES (?,?,?)",
                    (face_id, face["person_id"], face.get("similarity")),
                )
        if face.get("person_id"):
            # Lightweight count/rep update only — full centroid recompute is deferred
            # so Yes/No stays snappy during rapid review.
            pid = face["person_id"]
            if decision == "yes":
                # Confirmed face can become representative if quality is high
                row = conn.execute(
                    """SELECT id FROM faces WHERE person_id=? AND deleted_at IS NULL
                       ORDER BY CASE review_state WHEN 'confirmed' THEN 0 ELSE 1 END,
                                quality DESC, detection DESC, id LIMIT 1""",
                    (pid,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE people SET representative_face_id=? WHERE id=?",
                        (row["id"], pid),
                    )
            # Keep face_count accurate without scanning embeddings
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM faces WHERE person_id=? AND deleted_at IS NULL",
                (pid,),
            ).fetchone()
            conn.execute(
                "UPDATE people SET face_count=? WHERE id=?",
                (int(cnt["c"] if cnt else 0), pid),
            )
    # Soft invalidation only — do not rebuild all centroids on every click
    cluster.invalidate()
    # Autotune is expensive (full labeled scan). Run every ~25 reviews, not each click.
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='review_autotune_counter'").fetchone()
            n = int(json.loads(row[0])) if row else 0
            n += 1
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('review_autotune_counter',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(n),),
            )
        if n % 25 == 0:
            threshold_tuning.autotune(db, cluster)
    except Exception:
        pass
    return {"ok": True}


@app.delete("/api/faces/{face_id}")
def soft_delete_face(face_id: int, request: Request):
    _require_csrf(request)
    face = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not face:
        raise HTTPException(404, "Face not found")
    with db.connect() as conn:
        conn.execute("UPDATE faces SET deleted_at=? WHERE id=?", (_now(), face_id))
        if face.get("person_id"):
            cluster.refresh(conn, [face["person_id"]])
    cluster.invalidate()
    return {"ok": True}


@app.post("/api/faces/{face_id}/restore")
def restore_face(face_id: int, request: Request):
    _require_csrf(request)
    face = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not face:
        raise HTTPException(404, "Face not found")
    with db.connect() as conn:
        conn.execute("UPDATE faces SET deleted_at=NULL WHERE id=?", (face_id,))
        if face.get("person_id"):
            cluster.refresh(conn, [face["person_id"]])
    cluster.invalidate()
    return {"ok": True}


@app.delete("/api/faces/{face_id}/permanent")
def permanent_delete_face(face_id: int, body: ConfirmBody, request: Request):
    _require_csrf(request)
    if body.confirm != "DELETE FACE":
        raise HTTPException(400, "confirm must be 'DELETE FACE'")
    face = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not face:
        raise HTTPException(404, "Face not found")
    with db.connect() as conn:
        conn.execute("DELETE FROM faces WHERE id=?", (face_id,))
        if face.get("person_id"):
            cluster.refresh(conn, [face["person_id"]])
    cluster.invalidate()
    return {"ok": True}


@app.get("/api/faces/{face_id}/thumbnail")
def face_thumbnail(face_id: int):
    face = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not face:
        raise HTTPException(404, "Face not found")

    # Try stored thumbnail path (absolute or relative), plus common fallbacks
    candidates: list[Path] = []
    thumb = face.get("thumbnail")
    if thumb:
        p = Path(thumb)
        candidates.append(p)
        if not p.is_absolute():
            candidates.append(config.data_dir / "thumbnails" / thumb)
        candidates.append(config.data_dir / "thumbnails" / p.name)
    candidates.append(config.data_dir / "thumbnails" / f"face-{face_id}.jpg")

    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return FileResponse(path, media_type="image/jpeg")
        except OSError:
            continue

    # fallback: crop from original media
    media = db.one("SELECT * FROM media WHERE id=?", (face["media_id"],))
    if not media or not Path(media["path"]).is_file():
        raise HTTPException(404, "Thumbnail not available")
    try:
        bbox = face["bbox"]
        if isinstance(bbox, str):
            bbox = json.loads(bbox)
        x, y, w, h = [float(v) for v in bbox]
        # Pad crop a bit so the face is not tightly clipped
        pad = max(w, h) * 0.15
        bgr = load_image(media["path"])
        h_img, w_img = bgr.shape[:2]
        x1 = max(0, int(x - pad))
        y1 = max(0, int(y - pad))
        x2 = min(w_img, int(x + w + pad))
        y2 = min(h_img, int(y + h + pad))
        if x2 <= x1 or y2 <= y1:
            raise ValueError("invalid bbox")
        crop = bgr[y1:y2, x1:x2]
        from PIL import Image
        import numpy as np

        rgb = crop[:, :, ::-1]
        img = Image.fromarray(np.ascontiguousarray(rgb))
        img.thumbnail((256, 256))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        # Cache regenerated thumbnail for next time
        try:
            out = config.data_dir / "thumbnails" / f"face-{face_id}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(buf.getvalue())
            with db.connect() as conn:
                conn.execute("UPDATE faces SET thumbnail=? WHERE id=?", (str(out), face_id))
            buf.seek(0)
        except Exception:
            buf.seek(0)
        return Response(content=buf.read(), media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(404, f"Could not generate thumbnail: {exc}") from exc


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@app.get("/api/media")
def list_media(
    kind: Optional[str] = None,
    people: Optional[str] = None,
    exclude_people: Optional[str] = None,
    mode: str = "ANY",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    confidence: Optional[float] = None,
    reviewed: Optional[str] = None,
    excluded: bool = False,
    deleted: bool = False,
    no_faces: bool = False,
    sort: str = "date",
    page: int = 1,
    limit: int = 60,
    q: str = "",
):
    """sort: date (default, newest first) | size_desc | size_asc | name
    people: include media that contain these people
    exclude_people: hide media that contain any of these people
    """
    page = max(1, page)
    limit = max(1, min(limit, 200))
    offset = (page - 1) * limit

    where = ["1=1"]
    params: list[Any] = []

    if not deleted:
        where.append("m.deleted_at IS NULL")
    else:
        where.append("m.deleted_at IS NOT NULL")

    if kind in ("photo", "video"):
        where.append("m.kind = ?")
        params.append(kind)

    if date_from:
        where.append("m.captured_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("m.captured_at <= ?")
        params.append(date_to + "T23:59:59" if len(date_to) == 10 else date_to)

    if q.strip():
        where.append("m.name LIKE ?")
        params.append(f"%{q.strip()}%")

    if no_faces:
        # Media with zero non-deleted faces
        where.append(
            """NOT EXISTS (
                SELECT 1 FROM faces f
                WHERE f.media_id = m.id AND f.deleted_at IS NULL
            )"""
        )

    people_ids: list[int] = []
    if people:
        try:
            people_ids = [int(x) for x in people.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "Invalid people filter")

    if people_ids:
        placeholders = ",".join("?" * len(people_ids))
        if mode.upper() == "ALL":
            where.append(
                f"""(
                SELECT COUNT(DISTINCT f.person_id) FROM faces f
                WHERE f.media_id = m.id AND f.deleted_at IS NULL AND f.person_id IN ({placeholders})
                  AND f.review_state != 'rejected'
                  {"AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.media_id=f.media_id AND e.person_id=f.person_id)" if not excluded else ""}
              ) = ?"""
            )
            params.extend(people_ids)
            params.append(len(people_ids))
        else:
            where.append(
                f"""EXISTS (
                SELECT 1 FROM faces f WHERE f.media_id = m.id AND f.deleted_at IS NULL
                  AND f.person_id IN ({placeholders}) AND f.review_state != 'rejected'
                  {"AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.media_id=f.media_id AND e.person_id=f.person_id)" if not excluded else ""}
              )"""
            )
            params.extend(people_ids)

    exclude_ids: list[int] = []
    if exclude_people:
        try:
            exclude_ids = [int(x) for x in exclude_people.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "Invalid exclude_people filter")
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        # Hide any media that still has a non-rejected face of these people
        where.append(
            f"""NOT EXISTS (
                SELECT 1 FROM faces f WHERE f.media_id = m.id AND f.deleted_at IS NULL
                  AND f.person_id IN ({placeholders}) AND f.review_state != 'rejected'
            )"""
        )
        params.extend(exclude_ids)

    if reviewed == "confirmed":
        where.append(
            """EXISTS (SELECT 1 FROM faces f WHERE f.media_id=m.id AND f.deleted_at IS NULL AND f.review_state='confirmed')"""
        )
    elif reviewed == "unreviewed":
        where.append(
            """EXISTS (SELECT 1 FROM faces f WHERE f.media_id=m.id AND f.deleted_at IS NULL AND f.review_state='unreviewed')"""
        )

    if confidence is not None:
        where.append(
            """EXISTS (SELECT 1 FROM faces f WHERE f.media_id=m.id AND f.deleted_at IS NULL
               AND f.similarity IS NOT NULL AND f.similarity >= ?)"""
        )
        params.append(confidence)

    sort_key = (sort or "date").strip().lower()
    if sort_key == "size_desc":
        order_sql = "m.size DESC, m.id DESC"
    elif sort_key == "size_asc":
        order_sql = "m.size ASC, m.id ASC"
    elif sort_key == "name":
        order_sql = "m.name COLLATE NOCASE ASC, m.id ASC"
    else:
        order_sql = "COALESCE(m.captured_at, m.indexed_at) DESC, m.id DESC"

    where_sql = " AND ".join(where)
    total = int((db.one(f"SELECT COUNT(*) AS c FROM media m WHERE {where_sql}", tuple(params)) or {}).get("c") or 0)
    rows = db.all(
        f"""SELECT m.* FROM media m WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    return _page([_media_row(r) for r in rows], total, page, limit)


@app.get("/api/media/{media_id}")
def get_media(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    rt = _review_threshold()
    faces = db.all(
        "SELECT * FROM faces WHERE media_id=? ORDER BY COALESCE(timestamp, 0), id",
        (media_id,),
    )
    detail = _media_row(row)
    detail["faces"] = [_face_row(f, rt, media_id) for f in faces]
    return detail


@app.get("/api/media/{media_id}/thumbnail")
def media_thumbnail(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    thumb = row.get("thumbnail")
    if thumb:
        path = Path(thumb)
        if not path.is_absolute():
            path = config.data_dir / "thumbnails" / thumb
        if path.is_file():
            return FileResponse(path, media_type="image/jpeg")
    # fallback: serve a small preview of the original for photos
    if row["kind"] == "photo" and Path(row["path"]).is_file():
        try:
            from PIL import Image
            import numpy as np

            bgr = load_image(row["path"])
            rgb = bgr[:, :, ::-1]
            img = Image.fromarray(np.ascontiguousarray(rgb))
            img.thumbnail((480, 480))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            buf.seek(0)
            return Response(content=buf.read(), media_type="image/jpeg")
        except Exception:
            pass
    raise HTTPException(404, "Thumbnail not available")


@app.get("/api/media/{media_id}/preview")
def media_preview(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    if row["kind"] != "photo":
        raise HTTPException(400, "Preview only available for photos")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(404, "File missing")
    try:
        from PIL import Image
        import numpy as np

        bgr = load_image(path)
        rgb = bgr[:, :, ::-1]
        img = Image.fromarray(np.ascontiguousarray(rgb))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        return Response(content=buf.read(), media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(500, f"Could not generate preview: {exc}") from exc


@app.get("/api/media/{media_id}/file")
def media_file(media_id: int, request: Request):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(404, "File missing on disk")
    media_type = "video/mp4" if row["kind"] == "video" else "image/jpeg"
    # FileResponse handles Range for videos
    return FileResponse(path, media_type=media_type, filename=row["name"])


@app.delete("/api/media/{media_id}")
def soft_delete_media(media_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    with db.connect() as conn:
        conn.execute("UPDATE media SET deleted_at=? WHERE id=?", (_now(), media_id))
    cluster.invalidate()
    return {"ok": True}


class PurgeMediaBody(BaseModel):
    media_ids: list[int]
    confirm: str  # must be "DELETE"


@app.post("/api/media/purge")
def purge_media(body: PurgeMediaBody, request: Request):
    """Permanently delete original files from disk and remove them from the index.
    Irreversible. Requires confirm='DELETE'.
    """
    _require_csrf(request)
    if body.confirm != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm permanent deletion of original files.")
    if not body.media_ids:
        raise HTTPException(400, "media_ids required")

    ids = list(dict.fromkeys(body.media_ids))  # unique, preserve order
    rows = db.all(
        f"SELECT * FROM media WHERE id IN ({','.join('?' * len(ids))})",
        tuple(ids),
    )
    by_id = {r["id"]: r for r in rows}
    deleted_files = 0
    removed_from_db = 0
    failed: list[dict] = []

    for mid in ids:
        row = by_id.get(mid)
        if not row:
            failed.append({"id": mid, "error": "Not found in index"})
            continue
        path = Path(row["path"])
        # Only delete files that still exist under an allowed root
        try:
            resolved = path.expanduser().resolve()
            allowed = any(resolved == r or resolved.is_relative_to(r) for r in config.roots)
            if path.is_file():
                if not allowed:
                    failed.append({"id": mid, "path": row["path"], "error": "Path outside allowed roots"})
                    continue
                path.unlink()
                deleted_files += 1
            # else: already missing on disk — still purge from DB
        except OSError as exc:
            failed.append({"id": mid, "path": row["path"], "error": str(exc)})
            continue

        with db.connect() as conn:
            conn.execute("DELETE FROM media WHERE id=?", (mid,))
        removed_from_db += 1

    try:
        cluster.invalidate()
    except Exception:
        pass

    return {
        "deleted_files": deleted_files,
        "removed_from_index": removed_from_db,
        "failed": failed,
    }


@app.post("/api/media/{media_id}/restore")
def restore_media(media_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    with db.connect() as conn:
        conn.execute("UPDATE media SET deleted_at=NULL WHERE id=?", (media_id,))
    cluster.invalidate()
    return _media_row(db.one("SELECT * FROM media WHERE id=?", (media_id,)))


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

@app.get("/api/review")
def list_review(
    page: int = 1,
    limit: int = 30,
    person_id: Optional[int] = None,
    deleted: bool = False,
):
    """Review queue: at most one face per person per media (best quality wins).

    Same person appearing many times in one video is shown once.
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
    dedupe_sql = f"""
        SELECT * FROM (
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
        ) ranked
        WHERE _rn = 1
    """
    total = int((db.one(
        f"SELECT COUNT(*) AS c FROM ({dedupe_sql})",
        tuple(params),
    ) or {}).get("c") or 0)
    rows = db.all(
        f"""SELECT * FROM ({dedupe_sql})
            ORDER BY similarity IS NULL, similarity ASC, id
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    rt = _review_threshold()
    return _page([_face_row(r, rt) for r in rows], total, page, limit)


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

@app.get("/api/exclusions")
def list_exclusions():
    rows = db.all(
        """SELECT e.person_id, e.media_id, p.name, m.name AS media_name
           FROM exclusions e
           JOIN people p ON p.id = e.person_id
           JOIN media m ON m.id = e.media_id
           ORDER BY e.person_id, e.media_id"""
    )
    return {
        "items": [
            {
                "person_id": r["person_id"],
                "media_id": r["media_id"],
                "display_name": _display_name(r["name"], r["person_id"]),
                "name": r["media_name"],
            }
            for r in rows
        ]
    }


@app.post("/api/exclusions")
def add_exclusion(body: ExclusionBody, request: Request):
    _require_csrf(request)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO exclusions(person_id, media_id) VALUES (?,?)",
            (body.person_id, body.media_id),
        )
    cluster.invalidate()
    return {"ok": True}


@app.delete("/api/exclusions")
async def remove_exclusion(request: Request):
    _require_csrf(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    person_id = body.get("person_id")
    media_id = body.get("media_id")
    if person_id is None or media_id is None:
        raise HTTPException(400, "person_id and media_id required")
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM exclusions WHERE person_id=? AND media_id=?",
            (person_id, media_id),
        )
    cluster.invalidate()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Search parse
# ---------------------------------------------------------------------------

@app.post("/api/search/parse")
def parse_search(body: ParseSearchBody, request: Request):
    _require_csrf(request)
    tokens = body.query.strip().split()
    people: list[int] = []
    unmatched: list[str] = []
    mode = "ANY"
    date_from = date_to = kind = None

    for token in tokens:
        lower = token.lower()
        if lower in ("and", "all"):
            mode = "ALL"
            continue
        if lower in ("or", "any"):
            mode = "ANY"
            continue
        if lower in ("photo", "photos"):
            kind = "photo"
            continue
        if lower in ("video", "videos"):
            kind = "video"
            continue
        # try match person by name
        row = db.one(
            "SELECT id FROM people WHERE lower(name)=? AND face_count>0 LIMIT 1",
            (lower,),
        )
        if row:
            people.append(row["id"])
            continue
        # try partial
        rows = db.all(
            "SELECT id FROM people WHERE face_count>0 AND lower(name) LIKE ? ORDER BY face_count DESC LIMIT 3",
            (f"%{lower}%",),
        )
        if len(rows) == 1:
            people.append(rows[0]["id"])
        else:
            unmatched.append(token)

    return {
        "people": people,
        "mode": mode,
        "date_from": date_from,
        "date_to": date_to,
        "kind": kind,
        "unmatched": unmatched,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@app.get("/api/cleanup")
def get_cleanup():
    counts = _cleanup_counts()
    failed_media = [_media_row(r) for r in db.all(
        "SELECT * FROM media WHERE status='failed' AND deleted_at IS NULL ORDER BY id DESC LIMIT 50"
    )]
    missing_media = [_media_row(r) for r in db.all(
        "SELECT * FROM media WHERE missing=1 AND deleted_at IS NULL ORDER BY id DESC LIMIT 50"
    )]
    # possible people pairs by centroid cosine similarity
    # Prefer strong matches; pairs marked separate never reappear.
    possible: list[dict] = []
    try:
        people_rows = db.all(
            """SELECT id, name, centroid, face_count, representative_face_id
               FROM people WHERE face_count > 0 AND centroid IS NOT NULL
               ORDER BY face_count DESC LIMIT 400"""
        )
        import numpy as np
        from .embeddings import normalize

        vectors = []
        ids = []
        for r in people_rows:
            try:
                raw = r["centroid"]
                if raw is None:
                    continue
                v = normalize(np.frombuffer(bytes(raw), dtype="<f4"))
                vectors.append(v)
                ids.append(r)
            except Exception:
                continue

        separated = set()
        for row in db.all("SELECT person_a, person_b FROM separate_people"):
            separated.add((row["person_a"], row["person_b"]))

        if len(vectors) >= 2:
            matrix = np.stack(vectors)
            match_th = float(_settings().get("matching_threshold", 0.48))
            # Strong suggestions only — avoids noisy near-miss pairs
            threshold = max(0.42, match_th)
            scored: list[tuple[float, int, int]] = []
            n = len(ids)
            for i in range(n):
                sims = matrix[i] @ matrix[i + 1 :].T
                for offset, sim in enumerate(sims):
                    j = i + 1 + offset
                    a_id, b_id = ids[i]["id"], ids[j]["id"]
                    pair = (min(a_id, b_id), max(a_id, b_id))
                    if pair in separated:
                        continue
                    sim_f = float(sim)
                    if sim_f < threshold:
                        continue
                    # Drop weak singleton noise unless similarity is very high
                    fa = int(ids[i].get("face_count") or 0)
                    fb = int(ids[j].get("face_count") or 0)
                    if min(fa, fb) < 2 and sim_f < match_th + 0.08:
                        continue
                    scored.append((sim_f, i, j))
            scored.sort(reverse=True)
            for sim, i, j in scored[:40]:
                possible.append({
                    "a": _person_row(ids[i]),
                    "b": _person_row(ids[j]),
                    "similarity": sim,
                })
    except Exception:
        pass

    # Frontend "Duplicate candidates" count uses `duplicates` — show pair count there
    counts["duplicates"] = len(possible)

    return {
        **counts,
        "possible_people": possible,
        "failed_media": failed_media,
        "missing_media": missing_media,
        "embedding_errors": None,
    }


@app.post("/api/cleanup/check")
def cleanup_check(request: Request):
    _require_csrf(request)
    updated = 0
    with db.connect() as conn:
        for row in conn.execute("SELECT id, path FROM media WHERE deleted_at IS NULL"):
            missing = 0 if Path(row["path"]).is_file() else 1
            conn.execute("UPDATE media SET missing=? WHERE id=?", (missing, row["id"]))
            if missing:
                updated += 1
    cluster.invalidate()
    return {"checked": True, "missing_updated": updated}


@app.post("/api/cleanup/separate")
def cleanup_separate(body: SeparateBody, request: Request):
    _require_csrf(request)
    a, b = sorted((body.person_a, body.person_b))
    if a == b:
        raise HTTPException(400, "Cannot separate a person from itself")
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO separate_people(person_a, person_b) VALUES (?,?)",
            (a, b),
        )
    cluster.invalidate()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------

@app.get("/api/libraries")
def list_libraries():
    rows = db.all("SELECT * FROM libraries ORDER BY id")
    items = []
    for r in rows:
        count = int((db.one(
            "SELECT COUNT(*) AS c FROM media WHERE library_id=? AND deleted_at IS NULL",
            (r["id"],),
        ) or {}).get("c") or 0)
        ignored = r.get("ignored") or "[]"
        if isinstance(ignored, str):
            ignored = json.loads(ignored)
        items.append({
            "id": r["id"],
            "name": r["name"],
            "path": r["path"],
            "ignored": ignored,
            "media_count": count,
        })
    return {"items": items}


@app.post("/api/libraries")
def add_library(body: LibraryBody, request: Request):
    _require_csrf(request)
    path = Path(body.path).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(400, f"Path is not a directory: {path}")
    try:
        authorized_root(str(path), config.roots)
    except Exception as exc:
        raise HTTPException(403, str(exc)) from exc
    name = path.name or str(path)
    ignored = body.ignored or []
    try:
        with db.connect() as conn:
            lid = conn.execute(
                "INSERT INTO libraries(path, name, ignored) VALUES (?,?,?)",
                (str(path), name, json.dumps(ignored)),
            ).lastrowid
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(400, "Library path already registered") from exc
        raise
    return {
        "id": lid,
        "name": name,
        "path": str(path),
        "ignored": ignored,
        "media_count": 0,
    }


@app.patch("/api/libraries/{library_id}")
def patch_library(library_id: int, body: LibraryPatchBody, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not row:
        raise HTTPException(404, "Library not found")
    with db.connect() as conn:
        conn.execute(
            "UPDATE libraries SET ignored=? WHERE id=?",
            (json.dumps(body.ignored), library_id),
        )
    return {
        "id": library_id,
        "name": row["name"],
        "path": row["path"],
        "ignored": body.ignored,
        "media_count": int((db.one(
            "SELECT COUNT(*) AS c FROM media WHERE library_id=? AND deleted_at IS NULL",
            (library_id,),
        ) or {}).get("c") or 0),
    }


@app.delete("/api/libraries/{library_id}")
def delete_library(library_id: int, body: ConfirmBody, request: Request):
    _require_csrf(request)
    if body.confirm != "REMOVE LIBRARY":
        raise HTTPException(400, "confirm must be 'REMOVE LIBRARY'")
    row = db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not row:
        raise HTTPException(404, "Library not found")
    with db.connect() as conn:
        # cascade deletes media/faces via FK
        conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    cluster.invalidate()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Index / Jobs
# ---------------------------------------------------------------------------

@app.post("/api/index")
def start_index(body: IndexBody, request: Request):
    _require_csrf(request)
    try:
        job = worker.start(body.library_id, force=body.force, retry_failed=body.retry_failed)
        return _job_row(job)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/index/status")
def index_status():
    return _job_row(worker.latest())


@app.post("/api/index/{job_id}/pause")
def pause_job(job_id: int, request: Request):
    _require_csrf(request)
    try:
        return _job_row(worker.control(job_id, "pause"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/index/{job_id}/resume")
def resume_job(job_id: int, request: Request):
    _require_csrf(request)
    try:
        return _job_row(worker.control(job_id, "resume"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/index/{job_id}/cancel")
def cancel_job(job_id: int, request: Request):
    _require_csrf(request)
    try:
        return _job_row(worker.control(job_id, "cancel"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/index/reconcile")
def start_reconcile(request: Request):
    _require_csrf(request)
    try:
        job = worker.reconcile()
        return _job_row(job)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Settings / Maintenance
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    s = _settings()
    db_path = config.data_dir / "index.sqlite"
    emb_path = config.data_dir / "embeddings.bin"
    thumb_dir = config.data_dir / "thumbnails"
    return {
        **s,
        "storage": {
            "database": str(db_path),
            "embeddings": str(emb_path),
            "thumbnails": str(thumb_dir),
        },
        "roots": [str(p) for p in config.roots],
    }


@app.patch("/api/settings")
def patch_settings(body: SettingsPatch, request: Request):
    _require_csrf(request)
    updates = body.model_dump(exclude_none=True)
    if "matching_threshold" in updates and not (0.3 <= updates["matching_threshold"] <= 0.8):
        raise HTTPException(400, "matching_threshold must be between 0.3 and 0.8")
    if "review_threshold" in updates and not (0.4 <= updates["review_threshold"] <= 0.95):
        raise HTTPException(400, "review_threshold must be between 0.4 and 0.95")
    if "detection_size" in updates and updates["detection_size"] not in (320, 640, 960):
        raise HTTPException(400, "detection_size must be 320, 640, or 960")
    if "video_interval" in updates and not (1 <= updates["video_interval"] <= 30):
        raise HTTPException(400, "video_interval must be between 1 and 30")
    if "theme" in updates and updates["theme"] not in ("light", "dark", "system"):
        raise HTTPException(400, "theme must be light, dark, or system")
    if "min_face_quality" in updates and not (0.0 <= updates["min_face_quality"] <= 1.0):
        raise HTTPException(400, "min_face_quality must be between 0 and 1")
    if "auto_confirm_threshold" in updates and not (0.3 <= updates["auto_confirm_threshold"] <= 0.95):
        raise HTTPException(400, "auto_confirm_threshold must be between 0.3 and 0.95")

    with db.connect() as conn:
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            if key == "detection_size":
                engine.detection_size = int(value)
            if key == "multi_scale":
                engine.multi_scale = bool(value)
    cluster.invalidate()
    return get_settings()


@app.post("/api/settings/autotune")
def autotune_threshold(request: Request):
    _require_csrf(request)
    report = threshold_tuning.autotune(db, cluster)
    if report is None:
        raise HTTPException(
            400, f"Need at least {threshold_tuning.MIN_SAMPLES} reviewed faces first."
        )
    return report


@app.get("/api/evaluation")
def run_evaluation(threshold: Optional[float] = None):
    """Measure precision/recall on confirmed+named faces (leave-one-out)."""
    report = evaluation_mod.evaluate(db, store, matching_threshold=threshold)
    if report is None:
        raise HTTPException(
            400,
            f"Need at least {evaluation_mod.MIN_LABELED_FACES} confirmed faces "
            f"across {evaluation_mod.MIN_LABELED_PEOPLE} named people.",
        )
    return report


@app.post("/api/review/dedupe-video")
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


@app.post("/api/review/auto-confirm")
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


@app.post("/api/maintenance")
def maintenance(body: MaintenanceBody, request: Request):
    _require_csrf(request)
    action = body.action
    confirm = body.confirm
    expected = {
        "thumbnails": "CLEAR THUMBNAILS",
        "index": "CLEAR AI INDEX",
        "reset": "RESET DATABASE",
    }
    if action not in expected:
        raise HTTPException(400, "action must be thumbnails, index, or reset")
    if confirm != expected[action]:
        raise HTTPException(400, f"confirm must be '{expected[action]}'")

    if action == "thumbnails":
        thumb_dir = config.data_dir / "thumbnails"
        if thumb_dir.is_dir():
            for child in thumb_dir.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        with db.connect() as conn:
            conn.execute("UPDATE media SET thumbnail=NULL")
            conn.execute("UPDATE faces SET thumbnail=NULL")
        return {"ok": True, "cleared": "thumbnails"}

    if action == "index":
        with db.connect() as conn:
            conn.execute("DELETE FROM faces")
            conn.execute("DELETE FROM people")
            conn.execute("DELETE FROM exclusions")
            conn.execute("DELETE FROM separate_people")
            conn.execute("DELETE FROM rejections")
            conn.execute("UPDATE media SET status='pending', error=NULL, indexed_at=NULL, thumbnail=NULL, duplicate_count=0")
        emb = config.data_dir / "embeddings.bin"
        if emb.is_file():
            emb.unlink()
        # reopen store
        global store, cluster, worker
        store.close()
        store = EmbeddingStore(config.data_dir / "embeddings.bin")
        cluster = Clustering(db, store)
        worker = Worker(db, config, engine, store, cluster)
        return {"ok": True, "cleared": "index"}

    # reset
    with db.connect() as conn:
        for table in ("faces", "people", "exclusions", "separate_people", "rejections", "jobs", "media", "libraries"):
            conn.execute(f"DELETE FROM {table}")
    emb = config.data_dir / "embeddings.bin"
    if emb.is_file():
        emb.unlink()
    thumb_dir = config.data_dir / "thumbnails"
    if thumb_dir.is_dir():
        shutil.rmtree(thumb_dir, ignore_errors=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)
    store.close()
    store = EmbeddingStore(config.data_dir / "embeddings.bin")
    cluster = Clustering(db, store)
    worker = Worker(db, config, engine, store, cluster)
    return {"ok": True, "cleared": "database"}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _stream_export(media_ids: list[int], filename: str):
    if len(media_ids) > 5000:
        raise HTTPException(400, "At most 5000 media items per export")
    if not media_ids:
        raise HTTPException(400, "No media to export")

    def generate():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            manifest = []
            for mid in media_ids:
                row = db.one("SELECT * FROM media WHERE id=?", (mid,))
                if not row or row.get("deleted_at") or row.get("missing"):
                    continue
                path = Path(row["path"])
                if not path.is_file():
                    continue
                arcname = f"{row['kind']}s/{path.name}"
                # avoid collisions
                base, ext = path.stem, path.suffix
                counter = 1
                while arcname in {m["archive"] for m in manifest}:
                    arcname = f"{row['kind']}s/{base}_{counter}{ext}"
                    counter += 1
                try:
                    zf.write(path, arcname)
                    manifest.append({
                        "id": mid,
                        "name": row["name"],
                        "kind": row["kind"],
                        "archive": arcname,
                        "path": str(path),
                    })
                except Exception:
                    continue
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        buf.seek(0)
        yield from buf

    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export")
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
    return _stream_export(media_ids, "face-hunger-export.zip")


# ---------------------------------------------------------------------------
# Static frontend + SPA fallback
# ---------------------------------------------------------------------------

frontend_dir = config.frontend_dir
if frontend_dir.is_dir():
    assets_dir = frontend_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = frontend_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = frontend_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(404, "Frontend not built")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("shutdown")
def on_shutdown():
    worker.shutdown(timeout=5)
    try:
        store.close()
    except Exception:
        pass


def main():
    config.prepare()
    print(f"Face Hunger starting on http://{config.host}:{config.port}")
    print(f"  data_dir  = {config.data_dir}")
    print(f"  model_dir = {config.model_dir}")
    print(f"  frontend  = {config.frontend_dir}")
    uvicorn.run(
        "backend.__main__:app",
        host=config.host,
        port=config.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()