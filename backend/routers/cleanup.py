"""Cleanup API routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..deps import cluster, db
from ..schemas import BackfillBody, SeparateBody
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

    soft_originals = _list_soft_originals()
    counts["soft_originals"] = len(soft_originals)
    converted_backups = _list_converted_backups()
    counts["converted_backups"] = len(converted_backups)

    return {
        **counts,
        "possible_people": possible,
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
    from . import duplicates as dup_mod
    from .media_processing import frame_at, load_image

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
