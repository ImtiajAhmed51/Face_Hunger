"""Media API routes."""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from .. import edits as edit_model
from .. import imaging, scoring
from ..deps import cluster, config, db, services, video_compat
from ..media_http import hover_clip, media_response
from ..media_processing import _ffmpeg_bin, load_image
from ..scanner import authorized_root
from ..schemas import ConfirmBody, MoveMediaBody, PurgeMediaBody
from ..services.presenters import (
    _face_row,
    _media_row,
    _now,
    _page,
    _require_csrf,
    _review_threshold,
)
from ..services.soft_originals import (
    _list_converted_backups,
    _list_soft_originals,
    _resolve_soft_original,
    _restored_path_from_soft,
)
from ..services.thumbnails import lqip_data_url

router = APIRouter()

@router.get("/api/media")
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
    bbox: Optional[str] = Query(None, description="minLon,minLat,maxLon,maxLat: geotagged media inside"),
    event: Optional[int] = Query(None, description="Only media in this event"),
    album: Optional[int] = Query(None, description="Only media in this album (album order)"),
    rating_min: Optional[int] = Query(None, ge=1, le=5, description="Only media rated at least this"),
    label: Optional[str] = Query(None, description="Only media with this colour label"),
    flag: Optional[str] = Query(None, description="pick | reject | unflagged"),
    favorite: bool = Query(False, description="Only favorites"),
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

    if event is not None:
        where.append("EXISTS (SELECT 1 FROM event_media em WHERE em.media_id = m.id AND em.event_id = ?)")
        params.append(event)
    if album is not None:
        where.append("EXISTS (SELECT 1 FROM album_media am WHERE am.media_id = m.id AND am.album_id = ?)")
        params.append(album)
    if favorite:
        where.append("EXISTS (SELECT 1 FROM favorites fv WHERE fv.media_id = m.id)")
    if rating_min is not None:
        where.append("EXISTS (SELECT 1 FROM media_edits me WHERE me.media_id = m.id AND me.rating >= ?)")
        params.append(rating_min)
    if label:
        where.append("EXISTS (SELECT 1 FROM media_edits me WHERE me.media_id = m.id AND lower(me.label) = lower(?))")
        params.append(label)
    if flag in ("pick", "reject"):
        where.append("EXISTS (SELECT 1 FROM media_edits me WHERE me.media_id = m.id AND me.flag = ?)")
        params.append(flag)
    elif flag == "unflagged":
        where.append("NOT EXISTS (SELECT 1 FROM media_edits me WHERE me.media_id = m.id AND me.flag IS NOT NULL)")

    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(400, "bbox must be minLon,minLat,maxLon,maxLat")
        where.append("m.gps_lat BETWEEN ? AND ?")
        params.extend([min_lat, max_lat])
        if min_lon <= max_lon:
            where.append("m.gps_lon BETWEEN ? AND ?")
            params.extend([min_lon, max_lon])
        else:  # crosses the antimeridian
            where.append("(m.gps_lon >= ? OR m.gps_lon <= ?)")
            params.extend([min_lon, max_lon])

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
    if album is not None and sort_key == "date":  # albums keep the order items were added in
        sort_key = "album"
    if sort_key == "size_desc":
        order_sql = "m.size DESC, m.id DESC"
    elif sort_key == "size_asc":
        order_sql = "m.size ASC, m.id ASC"
    elif sort_key == "name":
        order_sql = "m.name COLLATE NOCASE ASC, m.id ASC"
    elif sort_key == "album":
        order_sql = (f"(SELECT am.position FROM album_media am WHERE am.media_id = m.id AND am.album_id = {int(album)}) ASC, m.id"
                     if album is not None else "m.id")
    elif sort_key == "date_asc":
        order_sql = "COALESCE(m.captured_at, m.indexed_at) ASC, m.id ASC"
    elif sort_key == "best":
        order_sql = (f"COALESCE((SELECT q.score FROM quality_scores q WHERE q.media_id=m.id "
                     f"AND q.formula_version={int(scoring.FORMULA_VERSION)}), -1) DESC, m.id DESC")
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


