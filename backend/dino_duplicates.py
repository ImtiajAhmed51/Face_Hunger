"""DINOv2 + FAISS near-duplicate detection for photos and videos.

Exact duplicates are found via content_hash (SHA-256).
Near-duplicates use cosine similarity on cached DINOv2 embeddings with FAISS
when available, otherwise a pure-NumPy path for small libraries.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import dino
from .media_embeddings import MediaEmbeddingStore

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY = 0.92  # cosine; configurable via settings


def _try_faiss():
    try:
        import faiss

        return faiss
    except ImportError:
        return None


def embed_media(
    path: Path,
    kind: str,
    duration: Optional[float],
    frame_at_fn,
    store: MediaEmbeddingStore,
) -> tuple[int, str]:
    """Compute DINOv2 embedding and append to store. Returns (offset, sha)."""
    if kind == "video":
        vec = dino.embed_video_path(path, duration, frame_at_fn)
    else:
        vec = dino.embed_image_path(path)
    return store.append(vec)


def backfill_embeddings(
    db,
    store: MediaEmbeddingStore,
    frame_at_fn,
    *,
    limit: int = 50,
    checkpoint: Optional[Callable] = None,
) -> dict:
    """Incrementally compute DINOv2 embeddings for media missing them."""
    rows = db.all(
        """SELECT id, path, kind, duration, dino_offset, dino_sha
           FROM media
           WHERE deleted_at IS NULL AND missing=0
             AND status IN ('indexed','stale')
             AND (dino_offset IS NULL OR dino_sha IS NULL)
           ORDER BY id LIMIT ?""",
        (limit,),
    )
    filled = 0
    failed = 0
    errors: list[str] = []
    for row in rows:
        if checkpoint:
            checkpoint()
        path = Path(row["path"])
        if not path.is_file():
            failed += 1
            continue
        try:
            offset, sha = embed_media(
                path, row["kind"], row.get("duration"), frame_at_fn, store
            )
            with db.connect() as conn:
                conn.execute(
                    "UPDATE media SET dino_offset=?, dino_sha=? WHERE id=?",
                    (offset, sha, row["id"]),
                )
            filled += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            if len(errors) > 20:
                errors = errors[:20]
    remaining = db.one(
        """SELECT COUNT(*) AS c FROM media
           WHERE deleted_at IS NULL AND missing=0
             AND status IN ('indexed','stale')
             AND (dino_offset IS NULL OR dino_sha IS NULL)"""
    )
    return {
        "filled": filled,
        "failed": failed,
        "remaining": int((remaining or {}).get("c") or 0),
        "errors": errors,
        "dino": dino.status(),
    }


def _load_vectors(db, store: MediaEmbeddingStore) -> tuple[list[dict], np.ndarray]:
    rows = db.all(
        """SELECT id, path, name, kind, size, width, height, duration,
                  content_hash, dino_offset, dino_sha, thumbnail, captured_at,
                  status, missing, deleted_at, phash
           FROM media
           WHERE deleted_at IS NULL AND missing=0
             AND status IN ('indexed','stale')
             AND dino_offset IS NOT NULL AND dino_sha IS NOT NULL
           ORDER BY id"""
    )
    items = []
    vectors = []
    for r in rows:
        try:
            vec = store.read(r["dino_offset"], r["dino_sha"])
            items.append(r)
            vectors.append(vec)
        except Exception:
            continue
    if not vectors:
        return [], np.zeros((0, dino.DINO_DIM), dtype=np.float32)
    return items, np.stack(vectors, axis=0).astype(np.float32)


def find_duplicate_groups(
    db,
    store: MediaEmbeddingStore,
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY,
    limit: int = 2000,
    media_row_fn: Optional[Callable] = None,
) -> list[dict]:
    """Return exact + near-duplicate groups.

    Exact groups share content_hash.
    Near groups are connected components of cosine sim >= threshold.
    """
    threshold = float(similarity_threshold)
    if not 0.5 <= threshold <= 0.999:
        threshold = DEFAULT_SIMILARITY

    all_rows = db.all(
        """SELECT id, path, name, kind, size, width, height, duration,
                  content_hash, dino_offset, dino_sha, thumbnail, captured_at,
                  status, missing, deleted_at, phash
           FROM media
           WHERE deleted_at IS NULL AND missing=0
             AND status IN ('indexed','stale')
           ORDER BY id"""
    )
    if not all_rows:
        return []

    def serialize(row, similarity: Optional[float] = None):
        base = media_row_fn(row) if media_row_fn else dict(row)
        if similarity is not None:
            base["similarity"] = round(float(similarity) * 100, 1)
        return base

    groups: list[dict] = []
    seen: set[int] = set()

    # --- Exact by content_hash ---
    by_hash: dict[str, list] = defaultdict(list)
    for r in all_rows:
        h = r.get("content_hash")
        if h:
            by_hash[h].append(r)
    for h, items in by_hash.items():
        if len(items) < 2:
            continue
        for it in items:
            seen.add(it["id"])
        groups.append(
            {
                "type": "exact",
                "key": h[:20],
                "similarity": 100.0,
                "items": [serialize(it, 100.0) for it in sorted(items, key=lambda x: x["id"])],
            }
        )

    # --- Near-duplicates via DINOv2 ---
    items, matrix = _load_vectors(db, store)
    # Filter out already exact-grouped
    keep_idx = [i for i, it in enumerate(items) if it["id"] not in seen]
    if len(keep_idx) < 2:
        return groups[:limit]

    items = [items[i] for i in keep_idx]
    matrix = matrix[keep_idx]

    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Pairwise similarities (store max sim to seed for display)
    max_sim: dict[int, float] = {i: 0.0 for i in range(n)}
    pair_sim: dict[tuple[int, int], float] = {}

    faiss = _try_faiss()
    if faiss is not None and n >= 32:
        index = faiss.IndexFlatIP(dino.DINO_DIM)
        index.add(matrix)
        # Search k neighbours; k grows slowly with n
        k = min(32, n)
        sims, idxs = index.search(matrix, k)
        for i in range(n):
            for j_pos in range(1, k):  # skip self at 0
                j = int(idxs[i, j_pos])
                if j < 0 or j <= i:
                    continue
                s = float(sims[i, j_pos])
                if s >= threshold:
                    union(i, j)
                    pair_sim[(i, j)] = s
                    max_sim[i] = max(max_sim[i], s)
                    max_sim[j] = max(max_sim[j], s)
    else:
        # Dense cosine (matrix is L2-normalized → IP = cosine)
        sims = matrix @ matrix.T
        for i in range(n):
            for j in range(i + 1, n):
                s = float(sims[i, j])
                if s >= threshold:
                    union(i, j)
                    pair_sim[(i, j)] = s
                    max_sim[i] = max(max_sim[i], s)
                    max_sim[j] = max(max_sim[j], s)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[find(i)].append(i)

    for root, members in components.items():
        if len(members) < 2:
            continue
        # Representative similarity = max edge in component (or avg of maxes)
        group_sim = max((max_sim[m] for m in members), default=threshold)
        group_items = []
        for m in sorted(members, key=lambda x: items[x]["id"]):
            group_items.append(serialize(items[m], max(max_sim[m], group_sim) * 100 if max_sim[m] else group_sim * 100))
        # Normalize similarity display to 0–100
        for gi in group_items:
            if "similarity" in gi and gi["similarity"] <= 1.0:
                gi["similarity"] = round(gi["similarity"] * 100, 1)
        groups.append(
            {
                "type": "near",
                "key": f"dino-{items[members[0]]['id']}",
                "similarity": round(group_sim * 100, 1),
                "items": group_items,
            }
        )
        if len(groups) >= limit:
            break

    # Prefer exact first, then higher similarity
    groups.sort(key=lambda g: (0 if g["type"] == "exact" else 1, -(g.get("similarity") or 0)))
    return groups[:limit]