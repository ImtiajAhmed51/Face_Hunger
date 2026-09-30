"""DINOv2 near-duplicate detection for photos and videos.

Exact duplicates are found via content_hash (SHA-256).
Near-duplicates use cosine similarity on DINOv2 vectors from the active visual
embedding space, with FAISS when installed (optional) and NumPy otherwise.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Optional

import numpy as np

from . import dino

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY = 0.92  # cosine; configurable via settings


def group_fingerprint(media_ids) -> str:
    """Stable key for a duplicate group: sorted unique media ids, comma-separated."""
    return ",".join(str(i) for i in sorted({int(x) for x in media_ids}))


def load_ignored_group_keys(db) -> set:
    rows = db.all("SELECT group_key FROM ignored_duplicate_groups")
    return {r["group_key"] for r in rows}



def _try_faiss():
    try:
        import faiss

        return faiss
    except ImportError:
        return None


def backfill_embeddings(db, space, embedder, *, limit: int = 50, checkpoint: Optional[Callable] = None) -> dict:
    """Incrementally compute DINOv2 embeddings for media missing them in ``space``."""
    from .vectors.spaces import run_backfill

    errors: list[str] = []
    filled = failed = 0
    if space is None or embedder is None:
        errors.append(dino.status().get("error") or "DINOv2 model not installed")
    else:
        result = run_backfill(space, embedder, max_items=limit, checkpoint=checkpoint or (lambda: None))
        filled, failed = result["embedded"], result["failed"]
        if failed:
            errors = [f"{r['name']}: {r['last_error']}" for r in db.all(
                """SELECT m.name, q.last_error FROM embedding_queue q JOIN media m ON m.id=q.media_id
                   WHERE q.model_key=? AND q.last_error IS NOT NULL ORDER BY q.rowid DESC LIMIT 20""",
                (space.key,))]
    remaining = 0
    if space is not None:
        cov = space.coverage()
        remaining = cov["total"] - cov["filled"]
    return {
        "filled": filled,
        "failed": failed,
        "remaining": int(remaining),
        "errors": errors,
        "dino": dino.status(),
    }


def _load_vectors(db, space) -> tuple[list[dict], np.ndarray]:
    if space is None:
        return [], np.zeros((0, 1), dtype=np.float32)
    rows = db.all(
        """SELECT m.id, m.path, m.name, m.kind, m.size, m.width, m.height, m.duration,
                  m.content_hash, m.thumbnail, m.captured_at,
                  m.status, m.missing, m.deleted_at, m.phash, v.offset AS v_offset, v.sha AS v_sha
           FROM media m JOIN media_vectors v ON v.media_id=m.id AND v.model_key=?
           WHERE m.deleted_at IS NULL AND m.missing=0
             AND m.status IN ('indexed','stale')
           ORDER BY m.id""",
        (space.key,),
    )
    items = []
    vectors = []
    for r in rows:
        try:
            vec = space.file.read(r.pop("v_offset"), r.pop("v_sha"))
            items.append(r)
            vectors.append(vec / max(float(np.linalg.norm(vec)), 1e-12))
        except Exception:
            continue
    if not vectors:
        return [], np.zeros((0, space.spec.dim), dtype=np.float32)
    return items, np.stack(vectors, axis=0).astype(np.float32)


def find_duplicate_groups(
    db,
    space,
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
    items, matrix = _load_vectors(db, space)
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

    def link(i: int, j: int, s: float) -> None:
        if j < 0 or j == i or s < threshold:
            return
        a, b = (i, j) if i < j else (j, i)
        union(a, b)
        pair_sim[(a, b)] = s
        max_sim[a] = max(max_sim[a], s)
        max_sim[b] = max(max_sim[b], s)

    faiss = _try_faiss()
    k = min(32, n)
    if faiss is not None and n >= 32:
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        sims, idxs = index.search(matrix, k)
        for i in range(n):
            for j_pos in range(1, k):  # skip self at 0
                link(i, int(idxs[i, j_pos]), float(sims[i, j_pos]))
    elif n > 2048:
        # Top-k neighbours with usearch's exact SIMD search; bounded memory, no O(n^2) matrix.
        from usearch.index import search as usearch_search

        matches = usearch_search(matrix, matrix, k, "cos", exact=True, threads=0)
        keys, dists = np.asarray(matches.keys), np.asarray(matches.distances)
        for i in range(n):
            for j_pos in range(k):
                link(i, int(keys[i, j_pos]), 1.0 - float(dists[i, j_pos]))
    else:
        # Dense cosine (matrix is L2-normalized -> IP = cosine)
        sims = matrix @ matrix.T
        rows, cols = np.nonzero(np.triu(sims >= threshold, k=1))
        for i, j in zip(rows.tolist(), cols.tolist()):
            link(i, j, float(sims[i, j]))

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

    ignored = load_ignored_group_keys(db)
    filtered = []
    for g in groups:
        ids = [it["id"] for it in g.get("items") or []]
        fp = group_fingerprint(ids)
        g["fingerprint"] = fp
        if fp not in ignored:
            filtered.append(g)
    return filtered[:limit]
