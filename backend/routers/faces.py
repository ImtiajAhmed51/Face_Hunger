"""Faces API routes."""

from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from .. import threshold_tuning
from ..deps import cluster, config, db
from ..media_processing import load_image
from ..schemas import ConfirmBody, MoveFacesBody, ReviewBody
from ..services.presenters import _now, _require_csrf
from ..services.thumbnails import _placeholder_face_jpeg, _serve_face_thumb_file

router = APIRouter()

@router.post("/api/faces/move")
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


@router.post("/api/faces/{face_id}/review")
def review_face(face_id: int, body: ReviewBody, request: Request):
    _require_csrf(request)
    decision = body.decision.lower()
    if decision not in ("yes", "no", "reset"):
        raise HTTPException(400, "decision must be 'yes', 'no' or 'reset'")
    if decision == "reset":
        # Undo a Yes/No: back to the review queue, without the rejection side effects.
        with db.connect() as conn:
            face = conn.execute("SELECT * FROM faces WHERE id=?", (face_id,)).fetchone()
            if not face:
                raise HTTPException(404, "Face not found")
            conn.execute("UPDATE faces SET review_state='unreviewed', manual=0 WHERE id=?", (face_id,))
            if face["person_id"]:
                conn.execute("DELETE FROM rejections WHERE face_id=? AND person_id=?", (face_id, face["person_id"]))
                conn.execute("DELETE FROM hard_negatives WHERE face_id=? AND person_id=?", (face_id, face["person_id"]))
        cluster.invalidate()
        return {"ok": True}
    # Single connection for the whole mutation — no extra round-trips.
    with db.connect() as conn:
        face = conn.execute("SELECT * FROM faces WHERE id=?", (face_id,)).fetchone()
        if not face:
            raise HTTPException(404, "Face not found")
        face = dict(face)
        current = face.get("review_state") or "unreviewed"
        target = "confirmed" if decision == "yes" else "rejected"
        # Idempotent only when already at the requested state — allows correcting a mistaken Confirm with Not them
        if current == target:
            return {"ok": True, "already": True}
        if decision == "yes":
            conn.execute(
                "UPDATE faces SET review_state='confirmed', manual=1 WHERE id=?",
                (face_id,),
            )
            # Undoing a prior "Not them" — drop rejection / hard-negative rows
            if face.get("person_id"):
                conn.execute(
                    "DELETE FROM rejections WHERE face_id=? AND person_id=?",
                    (face_id, face["person_id"]),
                )
                try:
                    conn.execute(
                        "DELETE FROM hard_negatives WHERE face_id=? AND person_id=?",
                        (face_id, face["person_id"]),
                    )
                except Exception:
                    pass
        else:
            # "Not them" works on unreviewed AND on a mistaken prior confirm
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
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO hard_negatives(face_id, person_id, similarity) VALUES (?,?,?)",
                        (face_id, face["person_id"], face.get("similarity")),
                    )
                except Exception:
                    pass
        if face.get("person_id"):
            # Lightweight count/rep update only — full centroid recompute is deferred
            # so Yes/No stays snappy during rapid review.
            pid = face["person_id"]
            if decision == "yes":
                # Only promote representative when this face looks better than current rep
                row = conn.execute(
                    """SELECT id FROM faces WHERE person_id=? AND deleted_at IS NULL
                       AND review_state != 'rejected'
                       ORDER BY CASE review_state WHEN 'confirmed' THEN 0 ELSE 1 END,
                                quality DESC, detection DESC, id LIMIT 1""",
                    (pid,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE people SET representative_face_id=? WHERE id=?",
                        (row["id"], pid),
                    )
            else:
                # If the rejected face was the representative, pick another
                row = conn.execute(
                    "SELECT representative_face_id FROM people WHERE id=?",
                    (pid,),
                ).fetchone()
                if row and row["representative_face_id"] == face_id:
                    alt = conn.execute(
                        """SELECT id FROM faces WHERE person_id=? AND deleted_at IS NULL
                           AND review_state != 'rejected' AND id != ?
                           ORDER BY CASE review_state WHEN 'confirmed' THEN 0 ELSE 1 END,
                                    quality DESC, detection DESC, id LIMIT 1""",
                        (pid, face_id),
                    ).fetchone()
                    conn.execute(
                        "UPDATE people SET representative_face_id=? WHERE id=?",
                        (alt["id"] if alt else None, pid),
                    )
            # face_count does not change on review (only on delete/move) — skip full COUNT
        # Autotune counter in the same transaction
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='review_autotune_counter'"
            ).fetchone()
            n = int(json.loads(row[0])) if row else 0
            n += 1
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('review_autotune_counter',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(n),),
            )
        except Exception:
            n = 0
    # Soft invalidation only — do not rebuild all centroids on every click
    cluster.invalidate()
    # Autotune is expensive. Run off the request thread every ~50 reviews (was 25).
    if n and n % 50 == 0:
        import threading
        def _bg_autotune():
            try:
                threshold_tuning.autotune(db, cluster)
            except Exception:
                pass
        threading.Thread(target=_bg_autotune, name="review-autotune", daemon=True).start()
    return {"ok": True}