@router.get("/api/media/conversion-statuses")
def media_conversion_statuses(ids: str = Query("", description="Comma-separated media ids")):
    """Batch conversion status for grid badges (does not start conversion)."""
    out: dict[str, dict] = {}
    raw = [x.strip() for x in ids.split(",") if x.strip()]
    id_list: list[int] = []
    for x in raw[:120]:
        try:
            id_list.append(int(x))
        except ValueError:
            continue

    try:
        with video_compat._lock:
            active_ids = set(video_compat._active)
            known_ids = set(video_compat._states.keys())
    except Exception:
        active_ids = set()
        known_ids = set()

    watch = set(id_list) | active_ids
    for mid in watch:
        if mid not in known_ids and mid not in active_ids:
            continue
        st = video_compat.status_response(mid)
        status = st.get("status") or ""
        # Only surface meaningful states to the grid
        if status in (
            "analyzing", "converting", "verifying", "replacing",
            "pending", "failed", "completed",
        ) or mid in active_ids:
            out[str(mid)] = st
    return {"items": out}

@router.get("/api/media/lqip")
def media_lqip(ids: str = Query("", description="Comma-separated media ids (max 240)")):
    """Blur-up placeholders: ~400-byte data URLs built from cached thumbnails only."""
    wanted = []
    for raw in ids.split(",")[:240]:
        try:
            wanted.append(int(raw))
        except ValueError:
            continue
    out: dict[str, str] = {}
    if wanted:
        rows = db.all(f"SELECT id, thumbnail FROM media WHERE id IN ({','.join('?' * len(wanted))})", tuple(wanted))
        thumbs = config.data_dir / "thumbnails"
        for row in rows:
            path = thumbs / f"media-{row['id']}.jpg"
            if not path.is_file() and row.get("thumbnail"):
                path = Path(row["thumbnail"])
            url = lqip_data_url(row["id"], path)
            if url:
                out[str(row["id"])] = url
    return {"items": out}

@router.get("/api/media/soft-originals")
def list_soft_originals():
    items = _list_soft_originals()
    return {"items": items, "total": len(items), "total_bytes": sum(int(i.get("size") or 0) for i in items)}

@router.post("/api/media/soft-originals/purge")
def purge_soft_originals(request: Request):
    """Permanently delete all soft-kept pre-conversion originals."""
    _require_csrf(request)
    items = _list_soft_originals()
    deleted = 0
    failed: list[dict] = []
    freed = 0
    for item in items:
        path = Path(item["original_path"])
        try:
            size = int(item.get("size") or 0)
            if path.is_file():
                path.unlink()
                deleted += 1
                freed += size
            mid = item.get("media_id")
            if mid is not None:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE media SET original_path=NULL WHERE id=?",
                        (mid,),
                    )
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)})
    return {
        "deleted": deleted,
        "failed": failed,
        "freed_bytes": freed,
    }


