"""Settings API routes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from .. import evaluation as evaluation_mod
from .. import threshold_tuning
from ..deps import cluster, config, db, engine, services, store
from ..media_processing import load_image
from ..schemas import MaintenanceBody, SettingsPatch
from ..services.presenters import (
    _REP_ORDER,
    _pick_representative_face,
    _require_csrf,
    _settings,
)

router = APIRouter()

@router.get("/api/settings")
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


@router.patch("/api/settings")
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
    if updates.get("map_pmtiles_path"):
        candidate = Path(updates["map_pmtiles_path"]).expanduser()
        if candidate.suffix.lower() != ".pmtiles" or not candidate.is_file():
            raise HTTPException(400, "map_pmtiles_path must be an existing .pmtiles file on this computer")
        updates["map_pmtiles_path"] = str(candidate.resolve())
    if "dino_similarity_threshold" in updates and not (0.5 <= updates["dino_similarity_threshold"] <= 0.999):
        raise HTTPException(400, "dino_similarity_threshold must be between 0.5 and 0.999")

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


@router.post("/api/settings/autotune")
def autotune_threshold(request: Request):
    _require_csrf(request)
    report = threshold_tuning.autotune(db, cluster)
    if report is None:
        raise HTTPException(
            400, f"Need at least {threshold_tuning.MIN_SAMPLES} reviewed faces first."
        )
    return report


@router.get("/api/evaluation")
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
@router.post("/api/maintenance")
def maintenance(body: MaintenanceBody, request: Request):
    _require_csrf(request)
    action = body.action
    confirm = body.confirm
    expected = {
        "thumbnails": "CLEAR THUMBNAILS",
        "rebuild_thumbnails": "REBUILD THUMBNAILS",
        "index": "CLEAR AI INDEX",
        "reset": "RESET DATABASE",
    }
    if action not in expected:
        raise HTTPException(
            400,
            "action must be thumbnails, rebuild_thumbnails, index, or reset",
        )
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
        thumb_dir.mkdir(parents=True, exist_ok=True)
        with db.connect() as conn:
            conn.execute("UPDATE media SET thumbnail=NULL")
            conn.execute("UPDATE faces SET thumbnail=NULL")
            # Re-pick tiles: prefer still photos so first page load regenerates useful faces
            people = conn.execute("SELECT id FROM people").fetchall()
            for person in people:
                pid = person["id"]
                row = conn.execute(
                    f"""
                    SELECT f.id FROM faces f
                    JOIN media m ON m.id = f.media_id
                    WHERE f.person_id = ? AND f.deleted_at IS NULL
                      AND m.deleted_at IS NULL AND m.missing = 0
                    ORDER BY {_REP_ORDER}
                    LIMIT 1
                    """,
                    (pid,),
                ).fetchone()
                conn.execute(
                    "UPDATE people SET representative_face_id=? WHERE id=?",
                    (row["id"] if row else None, pid),
                )
        return {"ok": True, "cleared": "thumbnails", "note": "Open People/Photos to rebuild on demand, or use Rebuild thumbnails."}

    if action == "rebuild_thumbnails":
        # Fast path: rebuild person face tiles only (photos preferred). Media thumbs
        # regenerate on demand when browsing — keeps this request under timeout.
        try:
            import numpy as np
            from PIL import Image

            from .media_processing import frame_at

            skip_suffix = {".mkv", ".webm"}
            thumb_dir = config.data_dir / "thumbnails"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            built_faces = 0
            failed = 0
            last_error = None

            people = db.all(
                """SELECT p.id FROM people p
                   WHERE EXISTS (
                     SELECT 1 FROM faces f JOIN media m ON m.id=f.media_id
                     WHERE f.person_id=p.id AND f.deleted_at IS NULL
                       AND m.deleted_at IS NULL AND m.missing=0
                   )
                   ORDER BY p.face_count DESC
                   LIMIT 300"""
            )
            for person in people:
                pid = int(person["id"])
                try:
                    fid = _pick_representative_face(pid)
                    if not fid:
                        continue
                    face = db.one("SELECT * FROM faces WHERE id=?", (fid,))
                    if not face:
                        failed += 1
                        continue
                    media = db.one("SELECT * FROM media WHERE id=?", (face["media_id"],))
                    if not media:
                        failed += 1
                        continue
                    src = Path(str(media["path"]))
                    if not src.is_file():
                        failed += 1
                        last_error = f"missing file: {src.name}"
                        continue
                    if media.get("kind") == "video" and src.suffix.lower() in skip_suffix:
                        failed += 1
                        continue

                    bbox = face["bbox"]
                    if isinstance(bbox, str):
                        bbox = json.loads(bbox)
                    x, y, w, h = [float(v) for v in bbox]
                    pad = max(w, h) * 0.15

                    if media.get("kind") == "video":
                        seconds = float(face["timestamp"]) if face.get("timestamp") is not None else 0.0
                        bgr = frame_at(str(src), max(0.0, seconds), prefer_ffmpeg=True)
                    else:
                        bgr = load_image(str(src))

                    if bgr is None or getattr(bgr, "size", 0) == 0:
                        raise ValueError("empty frame")

                    h_img, w_img = int(bgr.shape[0]), int(bgr.shape[1])
                    x1, y1 = max(0, int(x - pad)), max(0, int(y - pad))
                    x2, y2 = min(w_img, int(x + w + pad)), min(h_img, int(y + h + pad))
                    crop = bgr if x2 <= x1 or y2 <= y1 else bgr[y1:y2, x1:x2]
                    if crop is None or getattr(crop, "size", 0) == 0:
                        crop = bgr

                    rgb = np.ascontiguousarray(crop[:, :, ::-1])
                    img = Image.fromarray(rgb)
                    img.thumbnail((256, 256))
                    out = thumb_dir / f"face-{fid}.jpg"
                    img.save(str(out), format="JPEG", quality=85)

                    with db.connect() as conn:
                        conn.execute(
                            "UPDATE faces SET thumbnail=? WHERE id=?",
                            (str(out), fid),
                        )
                        conn.execute(
                            "UPDATE people SET representative_face_id=? WHERE id=?",
                            (fid, pid),
                        )
                    built_faces += 1
                except Exception as exc:
                    failed += 1
                    last_error = str(exc)[:200]

            return {
                "ok": True,
                "rebuilt_faces": built_faces,
                "failed": failed,
                "last_error": last_error,
            }
        except Exception as exc:
            raise HTTPException(500, f"Rebuild failed: {exc}") from exc

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
        # reopen store (and the clustering/worker bound to it)
        services().reopen_face_store()
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
    services().reopen_face_store()
    return {"ok": True, "cleared": "database"}
