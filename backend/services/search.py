"""Hybrid search: SQL filters + vector signals fused with weighted RRF.

Signals (each optional, each a ranked list of media ids):
  text           SigLIP 2 text embedding -> media image vectors
  similar_media  visual (DINOv2) vector of a media item -> media vectors
  similar_face   ArcFace vector of a face -> faces of the same identity
  recency        newest first (only when weighted > 0)

Filters (people, date range, kind, face-quality threshold, deleted) are pure
SQL and restrict every signal. When the filtered set is small the vector
signals are scored exactly over it; otherwise the ANN top-k is intersected
with it, widening k until a page can be filled.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

import numpy as np

from ..ops import diagnostics

logger = logging.getLogger(__name__)

RRF_K = 60
EXACT_LIMIT = 5000  # filtered sets up to this size are scored exactly
DEFAULT_WEIGHTS = {"text": 1.0, "similar_media": 1.0, "similar_face": 1.0, "recency": 0.0,
                   "expansion": 0.5, "caption": 0.7, "label": 0.7, "plugin": 0.5}
MIN_TEXT_SIM = 0.0  # SigLIP similarities are low in absolute terms; ranking matters, not the value


class SearchError(ValueError):
    pass


def build_filters(q: dict) -> tuple[list[str], list[Any]]:
    where = ["m.deleted_at IS NULL" if not q.get("deleted") else "m.deleted_at IS NOT NULL", "m.missing = 0"]
    params: list[Any] = []
    if q.get("kind") in ("photo", "video"):
        where.append("m.kind = ?")
        params.append(q["kind"])
    if q.get("date_from"):
        where.append("m.captured_at >= ?")
        params.append(q["date_from"])
    if q.get("date_to"):
        d = q["date_to"]
        where.append("m.captured_at <= ?")
        params.append(d + "T23:59:59" if len(d) == 10 else d)
    if (q.get("name") or "").strip():
        where.append("m.name LIKE ?")
        params.append(f"%{q['name'].strip()}%")
    active_face = ("f.media_id = m.id AND f.deleted_at IS NULL AND f.review_state != 'rejected' "
                   "AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.media_id=f.media_id AND e.person_id=f.person_id)")
    people = [int(p) for p in q.get("people") or []]
    if people:
        ph = ",".join("?" * len(people))
        if (q.get("people_mode") or "ANY").upper() == "ALL":
            where.append(f"(SELECT COUNT(DISTINCT f.person_id) FROM faces f WHERE {active_face} AND f.person_id IN ({ph})) = ?")
            params.extend([*people, len(set(people))])
        else:
            where.append(f"EXISTS (SELECT 1 FROM faces f WHERE {active_face} AND f.person_id IN ({ph}))")
            params.extend(people)
    excluded = [int(p) for p in q.get("exclude_people") or []]
    if excluded:
        ph = ",".join("?" * len(excluded))
        where.append(f"NOT EXISTS (SELECT 1 FROM faces f WHERE f.media_id = m.id AND f.deleted_at IS NULL "
                     f"AND f.review_state != 'rejected' AND f.person_id IN ({ph}))")
        params.extend(excluded)
    if q.get("min_quality") is not None:
        where.append("EXISTS (SELECT 1 FROM faces f WHERE f.media_id = m.id AND f.deleted_at IS NULL AND f.quality >= ?)")
        params.append(float(q["min_quality"]))
    return where, params


class HybridSearch:
    def __init__(self, services):
        self.s = services
        self._text_cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._face_sync: Optional[threading.Thread] = None

    # -- signal sources -----------------------------------------------------
    def _text_vector(self, text: str):
        encoder = self.s.models.text_encoder()
        if encoder is None:
            return None, None, "Text search needs SigLIP 2; run scripts/fetch_models.py"
        space = self.s.vectors.active(encoder.spec.role, prefer=[encoder.spec.key])
        if space is None or not space.coverage()["filled"]:
            return None, None, "Text embeddings are still being computed for your library"
        key = (encoder.spec.key, text.strip().lower())
        with self._cache_lock:
            vec = self._text_cache.get(key)
            if vec is not None:
                self._text_cache.move_to_end(key)
        if vec is None:
            vec = encoder.embed_texts([text])[0]
            with self._cache_lock:
                self._text_cache[key] = vec
                while len(self._text_cache) > 256:
                    self._text_cache.popitem(last=False)
        return space, vec, None

    def _media_vector(self, media_id: int, key: Optional[str] = None):
        space = self.s.vectors.get(key) if key else self.s.vectors.active("visual")
        if space is None:
            return None, None, "Visual similarity needs DINOv2 embeddings"
        vec = space.vector(int(media_id))
        if vec is None:
            return None, None, f"Media {media_id} has no visual embedding yet"
        return space, vec, None

    def _ranked_from_space(self, space, vec, allowed: Optional[np.ndarray], want: int,
                           filter_sql: Optional[tuple[str, list]] = None) -> list[tuple[int, float]]:
        space.ensure_ready()
        if allowed is not None:
            keys, sims = space.ann.exact(vec, allowed)
            return list(zip(keys.tolist(), sims.tolist()))
        k = max(want, 200)
        while True:
            keys, sims = space.ann.search(vec, k)
            pairs = [(int(a), float(b)) for a, b in zip(keys, sims)]
            if filter_sql is not None:
                alive = self._alive([p[0] for p in pairs], *filter_sql)
                pairs = [p for p in pairs if p[0] in alive]
            if len(pairs) >= want or k >= len(space.ann):
                return pairs
            k *= 4

    def _face_ranked(self, face_id: int, allowed: Optional[np.ndarray], want: int):
        from ..vectors.specs import FACE_ARCFACE

        row = self.s.db.one("SELECT media_id FROM faces WHERE id=?", (int(face_id),))
        if not row:
            return None, f"Face {face_id} not found"
        space = self.s.vectors.get(FACE_ARCFACE.key)
        vec = space.vector(int(face_id))
        if space._ready:
            keys, sims = space.ann.search(vec, max(want * 4, 2000))
        else:
            keys, sims = self._face_bruteforce(vec, max(want * 4, 2000))
            self._start_face_sync(space)
        if not len(keys):
            return [], None
        media = {}
        rows = self.s.db.all(
            f"SELECT id, media_id FROM faces WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(keys))})",
            tuple(int(k) for k in keys))
        by_face = {r["id"]: r["media_id"] for r in rows}
        allowed_set = set(allowed.tolist()) if allowed is not None else None
        for key, sim in zip(keys.tolist(), sims.tolist()):
            mid = by_face.get(int(key))
            if mid is None or (allowed_set is not None and mid not in allowed_set):
                continue
            if sim > media.get(mid, -2):
                media[mid] = sim
        return sorted(media.items(), key=lambda kv: -kv[1]), None

    def _face_bruteforce(self, vec, k):
        """Exact scan over embeddings.bin while the face ANN index is being built."""
        store = self.s.store
        n = store.path.stat().st_size // (512 * 4)
        if not n:
            return np.zeros(0, np.int64), np.zeros(0, np.float32)
        mm = np.memmap(store.path, dtype="<f4", mode="r", shape=(n, 512))
        sims = np.empty(n, np.float32)
        for start in range(0, n, 65536):
            sims[start:start + 65536] = mm[start:start + 65536] @ vec
        k = min(k, n)
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        offsets = (top * 512 * 4).tolist()
        rows = self.s.db.all(
            f"SELECT id, embedding_offset FROM faces WHERE embedding_offset IN ({','.join('?' * len(offsets))})",
            tuple(offsets))
        by_offset = {r["embedding_offset"]: r["id"] for r in rows}
        pairs = [(by_offset[o], float(sims[i])) for i, o in zip(top.tolist(), offsets) if o in by_offset]
        return np.array([p[0] for p in pairs], np.int64), np.array([p[1] for p in pairs], np.float32)

    def _start_face_sync(self, space):
        if self._face_sync is None or not self._face_sync.is_alive():
            self._face_sync = threading.Thread(target=space.sync, name="face-index-sync", daemon=True)
            self._face_sync.start()

    # -- main entry ---------------------------------------------------------
    def run(self, q: dict) -> dict:
        with diagnostics.span("search", "hybrid") as timing:
            result = self._run(q)
            timing.set(results=len(result["items"]), signals=len(result["signals"]))
            return result

    def _run(self, q: dict) -> dict:
        started = time.perf_counter()
        page = max(1, int(q.get("page") or 1))
        limit = max(1, min(int(q.get("limit") or 60), 200))
        weights = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (q.get("weights") or {}).items() if k in DEFAULT_WEIGHTS}}
        where, params = build_filters(q)
        where_sql = " AND ".join(where)
        warnings: list[str] = []
        want = page * limit

        text = (q.get("text") or "").strip()
        sim_media = q.get("similar_media_id")
        sim_face = q.get("similar_face_id")
        vector_signals = bool(text) or sim_media is not None or sim_face is not None or bool(q.get("expansions"))
        has_filters = len(where) > 2 or bool(q.get("deleted"))

        if not vector_signals:
            return self._filter_only(where_sql, params, page, limit, started, weights)

        # Materialise the filtered set only when it is selective; broad filters are
        # applied after the ANN search instead (see _alive).
        allowed = None
        if has_filters:
            ids = [r["id"] for r in self.s.db.all(
                f"SELECT m.id FROM media m WHERE {where_sql} LIMIT ?", (*params, EXACT_LIMIT + 1))]
            if len(ids) <= EXACT_LIMIT:
                allowed = np.array(ids, dtype=np.int64)

        broad = (where_sql, params) if allowed is None else None
        ranked: dict[str, list[tuple[int, float]]] = {}
        if text:
            space, vec, warn = self._text_vector(text)
            if warn:
                warnings.append(warn)
            else:
                ranked["text"] = self._ranked_from_space(space, vec, allowed, max(want * 3, 300), broad)
        # Alternative phrasings (from the optional query rewriter): extra, lower-weight text signals.
        for index, phrase in enumerate([e.strip() for e in (q.get("expansions") or []) if e and e.strip()][:4]):
            space, vec, warn = self._text_vector(phrase)
            if not warn:
                ranked[f"expansion_{index}"] = self._ranked_from_space(space, vec, allowed, max(want * 3, 300), broad)
        if text:
            caption_hits = self._caption_ranked(text, allowed, max(want * 3, 300))
            if caption_hits:
                ranked["caption"] = caption_hits
            label_hits = self.s.plugins.label_ranked(text, allowed, max(want * 3, 300))
            if label_hits:
                ranked["label"] = label_hits
        if sim_media is not None:
            space, vec, warn = self._media_vector(int(sim_media), (q.get("space_overrides") or {}).get("visual"))
            if warn:
                warnings.append(warn)
            else:
                pairs = self._ranked_from_space(space, vec, allowed, max(want * 3, 300) + 1, broad)
                ranked["similar_media"] = [p for p in pairs if p[0] != int(sim_media)]
        if sim_face is not None:
            pairs, warn = self._face_ranked(int(sim_face), allowed, max(want * 3, 300))
            if warn:
                warnings.append(warn)
            else:
                ranked["similar_face"] = pairs

        if not ranked:
            result = self._filter_only(where_sql, params, page, limit, started, weights)
            result["warnings"] = warnings
            return result

        # Vector hits may include deleted/missing items when no filter set was materialised.
        candidates = {mid for pairs in ranked.values() for mid, _ in pairs}
        if allowed is None and candidates:
            alive = self._alive(candidates, where_sql, params)
            ranked = {name: [p for p in pairs if p[0] in alive] for name, pairs in ranked.items()}
            candidates = alive
        if text and candidates and weights["plugin"] > 0 and self.s.plugins.enabled_with("search_signal"):
            # Enabled search-signal plugins re-rank the top candidates (isolated, 2 s budget, failures only warn).
            top = dict.fromkeys(mid for pairs in ranked.values() for mid, _ in pairs[:300] if mid in candidates)
            extra, plugin_warnings = self.s.plugins.search_signals(text, list(top))
            ranked.update(extra)
            warnings.extend(plugin_warnings)
        if weights["recency"] > 0 and candidates:
            order = self.s.db.all(
                f"SELECT id FROM media WHERE id IN ({','.join('?' * len(candidates))}) "
                "ORDER BY COALESCE(captured_at, indexed_at) DESC, id DESC", tuple(candidates))
            ranked["recency"] = [(r["id"], 0.0) for r in order]

        scores: dict[int, float] = {}
        detail: dict[int, dict] = {}
        for name, pairs in ranked.items():
            w = weights.get("expansion" if name.startswith("expansion_") else "plugin" if name.startswith("plugin_") else name, 1.0)
            if w <= 0:
                continue
            for rank, (mid, sim) in enumerate(pairs, start=1):
                scores[mid] = scores.get(mid, 0.0) + w / (RRF_K + rank)
                detail.setdefault(mid, {})[name] = {"rank": rank, "similarity": round(sim, 4)}
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], -kv[0]))
        page_ids = ordered[(page - 1) * limit: page * limit]
        items = serialize_media(self.s.db, [mid for mid, _ in page_ids])
        for item, (mid, score) in zip(items, page_ids):
            item["score"] = round(score, 6)
            item["signals"] = detail.get(mid, {})
            best = max((d["similarity"] for n, d in item["signals"].items() if n != "recency"), default=None)
            if best is not None:
                item["similarity"] = best
        return {"items": items, "total": len(ordered), "page": page, "limit": limit,
                "signals": sorted(ranked), "weights": weights, "warnings": warnings,
                "took_ms": round((time.perf_counter() - started) * 1000, 1)}

    def _caption_ranked(self, text: str, allowed: Optional[np.ndarray], want: int) -> list[tuple[int, float]]:
        """Full-text match on generated captions/tags (only exists when the VLM has written some)."""
        import re

        words = [w for w in re.findall(r"[\w']+", text.lower()) if len(w) > 2][:8]
        if not words:
            return []
        try:
            rows = self.s.db.all("SELECT rowid AS media_id, bm25(caption_fts) AS score FROM caption_fts WHERE caption_fts MATCH ? "
                                 "ORDER BY score LIMIT ?", (" OR ".join(f'"{w}"' for w in words), want * 2))
        except Exception:
            return []
        keep = set(allowed.tolist()) if allowed is not None else None
        return [(r["media_id"], float(-r["score"])) for r in rows if keep is None or r["media_id"] in keep][:want]

    def _alive(self, ids, where_sql, params) -> set[int]:
        ids = list(ids)
        alive: set[int] = set()
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            rows = self.s.db.all(
                f"SELECT m.id FROM media m WHERE {where_sql} AND m.id IN ({','.join('?' * len(chunk))})",
                (*params, *chunk))
            alive.update(r["id"] for r in rows)
        return alive

    def _filter_only(self, where_sql, params, page, limit, started, weights) -> dict:
        total = int(self.s.db.one(f"SELECT COUNT(*) c FROM media m WHERE {where_sql}", tuple(params))["c"])
        rows = self.s.db.all(
            f"SELECT m.id FROM media m WHERE {where_sql} ORDER BY COALESCE(m.captured_at, m.indexed_at) DESC, m.id DESC "
            "LIMIT ? OFFSET ?", (*params, limit, (page - 1) * limit))
        items = serialize_media(self.s.db, [r["id"] for r in rows])
        return {"items": items, "total": total, "page": page, "limit": limit, "signals": ["recency"],
                "weights": weights, "warnings": [], "took_ms": round((time.perf_counter() - started) * 1000, 1)}


def serialize_media(db, ids: list[int]) -> list[dict]:
    """Batched, filesystem-free media rows compatible with the frontend Media type."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in db.all(f"SELECT * FROM media WHERE id IN ({ph})", tuple(ids))}
    counts = {r["media_id"]: r["c"] for r in db.all(
        f"SELECT media_id, COUNT(*) c FROM faces WHERE deleted_at IS NULL AND media_id IN ({ph}) GROUP BY media_id",
        tuple(ids))}
    favorites = {r["media_id"] for r in db.all(f"SELECT media_id FROM favorites WHERE media_id IN ({ph})", tuple(ids))}
    people: dict[int, list[dict]] = {}
    for r in db.all(
        f"""SELECT DISTINCT f.media_id, p.id, p.name FROM faces f JOIN people p ON p.id = f.person_id
            WHERE f.media_id IN ({ph}) AND f.deleted_at IS NULL AND f.review_state != 'rejected'
              AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.media_id=f.media_id AND e.person_id=f.person_id)
            ORDER BY p.id""", tuple(ids)):
        name = (r["name"] or "").strip() or f"Person {r['id']}"
        people.setdefault(r["media_id"], []).append({"id": r["id"], "display_name": name})
    out = []
    for mid in ids:
        row = rows.get(mid)
        if row is None:
            continue
        out.append({
            "id": row["id"], "name": row["name"], "path": row["path"], "kind": row["kind"],
            "captured_at": row["captured_at"], "width": row["width"], "height": row["height"],
            "duration": row["duration"], "size": int(row["size"] or 0), "status": row["status"] or "indexed",
            "missing": bool(row["missing"]), "deleted_at": row["deleted_at"],
            "face_count": int(counts.get(mid, 0)), "people": people.get(mid, []), "favorite": mid in favorites,
        })
    return out