@router.post("/api/media/soft-originals/restore-all")
def restore_all_soft_originals(request: Request):
    """Restore every soft-kept original found by _list_soft_originals()."""
    _require_csrf(request)
    items = _list_soft_originals()
    restored = 0
    failed: list[dict] = []
    for item in items:
        mid = int(item["media_id"])
        try:
            # inline same logic via internal call pattern
            row, soft = _resolve_soft_original(mid, item.get("original_path"))
            target = _restored_path_from_soft(soft)
            converted = Path(row["path"]) if row.get("path") else None
            backup = None
            if target.is_file() and target.resolve() != soft.resolve():
                backup = Path(str(target) + ".lfs_converted")
                n = 1
                while backup.exists():
                    backup = Path(str(target) + f".lfs_converted.{n}")
                    n += 1
                os.replace(str(target), str(backup))
            elif (
                converted is not None
                and converted.is_file()
                and converted.resolve() != soft.resolve()
                and converted.resolve() != target.resolve()
            ):
                backup = Path(str(converted) + ".lfs_converted")
                n = 1
                while backup.exists():
                    backup = Path(str(converted) + f".lfs_converted.{n}")
                    n += 1
                os.replace(str(converted), str(backup))
            os.replace(str(soft), str(target))
            try:
                st = target.stat()
                size = int(st.st_size)
                mtime_ns = int(st.st_mtime_ns)
            except OSError:
                size = 0
                mtime_ns = 0
            with db.connect() as conn:
                conn.execute(
                    """UPDATE media SET path=?, name=?, size=?, mtime_ns=?,
                       original_path=NULL, missing=0 WHERE id=?""",
                    (str(target), target.name, size, mtime_ns, mid),
                )
            restored += 1
        except HTTPException as exc:
            failed.append({"media_id": mid, "error": str(exc.detail)})
        except Exception as exc:
            failed.append({"media_id": mid, "error": str(exc)})
    return {"restored": restored, "failed": failed, "total": len(items)}


@router.post("/api/media/converted-backups/purge")
def purge_converted_backups(request: Request):
    """Permanently delete every *.lfs_converted backup on disk."""
    _require_csrf(request)
    items = _list_converted_backups()
    deleted = 0
    failed: list[dict] = []
    freed = 0
    for item in items:
        path = Path(item["path"])
        try:
            size = int(item.get("size") or 0)
            if path.is_file():
                path.unlink()
                deleted += 1
                freed += size
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)})
    return {"deleted": deleted, "failed": failed, "freed_bytes": freed}


