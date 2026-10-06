"""Row presenters and shared helpers used by the API routers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from ..deps import db, video_compat


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


# Prefer still photos for person tiles; only use video faces when no photo remains.
_REP_ORDER = """
    CASE m.kind WHEN 'photo' THEN 0 ELSE 1 END,
    CASE f.review_state WHEN 'confirmed' THEN 0 ELSE 1 END,
    COALESCE(f.quality, 0) DESC,
    f.detection DESC,
    f.id
"""


def _pick_representative_face(person_id: int, conn=None) -> Optional[int]:
    """Best face for a person tile: best photo first, else best video face."""
    sql = f"""
        SELECT f.id FROM faces f
        JOIN media m ON m.id = f.media_id
        WHERE f.person_id = ? AND f.deleted_at IS NULL
          AND m.deleted_at IS NULL AND m.missing = 0
        ORDER BY {_REP_ORDER}
        LIMIT 1
    """
    if conn is not None:
        row = conn.execute(sql, (person_id,)).fetchone()
        return int(row["id"]) if row else None
    row = db.one(sql, (person_id,))
    return int(row["id"]) if row else None


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
    face_count = int(stats.get("face_count") or 0)
    photo_count = int(stats.get("photo_count") or 0)

    need_new = face_count > 0 and rep is None
    if rep is not None and face_count > 0:
        meta = db.one(
            """
            SELECT f.id, m.kind FROM faces f
            JOIN media m ON m.id = f.media_id
            WHERE f.id = ? AND f.person_id = ? AND f.deleted_at IS NULL
              AND m.deleted_at IS NULL AND m.missing = 0
            """,
            (rep, pid),
        )
        if not meta:
            need_new = True
        elif meta.get("kind") == "video" and photo_count > 0:
            # Upgrade tile from video frame → best still photo
            need_new = True

    if need_new:
        rep = _pick_representative_face(pid) if face_count > 0 else None
        try:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE people SET representative_face_id=? WHERE id=?",
                    (rep, pid),
                )
        except Exception:
            pass

    return {
        "id": pid,
        "name": name,
        "display_name": _display_name(name, pid),
        "face_count": face_count,
        "photo_count": photo_count,
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


def _edit_fields(media_id: int) -> dict:
    """Rating / label / flag and the edit version (cache-buster for edited thumbnails)."""
    e = db.one("SELECT rating, label, flag, version, rotation, flip_h, flip_v, crop FROM media_edits WHERE media_id=?",
               (media_id,))
    if not e:
        return {"rating": 0, "label": None, "flag": None, "edit_version": 0, "edited": False}
    return {"rating": e["rating"], "label": e["label"], "flag": e["flag"], "edit_version": e["version"],
            "edited": bool(e["rotation"] or e["flip_h"] or e["flip_v"] or e["crop"])}


def _media_row(row: dict) -> dict:
    face_count = db.one(
        "SELECT COUNT(*) AS c FROM faces WHERE media_id=? AND deleted_at IS NULL",
        (row["id"],),
    )
    out = {
        "id": row["id"],
        "name": row["name"],
        "path": row.get("path"),
        "kind": row["kind"],
        "captured_at": row.get("captured_at"),
        "width": row.get("width"),
        "height": row.get("height"),
        "duration": row.get("duration"),
        "size": int(row.get("size") or 0),
        "status": row.get("status") or "indexed",
        "missing": bool(row.get("missing")),
        "deleted_at": row.get("deleted_at"),
        "face_count": int((face_count or {}).get("c") or 0),
        "people": _media_people(row["id"]),
        "favorite": bool(db.one("SELECT 1 AS x FROM favorites WHERE media_id=?", (row["id"],))),
        **_edit_fields(row["id"]),
        # Capture metadata (Phase 2); None for rows not backfilled yet.
        "date_source": row.get("date_source"),
        "camera_make": row.get("camera_make"),
        "camera_model": row.get("camera_model"),
        "lens": row.get("lens"),
        "gps_lat": row.get("gps_lat"),
        "gps_lon": row.get("gps_lon"),
    }
    # Soft-kept pre-conversion original (for frontend)
    orig = row.get("original_path")
    if orig:
        op = Path(orig)
        out["original_path"] = str(op) if op.is_file() else None
        out["original_name"] = op.name if op.is_file() else None
        out["has_original"] = op.is_file()
    else:
        # Heuristic: sibling *.lfs_original from current stem
        out["original_path"] = None
        out["original_name"] = None
        out["has_original"] = False
        try:
            cur = Path(row["path"]) if row.get("path") else None
            if cur is not None and cur.parent.is_dir():
                stem = cur.stem
                for cand in cur.parent.iterdir():
                    if cand.name.endswith(".lfs_original") and (
                        cand.name.startswith(stem + ".")
                        or cand.name.startswith(stem + ".lfs")
                    ):
                        if cand.is_file():
                            out["original_path"] = str(cand)
                            out["original_name"] = cand.name
                            out["has_original"] = True
                            break
        except OSError:
            pass

    if row.get("kind") == "video" and row.get("path"):
        try:
            st = video_compat.status_response(row["id"])
            out["playback_status"] = st.get("status")
            out["conversion"] = st
        except Exception:
            out["playback_status"] = "pending"
    return out


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

