"""Cleanup API routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..deps import cluster, db, services, store
from ..schemas import AutoIdentityBody, BackfillBody, MergeConfidentBody, NonFaceBody, SeparateBody
from ..services.presenters import (
    _cleanup_counts,
    _media_row,
    _person_row,
    _require_csrf,
    _settings,
)
from ..services.soft_originals import _list_converted_backups, _list_soft_originals

router = APIRouter()

@router.get("/api/cleanup")
def get_cleanup():
    counts = _cleanup_counts()
    failed_media = [_media_row(r) for r in db.all(
        "SELECT * FROM media WHERE status='failed' AND deleted_at IS NULL ORDER BY id DESC LIMIT 50"
    )]
    missing_media = [_media_row(r) for r in db.all(
        "SELECT * FROM media WHERE missing=1 AND deleted_at IS NULL ORDER BY id DESC LIMIT 50"
    )]
    possible = []
    diagnostics = {}
    try:
        from ..merge_evidence import suggestions
        for pair in suggestions(db, store, cluster, _settings().get('matching_threshold', .48), diagnostics):
            possible.append({**pair, 'a': _person_row(pair['a']), 'b': _person_row(pair['b'])})
    except Exception:
        logging.getLogger(__name__).exception("Duplicate-people evidence scan failed")

    # Frontend "Duplicate candidates" count uses `duplicates` — show pair count there
    counts["duplicates"] = len(possible)

    soft_originals = _list_soft_originals()
    counts["soft_originals"] = len(soft_originals)
    converted_backups = _list_converted_backups()
    counts["converted_backups"] = len(converted_backups)

    return {
        **counts,
        "possible_people": possible,
        "duplicate_diagnostics": diagnostics,
        "failed_media": failed_media,
        "missing_media": missing_media,
        "soft_originals": soft_originals,
        "converted_backups": converted_backups,
        "embedding_errors": None,
    }


@router.post("/api/cleanup/backfill-hashes")
def backfill_hashes(request: Request, body: BackfillBody = BackfillBody()):
    """Fill content_hash + phash for media missing them (no face re-detection)."""
    _require_csrf(request)
    from .. import duplicates as dup_mod
    from ..media_processing import frame_at, load_image

    limit = max(1, min(int(body.limit or 150), 500))
    rows = db.all(
        """SELECT id, path, kind, duration, content_hash, phash
           FROM media
           WHERE deleted_at IS NULL
             AND status IN ('indexed', 'stale')
             AND (content_hash IS NULL OR phash IS NULL)
           ORDER BY id
           LIMIT ?""",
        (limit,),
    )
    updated = 0
    failed = 0
    for row in rows:
        path = Path(row["path"])
        if not path.is_file():
            failed += 1
            continue
        content_hash = row.get("content_hash")
        phash = row.get("phash")
        try:
            if not content_hash:
                content_hash = dup_mod.content_hash(path)
            if not phash:
                if row["kind"] == "photo":
                    bgr = load_image(path)
                    phash = dup_mod.image_phash(bgr)
                else:
                    phash = dup_mod.video_phash(
                        path,
                        row.get("duration"),
                        frame_at_fn=frame_at,
                    )
            with db.connect() as conn:
                conn.execute(
                    "UPDATE media SET content_hash=?, phash=? WHERE id=?",
                    (content_hash, phash, row["id"]),
                )
            updated += 1
        except Exception:
            failed += 1

    total = int((db.one(
        "SELECT COUNT(*) AS c FROM media WHERE deleted_at IS NULL AND status IN ('indexed','stale')"
    ) or {}).get("c") or 0)
    filled = int((db.one(
        "SELECT COUNT(*) AS c FROM media WHERE deleted_at IS NULL AND status IN ('indexed','stale') AND content_hash IS NOT NULL AND phash IS NOT NULL"
    ) or {}).get("c") or 0)
    remaining = max(0, total - filled)

    return {
        "updated": updated,
        "failed": failed,
        "batch": len(rows),
        "filled": filled,
        "total": total,
        "remaining": remaining,
        "done": remaining == 0,
    }
@router.post("/api/cleanup/check")
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


@router.post("/api/cleanup/separate")
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


def _nonface_candidates(min_detection: float, min_size: int, min_quality: float):
    """Faces that are almost certainly not faces (or too tiny/weak to identify).

    Never touches faces the user has confirmed, created manually, or that belong to a named person.
    """
    import json

    import numpy as np

    from ..engine import Engine

    rows = db.all(
        """SELECT f.id, f.person_id, f.review_state, f.manual, f.detection, f.quality, f.bbox, f.landmarks,
                  m.width, m.height
           FROM faces f JOIN media m ON m.id=f.media_id
           LEFT JOIN people p ON p.id=f.person_id
           WHERE f.deleted_at IS NULL AND m.deleted_at IS NULL
             AND f.review_state='unreviewed' AND f.manual=0
             AND TRIM(COALESCE(p.name,''))=''""")
    found = []
    for row in rows:
        reason = None
        try:
            bbox = json.loads(row["bbox"])
            if float(row["detection"]) < min_detection:
                reason = "low detection score"
            elif min(bbox[2:]) < min_size:
                reason = "too small"
            elif float(row["quality"] if row["quality"] is not None else .5) < min_quality:
                reason = "very low quality"
            elif row["landmarks"] and not Engine._valid_landmarks(
                    np.asarray(json.loads(row["landmarks"])), bbox, row["width"] or 1e9, row["height"] or 1e9):
                reason = "landmarks do not form a face"
        except (ValueError, TypeError, KeyError, IndexError):
            reason = "unreadable record"
        if reason:
            found.append({"id": row["id"], "person_id": row["person_id"], "reason": reason})
    return found


@router.post("/api/cleanup/nonfaces")
def cleanup_nonfaces(body: NonFaceBody, request: Request):
    """Dry-run by default. With apply=true, mark likely non-faces as rejected (reversible via Undo)."""
    _require_csrf(request)
    found = _nonface_candidates(body.min_detection, body.min_size, body.min_quality)
    reasons: dict[str, int] = {}
    for item in found:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    result = {"found": len(found), "reasons": reasons, "sample_face_ids": [i["id"] for i in found[:24]],
              "applied": False, "audit_id": None}
    if not body.apply or not found:
        return result
    import json
    lib = services().library
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = [i["id"] for i in found]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            conn.execute(f"UPDATE faces SET review_state='rejected' WHERE id IN ({','.join('?' * len(chunk))}) "
                         "AND review_state='unreviewed' AND manual=0", chunk)
        people = sorted({i["person_id"] for i in found if i["person_id"] is not None})
        cluster.refresh(conn, people)
        entry = lib._log(conn, "cleanup.nonfaces", f"Rejected {len(ids)} likely non-face detection(s)",
                         {"face_ids": ids, "people": people}, len(ids))
    cluster.invalidate()
    return {**result, "applied": True, "audit_id": entry}


@router.post("/api/cleanup/merge-confident")
def cleanup_merge_confident(body: MergeConfidentBody, request: Request):
    """Merge only 'high' confidence duplicate-people pairs. Dry-run by default; every merge is undoable."""
    _require_csrf(request)
    from ..merge_evidence import suggestions
    pairs = [p for p in suggestions(db, store, cluster, _settings().get("matching_threshold", .48))
             if p["confidence"] == "high"]
    plan, used = [], set()
    for pair in pairs:
        a, b = pair["a"], pair["b"]
        if a["id"] in used or b["id"] in used:
            continue
        # Never auto-merge named people into each other; keep the named / larger one as the target.
        if (a.get("name") or "").strip() and (b.get("name") or "").strip():
            continue
        target, source = (a, b) if ((a.get("name") or "").strip() or a["face_count"] >= b["face_count"]) else (b, a)
        plan.append({"source": source["id"], "target": target["id"], "scores": pair["scores"]})
        used.update((a["id"], b["id"]))
        if len(plan) >= body.limit:
            break
    if not body.apply:
        return {"would_merge": len(plan), "plan": plan, "applied": False}
    audit_ids = []
    for item in plan:
        audit_ids.append(services().library.merge_people(item["source"], item["target"])["audit_id"])
    return {"would_merge": len(plan), "plan": plan, "applied": True, "audit_ids": audit_ids}


@router.post("/api/cleanup/auto-identity")
def cleanup_auto_identity(body: AutoIdentityBody, request: Request):
    """Let the system decide which People are the same individual. Dry-run unless apply=true."""
    _require_csrf(request)
    from .. import auto_identity
    return auto_identity.run(services(), level=body.level, apply=body.apply)