@router.post("/api/media/converted-backups/delete")
async def delete_converted_backup(request: Request):
    """Delete one *.lfs_converted file by absolute path."""
    _require_csrf(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    raw = body.get("path") if isinstance(body, dict) else None
    if not raw or not isinstance(raw, str):
        raise HTTPException(400, "path required")
    path = Path(raw)
    if not path.is_file() or ".lfs_converted" not in path.name:
        raise HTTPException(404, "Converted backup not found")
    # Stay inside configured library roots
    try:
        resolved = path.resolve()
        allowed = False
        for root in config.roots:
            try:
                resolved.relative_to(root.resolve())
                allowed = True
                break
            except ValueError:
                continue
        # Also allow under data_dir just in case
        if not allowed:
            try:
                resolved.relative_to(Path(config.data_dir).resolve())
                allowed = True
            except ValueError:
                pass
        if not allowed:
            # Allow if parent matches any media path parent
            parents = {
                str(Path(r["path"]).parent.resolve())
                for r in db.all(
                    "SELECT path FROM media WHERE path IS NOT NULL AND deleted_at IS NULL"
                )
                if r.get("path")
            }
            if str(resolved.parent) not in parents:
                raise HTTPException(403, "Path is outside library roots")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Invalid path: {exc}") from exc
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(500, f"Could not delete: {exc}") from exc
    return {"ok": True, "deleted": str(path)}



@router.get("/api/media/{media_id}")
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


@router.get("/api/media/{media_id}/thumbnail")
def media_thumbnail(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")

    # 1) Cached thumbnail paths
    candidates: list[Path] = []
    thumb = row.get("thumbnail")
    if thumb:
        p = Path(thumb)
        candidates.append(p if p.is_absolute() else config.data_dir / "thumbnails" / thumb)
        candidates.append(config.data_dir / "thumbnails" / p.name)
    candidates.append(config.data_dir / "thumbnails" / f"media-{media_id}.jpg")
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                edit = services().edits.get(media_id)
                if edit_model.is_identity_geometry(edit):
                    return FileResponse(path, media_type="image/jpeg")
                # Geometric edits are applied to the cached thumbnail on the fly (a few ms).
                from PIL import Image
                with Image.open(path) as thumb_image:
                    out = io.BytesIO()
                    edit_model.apply(thumb_image.convert("RGB"), edit).save(out, format="JPEG", quality=86)
                return Response(out.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})
        except OSError:
            continue

    src = Path(row["path"])
    if not src.is_file():
        raise HTTPException(404, "Original file missing; thumbnail unavailable")

    # 2) Generate from original (photo or video) and cache
    try:
        import numpy as np
        from PIL import Image

        bgr = None
        if row["kind"] == "photo":
            bgr = load_image(str(src))
        else:
            # Prefer a face timestamp if one exists; else ~1s or first frame
            stamp_row = db.one(
                """SELECT timestamp FROM faces
                   WHERE media_id=? AND deleted_at IS NULL AND timestamp IS NOT NULL
                   ORDER BY COALESCE(quality, 0) DESC, detection DESC LIMIT 1""",
                (media_id,),
            )
            seconds = float(stamp_row["timestamp"]) if stamp_row and stamp_row["timestamp"] is not None else 1.0
            from .media_processing import frame_at
            bgr = frame_at(str(src), max(0.0, seconds), prefer_ffmpeg=True)

        if bgr is None or getattr(bgr, "size", 0) == 0:
            raise ValueError("could not decode frame")

        rgb = bgr[:, :, ::-1]
        img = Image.fromarray(np.ascontiguousarray(rgb))
        img.thumbnail((480, 480))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        data = buf.getvalue()

        try:
            out = config.data_dir / "thumbnails" / f"media-{media_id}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            with db.connect() as conn:
                conn.execute("UPDATE media SET thumbnail=? WHERE id=?", (str(out), media_id))
        except Exception:
            pass

        return Response(content=data, media_type="image/jpeg")
    except Exception as exc:
        raise HTTPException(404, f"Thumbnail not available: {exc}") from exc


@router.get("/api/media/{media_id}/preview")
def media_preview(media_id: int, full: bool = Query(False, description="RAW: full demosaic instead of the embedded camera preview")):
    """Upright viewer JPEG (<= 2560 px), cached in data_dir/previews. Originals are never modified."""
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    if row["kind"] != "photo":
        raise HTTPException(400, "Preview only available for photos")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(404, "File missing")
    try:
        cached = imaging.ensure_preview(path, config.data_dir, media_id, full=full and imaging.is_raw(path))
    except Exception as exc:
        raise HTTPException(500, f"Could not generate preview: {exc}") from exc
    return FileResponse(cached, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/api/media/{media_id}/hover-preview")
def media_hover_preview(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row or row["kind"] != "video":
        raise HTTPException(404, "Video not found")
    source = Path(row["path"])
    if not source.is_file():
        raise HTTPException(404, "Original file missing")
    try:
        clip = hover_clip(source, config.data_dir / "hover_previews", _ffmpeg_bin())
    except Exception as exc:
        raise HTTPException(503, "Preview temporarily unavailable", headers={"Retry-After": "15"}) from exc
    return media_response(clip)


@router.head("/api/media/{media_id}/file")
@router.get("/api/media/{media_id}/file")
def media_file(media_id: int, request: Request):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    path = Path(row["path"])
    if not path.is_file():
        # After conversion the path may still be stale for one request — try .mp4 sibling
        if row["kind"] == "video":
            alt = path.with_suffix(".mp4")
            if alt.is_file():
                path = alt
            else:
                raise HTTPException(404, "File missing on disk")
        else:
            raise HTTPException(404, "File missing on disk")

    if row["kind"] != "video":
        return media_response(path, row["name"])

    ready, state = video_compat.ensure_playable(media_id, path, start_if_needed=False)

    # Re-read path in case conversion replaced the file and updated DB
    if state.ready or state.status in ("completed", "ready"):
        row2 = db.one("SELECT * FROM media WHERE id=?", (media_id,))
        if row2 and row2.get("path"):
            path = Path(row2["path"])
        if not path.is_file():
            alt = Path(row["path"]).with_suffix(".mp4")
            if alt.is_file():
                path = alt
        if path.is_file():
            return media_response(path)

    # A failed conversion must not prevent downloading the preserved source.

    # Conversion needed but not started / in progress — still try original for native play.
    # Manual convert is started only via POST /api/media/{id}/convert.
    if path.is_file() and state.status not in ("converting", "analyzing", "verifying", "replacing"):
        return media_response(path, row.get("name") or path.name)

    raise HTTPException(
        409,
        detail={
            "status": state.status,
            "progress": state.progress,
            "stage": state.stage or "Converting video",
            "ready": False,
            "error": state.error,
            "message": "Video is being prepared for playback",
            "needs_conversion": True,
        },
    )




@router.get("/api/media/{media_id}/conversion-status")
@router.get("/api/media/{media_id}/playback")
def media_conversion_status(media_id: int):
    """Return conversion progress/stage. Does NOT start conversion (manual only)."""
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    if row["kind"] != "video":
        return {
            "status": "completed",
            "progress": 100,
            "stage": "Ready",
            "ready": True,
            "error": None,
            "needs_conversion": False,
        }
    path = Path(row["path"])
    if not path.is_file():
        alt = path.with_suffix(".mp4")
        if alt.is_file():
            path = alt
        else:
            return {
                "status": "failed",
                "progress": 0,
                "stage": "Source missing",
                "ready": False,
                "error": "Original file is missing on disk",
                "needs_conversion": False,
            }
    # Status only — never auto-start
    ready, state = video_compat.ensure_playable(media_id, path, start_if_needed=False)
    resp = video_compat.status_response(media_id)
    resp["file_url"] = f"/api/media/{media_id}/file"
    needs, _probe = video_compat.needs_conversion(path)
    # If already completed/ready, no conversion needed
    if ready or resp.get("status") in ("completed", "ready"):
        needs = False
    resp["needs_conversion"] = bool(needs) and not ready
    return resp


@router.post("/api/media/{media_id}/convert")
def media_start_convert(media_id: int, request: Request):
    """Explicitly start browser conversion for a video (no auto-start elsewhere)."""
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    if row["kind"] != "video":
        return {
            "status": "completed",
            "progress": 100,
            "stage": "Ready",
            "ready": True,
            "error": None,
            "needs_conversion": False,
        }
    path = Path(row["path"])
    if not path.is_file():
        alt = path.with_suffix(".mp4")
        if alt.is_file():
            path = alt
        else:
            raise HTTPException(404, "Original file is missing on disk")
    ready, state = video_compat.ensure_playable(media_id, path, start_if_needed=True)
    resp = video_compat.status_response(media_id)
    resp["file_url"] = f"/api/media/{media_id}/file"
    resp["needs_conversion"] = not ready and state.status not in ("completed", "ready")
    return resp


@router.get("/api/media/{media_id}/original")
def media_original_file(media_id: int):
    """Download the soft-kept pre-conversion original (*.lfs_original)."""
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    path = None
    if row.get("original_path"):
        path = Path(row["original_path"])
    if path is None or not path.is_file():
        cur = Path(row["path"]) if row.get("path") else None
        if cur is not None and cur.parent.is_dir():
            stem = cur.stem
            for cand in cur.parent.iterdir():
                if cand.name.endswith(".lfs_original") and cand.name.startswith(stem):
                    if cand.is_file():
                        path = cand
                        break
    if path is None or not path.is_file():
        raise HTTPException(
            404,
            "Original file not found (may not have been converted, or soft-keep missing)",
        )
    download_name = path.name
    if download_name.endswith(".lfs_original"):
        download_name = download_name[: -len(".lfs_original")]
    return FileResponse(
        path,
        filename=download_name,
        media_type="application/octet-stream",
    )






@router.delete("/api/media/{media_id}/soft-original")
def delete_soft_original(media_id: int, request: Request):
    """Permanently delete the soft-kept pre-conversion original for one media."""
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    path = None
    if row.get("original_path"):
        path = Path(row["original_path"])
    if path is None or not path.is_file():
        # heuristic
        cur = Path(row["path"]) if row.get("path") else None
        if cur is not None and cur.parent.is_dir():
            stem = cur.stem
            for cand in cur.parent.iterdir():
                if cand.name.endswith(".lfs_original") and cand.name.startswith(stem):
                    if cand.is_file():
                        path = cand
                        break
    if path is None or not path.is_file():
        raise HTTPException(404, "Soft-kept original not found on disk")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(500, f"Could not delete original: {exc}") from exc
    with db.connect() as conn:
        conn.execute("UPDATE media SET original_path=NULL WHERE id=?", (media_id,))
    return {"ok": True, "deleted": str(path)}




@router.post("/api/media/{media_id}/soft-original/restore")
async def restore_soft_original(media_id: int, request: Request):
    """Restore *.lfs_original over the converted file and point the DB at the original.

    The converted MP4 (if different) is renamed to ``*.lfs_converted`` as a backup.
    """
    _require_csrf(request)
    original_path = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            original_path = body.get("original_path")
    except Exception:
        pass
    row, soft = _resolve_soft_original(media_id, original_path)
    target = _restored_path_from_soft(soft)
    converted = Path(row["path"]) if row.get("path") else None
    backup: Path | None = None

    try:
        # If something already sits at the restored name and it is the converted file,
        # move it aside; if it is a different file, also move aside.
        if target.is_file() and target.resolve() != soft.resolve():
            backup = Path(str(target) + ".lfs_converted")
            n = 1
            while backup.exists():
                backup = Path(str(target) + f".lfs_converted.{n}")
                n += 1
            os.replace(str(target), str(backup))
        elif (
            converted is not None
            and converted.is_file()
            and converted.resolve() != soft.resolve()
            and converted.resolve() != target.resolve()
        ):
            # Converted lives at a different path (e.g. .mov → .mp4)
            backup = Path(str(converted) + ".lfs_converted")
            n = 1
            while backup.exists():
                backup = Path(str(converted) + f".lfs_converted.{n}")
                n += 1
            os.replace(str(converted), str(backup))

        os.replace(str(soft), str(target))
    except OSError as exc:
        raise HTTPException(500, f"Could not restore original: {exc}") from exc

    try:
        st = target.stat()
        size = int(st.st_size)
        mtime_ns = int(st.st_mtime_ns)
    except OSError:
        size = row.get("size") or 0
        mtime_ns = row.get("mtime_ns") or 0

    with db.connect() as conn:
        conn.execute(
            """UPDATE media SET path=?, name=?, size=?, mtime_ns=?,
               original_path=NULL, missing=0 WHERE id=?""",
            (str(target), target.name, size, mtime_ns, media_id),
        )
    return {
        "ok": True,
        "restored_path": str(target),
        "backup_path": str(backup) if backup else None,
        "media_id": media_id,
    }



@router.delete("/api/media/{media_id}")
def soft_delete_media(media_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    # People who used a face on this media as representative need a new tile
    person_ids = [
        r["person_id"]
        for r in db.all(
            """SELECT DISTINCT person_id FROM faces
               WHERE media_id=? AND person_id IS NOT NULL AND deleted_at IS NULL""",
            (media_id,),
        )
    ]
    with db.connect() as conn:
        conn.execute("UPDATE media SET deleted_at=? WHERE id=?", (_now(), media_id))
        if person_ids:
            try:
                cluster.refresh(conn, person_ids)
            except Exception:
                # Lightweight rep fix if full refresh fails
                for pid in person_ids:
                    r = conn.execute(
                        """SELECT f.id FROM faces f
                           JOIN media m ON m.id=f.media_id
                           WHERE f.person_id=? AND f.deleted_at IS NULL
                             AND m.deleted_at IS NULL AND m.missing=0
                           ORDER BY CASE m.kind WHEN 'photo' THEN 0 ELSE 1 END,
                                    CASE f.review_state WHEN 'confirmed' THEN 0 ELSE 1 END,
                                    COALESCE(f.quality, 0) DESC, f.detection DESC, f.id
                           LIMIT 1""",
                        (pid,),
                    ).fetchone()
                    conn.execute(
                        "UPDATE people SET representative_face_id=? WHERE id=?",
                        (r["id"] if r else None, pid),
                    )
    cluster.invalidate()
    return {"ok": True}


@router.post("/api/media/purge")
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


@router.post("/api/media/{media_id}/restore")
def restore_media(media_id: int, request: Request):
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    with db.connect() as conn:
        conn.execute("UPDATE media SET deleted_at=NULL WHERE id=?", (media_id,))
    cluster.invalidate()
    return _media_row(db.one("SELECT * FROM media WHERE id=?", (media_id,)))


@router.post("/api/media/empty-deleted")
def empty_deleted(body: ConfirmBody, request: Request):
    """Permanently delete all soft-deleted media from disk and remove their index records."""
    _require_csrf(request)
    if body.confirm != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm permanent deletion of all deleted files.")
    rows = db.all("SELECT id, path FROM media WHERE deleted_at IS NOT NULL")
    if not rows:
        return {"deleted_files": 0, "removed_from_index": 0, "failed": []}

    deleted_files = 0
    removed_from_db = 0
    failed: list[dict] = []

    for row in rows:
        mid = row["id"]
        path = Path(row["path"])
        try:
            resolved = path.expanduser().resolve()
            allowed = any(resolved == r or resolved.is_relative_to(r) for r in config.roots)
            if path.is_file():
                if not allowed:
                    failed.append({"id": mid, "path": row["path"], "error": "Path outside allowed roots"})
                    continue
                path.unlink()
                deleted_files += 1
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
@router.post("/api/media/{media_id}/reveal")
def reveal_media(media_id: int, request: Request):
    """Reveal file in system file manager (Finder / Explorer / xdg-open parent)."""
    _require_csrf(request)
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(404, "File not found on disk")
    import platform
    import subprocess
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)], start_new_session=True)
        elif system == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)], start_new_session=True)
        else:
            subprocess.Popen(["xdg-open", str(path.parent)], start_new_session=True)
    except Exception as exc:
        raise HTTPException(500, f"Could not reveal file: {exc}") from exc
    return {"ok": True, "path": str(path)}


