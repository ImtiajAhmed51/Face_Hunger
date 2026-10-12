"""Faces API routes."""

from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from ..deps import cluster, config, db, services
from ..media_processing import load_image
from ..schemas import BulkReviewBody, ConfirmBody, MoveFacesBody, ReviewBody
from ..services.presenters import _now, _require_csrf
from ..services.thumbnails import _placeholder_face_jpeg, _serve_face_thumb_file

router = APIRouter()

@router.post("/api/faces/move")
def move_faces(body: MoveFacesBody, request: Request):
    _require_csrf(request)
    if not body.face_ids:
        raise HTTPException(400, "face_ids required")
    try:
        result = services().library.move_faces(body.face_ids, target_id=body.target_id, name=body.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"person_id": result["person_id"], "audit_id": result["audit_id"]}


@router.post("/api/faces/review")
def review_faces(body: BulkReviewBody, request: Request):
    _require_csrf(request)
    return _review_faces(list(dict.fromkeys(body.face_ids)), body.decision, audited=True)


@router.post("/api/faces/{face_id}/review")
def review_face(face_id: int, body: ReviewBody, request: Request):
    _require_csrf(request)
    return _review_faces([face_id], body.decision)


def _review_faces(face_ids: list[int], decision: str, *, audited: bool = False):
    """One atomic transaction and one representative refresh per affected person."""
    decision = decision.lower()
    if decision not in ("yes", "no", "reset"):
        raise HTTPException(400, "decision must be 'yes', 'no' or 'reset'")
    target = {"yes": "confirmed", "no": "rejected", "reset": "unreviewed"}[decision]
    placeholders = ",".join("?" for _ in face_ids)
    with db.connect() as conn:
        faces = conn.execute(f"SELECT * FROM faces WHERE id IN ({placeholders})", face_ids).fetchall()
        if len(faces) != len(face_ids):
            raise HTTPException(404, "Face not found")
        if any(face["deleted_at"] for face in faces):
            raise HTTPException(409, "Restore deleted faces before reviewing them")
        changed = [face for face in faces if face["review_state"] != target or face["manual"] != int(decision != "reset") or decision == "reset"]
        if not changed:
            return {"ok": True, "already": True}
        audit_id = None
        if audited:
            changed_ids = [f["id"] for f in changed]
            ph = ",".join("?" for _ in changed_ids)
            snapshot = {
                "faces": [{k: f[k] for k in ("id", "person_id", "review_state", "manual")} for f in changed],
                "after": target,
                "face_match_feedback": [dict(r) for r in conn.execute(f"SELECT * FROM face_match_feedback WHERE face_id IN ({ph})", changed_ids)],
                "rejections": [dict(r) for r in conn.execute(f"SELECT * FROM rejections WHERE face_id IN ({ph})", changed_ids)],
                "hard_negatives": [dict(r) for r in conn.execute(f"SELECT * FROM hard_negatives WHERE face_id IN ({ph})", changed_ids)],
            }
            audit_id = services().library._log(conn, "faces.review", f"Reviewed {len(changed)} face(s): {decision}", snapshot, len(changed))
        conn.executemany("UPDATE faces SET review_state=?, manual=? WHERE id=?",
                         [(target, int(decision != "reset"), f["id"]) for f in changed])
        for face in changed:
            previous_feedback = conn.execute("SELECT person_id FROM face_match_feedback WHERE face_id=?", (face["id"],)).fetchone()
            # Legacy manual moves did not clear the old candidate score. Never
            # treat those scores as labels; only record an automatic candidate
            # or a previously recorded review of the same person.
            trustworthy = not face["manual"] or (previous_feedback and previous_feedback[0] == face["person_id"])
            if decision == "reset":
                conn.execute("DELETE FROM face_match_feedback WHERE face_id=?", (face["id"],))
            elif trustworthy and face["person_id"] and face["similarity"] is not None:
                conn.execute("""INSERT INTO face_match_feedback(face_id,person_id,similarity,correct) VALUES (?,?,?,?)
                    ON CONFLICT(face_id) DO UPDATE SET person_id=excluded.person_id,similarity=excluded.similarity,
                    correct=excluded.correct,reviewed_at=CURRENT_TIMESTAMP""",
                    (face["id"], face["person_id"], face["similarity"], int(decision == "yes")))
        people = set()
        for face in changed:
            pid = face["person_id"]
            if not pid:
                continue
            people.add(pid)
            if decision == "no":
                conn.execute("INSERT OR IGNORE INTO rejections(face_id,person_id) VALUES (?,?)", (face["id"], pid))
                conn.execute("INSERT OR IGNORE INTO hard_negatives(face_id,person_id,similarity) VALUES (?,?,?)",
                             (face["id"], pid, face["similarity"]))
            else:
                conn.execute("DELETE FROM rejections WHERE face_id=? AND person_id=?", (face["id"], pid))
                conn.execute("DELETE FROM hard_negatives WHERE face_id=? AND person_id=?", (face["id"], pid))
        for pid in people:
            rep = conn.execute("""SELECT id FROM faces WHERE person_id=? AND deleted_at IS NULL AND review_state!='rejected'
                ORDER BY CASE review_state WHEN 'confirmed' THEN 0 ELSE 1 END, quality DESC, detection DESC, id LIMIT 1""",
                               (pid,)).fetchone()
            conn.execute("UPDATE people SET representative_face_id=? WHERE id=?", (rep["id"] if rep else None, pid))
        counter = conn.execute("SELECT value FROM settings WHERE key='review_autotune_counter'").fetchone()
        previous = int(json.loads(counter[0])) if counter else 0
        n = previous + (len(changed) if decision != "reset" else 0)
        conn.execute("INSERT INTO settings(key,value) VALUES ('review_autotune_counter',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(n),))
    cluster.invalidate()
    if n // 50 > previous // 50:
        import threading

        def _bg_train():
            try:
                from ..match_learning import train
                train(db)
                cluster.invalidate()
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Local match training failed")

        threading.Thread(target=_bg_train, name="review-match-training", daemon=True).start()
    return {"ok": True, **({"audit_id": audit_id} if audit_id is not None else {})}


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
                    from ..media_processing import frame_at
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