@router.delete("/api/faces/{face_id}")
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


@router.post("/api/faces/{face_id}/restore")
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


@router.delete("/api/faces/{face_id}/permanent")
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

@router.get("/api/faces/{face_id}/thumbnail")
def face_thumbnail(face_id: int):
    face = db.one("SELECT * FROM faces WHERE id=?", (face_id,))
    if not face:
        # Soft 200 placeholder — avoids red 404 noise in browser/network logs
        return Response(content=_placeholder_face_jpeg(), media_type="image/jpeg")

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
        resp = _serve_face_thumb_file(path)
        if resp is not None:
            return resp

    # Regenerate from original (ignore soft-deleted media row if file still on disk)
    media = db.one("SELECT * FROM media WHERE id=?", (face["media_id"],))
    if media:
        src = Path(media["path"])
        if src.is_file():
            try:
                import numpy as np
                from PIL import Image
                
                if media.get("kind") == "video" and src.suffix.lower() in {".mkv", ".webm"}:
                    raise ValueError("skipped container")

                bbox = face["bbox"]
                if isinstance(bbox, str):
                    bbox = json.loads(bbox)
                x, y, w, h = [float(v) for v in bbox]
                pad = max(w, h) * 0.15

                if media.get("kind") == "video":
                    seconds = float(face["timestamp"]) if face.get("timestamp") is not None else 0.0
                    from .media_processing import frame_at
                    bgr = frame_at(str(src), max(0.0, seconds), prefer_ffmpeg=True)
                else:
                    bgr = load_image(str(src))

                if bgr is None or getattr(bgr, "size", 0) == 0:
                    raise ValueError("decode failed")

                h_img, w_img = bgr.shape[:2]
                x1 = max(0, int(x - pad))
                y1 = max(0, int(y - pad))
                x2 = min(w_img, int(x + w + pad))
                y2 = min(h_img, int(y + h + pad))
                if x2 <= x1 or y2 <= y1:
                    side = max(32, min(h_img, w_img) // 2)
                    cx, cy = w_img // 2, h_img // 2
                    x1, y1 = max(0, cx - side // 2), max(0, cy - side // 2)
                    x2, y2 = min(w_img, x1 + side), min(h_img, y1 + side)
                crop = bgr[y1:y2, x1:x2]
                if crop.size == 0:
                    crop = bgr

                rgb = crop[:, :, ::-1]
                img = Image.fromarray(np.ascontiguousarray(rgb))
                img.thumbnail((256, 256))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                data = buf.getvalue()
                try:
                    out = config.data_dir / "thumbnails" / f"face-{face_id}.jpg"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(data)
                    with db.connect() as conn:
                        conn.execute(
                            "UPDATE faces SET thumbnail=? WHERE id=?",
                            (str(out), face_id),
                        )
                except Exception:
                    pass
                return Response(content=data, media_type="image/jpeg")
            except Exception:
                pass

    return Response(
        content=_placeholder_face_jpeg(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