@router.post("/api/media/move")
def move_media_files(body: MoveMediaBody, request: Request):
    """Move selected media files to a destination folder (user-initiated only)."""
    _require_csrf(request)
    if not body.media_ids:
        raise HTTPException(400, "media_ids required")
    dest_root = Path(body.destination).expanduser().resolve()
    try:
        authorized_root(str(dest_root), config.roots)
    except Exception:
        # Allow destinations under home even if not a library root
        home = Path.home().resolve()
        if not str(dest_root).startswith(str(home)):
            raise HTTPException(400, "Destination must be under an allowed root or home")
    dest_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    failed: list[dict] = []
    moved_ids: list[int] = []
    for mid in body.media_ids:
        row = db.one("SELECT * FROM media WHERE id=? AND deleted_at IS NULL", (mid,))
        if not row:
            failed.append({"id": mid, "error": "not found"})
            continue
        src = Path(row["path"])
        if not src.is_file():
            failed.append({"id": mid, "error": "missing on disk"})
            continue
        target = dest_root / src.name
        if target.exists():
            stem, suf = target.stem, target.suffix
            n = 1
            while target.exists():
                target = dest_root / f"{stem}_{n}{suf}"
                n += 1
        try:
            shutil.move(str(src), str(target))
            with db.connect() as conn:
                conn.execute(
                    "UPDATE media SET path=?, name=? WHERE id=?",
                    (str(target), target.name, mid),
                )
            moved += 1
            moved_ids.append(mid)
        except Exception as exc:
            failed.append({"id": mid, "error": str(exc)})
    return {"moved": moved, "failed": failed, "destination": str(dest_root), "moved_ids": moved_ids}
