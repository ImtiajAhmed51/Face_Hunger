"""Embedding spaces: one vector file + DB rows + ANN index per model key.

Media spaces store one vector per media item in ``media_vectors``. The face
space is a read-only view over the existing ``faces`` table and
``embeddings.bin`` (the indexing worker keeps writing those).

Crash safety: vectors are fsynced to the file before their row is committed,
and ``UNIQUE(model_key, media_id)`` makes re-running a batch idempotent. A
kill -9 therefore leaves at most some unreferenced bytes at the end of the
file (truncated on open when partial), never a duplicate or a dangling row.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, Sequence

import numpy as np

from ..ops import diagnostics
from .ann import AnnIndex
from .file import VectorFile, unit
from .specs import KNOWN, ModelSpec

logger = logging.getLogger(__name__)

ACTIVE_MEDIA = "m.deleted_at IS NULL AND m.missing=0 AND m.status IN ('indexed','stale')"
MAX_ATTEMPTS = 3
SAVE_EVERY = 2048


class Embedder(Protocol):
    spec: ModelSpec

    def embed_media(self, rows: Sequence[dict]) -> list:  # vector or Exception per row
        ...


class Space:
    def __init__(self, db, data_dir: Path, spec: ModelSpec, file_rel: str, *, owned: bool, dtype: str):
        self.db, self.spec, self.key = db, spec, spec.key
        self.owned = owned
        self.file = VectorFile(Path(data_dir) / file_rel, spec.dim, readonly=not owned)
        self.ann = AnnIndex(Path(data_dir) / "vectors" / f"{spec.slug}.usearch", spec.dim, dtype)
        self._ready = False
        self._sync_lock = threading.Lock()
        self._last_save = time.monotonic()
        self._coverage: Optional[tuple[float, dict]] = None

    # -- source-of-truth rows ------------------------------------------------
    def _rows_after(self, watermark: int, limit: int) -> list[dict]:
        if self.spec.subject == "face":
            return self.db.all(
                "SELECT id AS rid, id AS key, embedding_offset AS offset, embedding_sha AS sha FROM faces "
                "WHERE id>? ORDER BY id LIMIT ?", (watermark, limit))
        return self.db.all(
            "SELECT id AS rid, media_id AS key, offset, sha FROM media_vectors "
            "WHERE model_key=? AND id>? ORDER BY id LIMIT ?", (self.key, watermark, limit))

    def _db_keys(self) -> np.ndarray:
        if self.spec.subject == "face":
            rows = self.db.all("SELECT id AS k FROM faces")
        else:
            rows = self.db.all("SELECT media_id AS k FROM media_vectors WHERE model_key=?", (self.key,))
        return np.fromiter((r["k"] for r in rows), dtype=np.uint64, count=len(rows))

    def _face_signature(self, watermark: int) -> str:
        row = self.db.one("SELECT COUNT(*) c, COALESCE(SUM(embedding_offset),0) s FROM faces WHERE id<=?", (watermark,))
        return f"{row['c']}:{row['s']}"

    def count(self) -> int:
        if self.spec.subject == "face":
            return int(self.db.one("SELECT COUNT(*) c FROM faces")["c"])
        return int(self.db.one("SELECT COUNT(*) c FROM media_vectors WHERE model_key=?", (self.key,))["c"])

    def coverage(self, max_age: float = 5.0) -> dict:
        cached = self._coverage
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        value = self._coverage_now()
        self._coverage = (time.monotonic(), value)
        return value

    def _coverage_now(self) -> dict:
        if self.spec.subject == "face":
            total = int(self.db.one("SELECT COUNT(*) c FROM faces WHERE deleted_at IS NULL")["c"])
            return {"filled": total, "total": total, "ratio": 1.0 if total else 0.0}
        total = int(self.db.one(f"SELECT COUNT(*) c FROM media m WHERE {ACTIVE_MEDIA}")["c"])
        filled = int(self.db.one(
            f"SELECT COUNT(*) c FROM media m JOIN media_vectors v ON v.media_id=m.id AND v.model_key=? WHERE {ACTIVE_MEDIA}",
            (self.key,))["c"])
        return {"filled": filled, "total": total, "ratio": (filled / total) if total else 0.0}

    # -- index lifecycle ---------------------------------------------------
    def _fold(self, rows: list[dict]) -> None:
        keys, vecs = [], []
        for row in rows:
            try:
                vecs.append(self.file.read(row["offset"], row["sha"]))
                keys.append(row["key"])
            except ValueError as exc:
                logger.warning("%s: skipping unreadable vector for %s: %s", self.key, row["key"], exc)
        if keys:
            self.ann.upsert(keys, np.stack(vecs))

    def sync(self, *, force_rebuild: bool = False) -> dict:
        """Bring the ANN index up to date with the rows. Returns what happened."""
        with self._sync_lock:
            action = "catch_up"
            loaded = False if force_rebuild else (self._ready or self.ann.load())
            watermark = int(self.ann.meta.get("watermark", 0)) if loaded else 0
            if loaded and self.spec.subject == "face" and not self._ready:
                if self.ann.meta.get("signature") != self._face_signature(watermark):
                    loaded, watermark = False, 0
            if not loaded:
                action = "rebuild"
                self.ann.reset()
            added = 0
            while True:
                rows = self._rows_after(watermark, 4096)
                if not rows:
                    break
                self._fold(rows)
                added += len(rows)
                watermark = rows[-1]["rid"]
            removed = 0
            if len(self.ann) != self.count():
                extra = np.setdiff1d(self.ann.keys(), self._db_keys(), assume_unique=True)
                removed = self.ann.remove(extra)
            self.ann.meta["watermark"] = watermark
            if self.ann.dirty:
                sig = self._face_signature(watermark) if self.spec.subject == "face" else ""
                self.ann.save(watermark=watermark, signature=sig, key=self.key)
            self._ready = True
            return {"action": action, "added": added, "removed": removed, "size": len(self.ann)}

    def ensure_ready(self) -> None:
        if not self._ready:
            self.sync()

    def maybe_save(self, *, force: bool = False) -> None:
        if self.ann.dirty and (force or self.ann.dirty >= SAVE_EVERY or time.monotonic() - self._last_save > 30):
            watermark = int(self.ann.meta.get("watermark", 0))
            sig = self._face_signature(watermark) if self.spec.subject == "face" else ""
            self.ann.save(watermark=watermark, signature=sig, key=self.key)
            self._last_save = time.monotonic()

    # -- writes (media spaces) ---------------------------------------------
    def add(self, items: Sequence[tuple[int, np.ndarray]]) -> int:
        """Persist vectors for media ids (replacing older ones) and index them."""
        if self.spec.subject != "media" or not self.owned:
            raise ValueError(f"{self.key} is read-only")
        if not items:
            return 0
        vectors = [unit(v, self.spec.dim) for _, v in items]
        records = self.file.append_many(vectors)
        ids = [int(mid) for mid, _ in items]
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rids = []
            for mid, (offset, sha) in zip(ids, records):
                # Replace (never update) so the new row gets a fresh id above the index watermark.
                conn.execute("DELETE FROM media_vectors WHERE model_key=? AND media_id=?", (self.key, mid))
                rids.append(conn.execute(
                    "INSERT INTO media_vectors(model_key, media_id, offset, sha) VALUES (?,?,?,?)",
                    (self.key, mid, offset, sha)).lastrowid)
            conn.execute(
                f"DELETE FROM embedding_queue WHERE model_key=? AND media_id IN ({','.join('?' * len(ids))})",
                (self.key, *ids))
        self._coverage = None
        if self._ready:
            with self._sync_lock:
                self.ann.upsert(ids, np.stack(vectors))
                self.ann.meta["watermark"] = max(int(self.ann.meta.get("watermark", 0)), max(rids))
        return len(ids)

    def remove(self, media_ids: Iterable[int]) -> int:
        ids = [int(i) for i in media_ids]
        if not ids or self.spec.subject != "media":
            return 0
        with self.db.connect() as conn:
            conn.execute(f"DELETE FROM media_vectors WHERE model_key=? AND media_id IN ({','.join('?' * len(ids))})",
                         (self.key, *ids))
        return self.ann.remove(ids) if self._ready else 0

    def vector(self, key: int) -> Optional[np.ndarray]:
        if self.spec.subject == "face":
            row = self.db.one("SELECT embedding_offset AS offset, embedding_sha AS sha FROM faces WHERE id=?", (key,))
        else:
            row = self.db.one("SELECT offset, sha FROM media_vectors WHERE model_key=? AND media_id=?", (self.key, key))
        if not row:
            return None
        return self.file.read(row["offset"], row["sha"])

    # -- backfill ----------------------------------------------------------
    def pending(self, limit: int, exclude: Iterable[int] = ()) -> list[dict]:
        exclude = [int(i) for i in exclude]
        skip = f"AND m.id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
        return self.db.all(
            f"""SELECT m.id, m.path, m.kind, m.duration FROM media m
                LEFT JOIN embedding_queue q ON q.model_key=? AND q.media_id=m.id
                WHERE {ACTIVE_MEDIA}
                  AND NOT EXISTS (SELECT 1 FROM media_vectors v WHERE v.model_key=? AND v.media_id=m.id)
                  AND COALESCE(q.attempts, 0) < ? {skip}
                ORDER BY COALESCE(q.priority, 0) DESC, m.id DESC LIMIT ?""",
            (self.key, self.key, MAX_ATTEMPTS, *exclude, limit))

    def prioritize(self, media_ids: Iterable[int], priority: int = 100) -> None:
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO embedding_queue(model_key, media_id, priority) VALUES (?,?,?) "
                "ON CONFLICT(model_key, media_id) DO UPDATE SET priority=MAX(priority, excluded.priority), attempts=0",
                [(self.key, int(m), int(priority)) for m in media_ids])

    def _record_failures(self, failures: list[tuple[int, str]]) -> None:
        if not failures:
            return
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO embedding_queue(model_key, media_id, attempts, last_error) VALUES (?,?,1,?) "
                "ON CONFLICT(model_key, media_id) DO UPDATE SET attempts=attempts+1, last_error=excluded.last_error",
                [(self.key, mid, err[:500]) for mid, err in failures])

    def close(self) -> None:
        try:
            self.maybe_save(force=True)
        except Exception:
            logger.exception("%s: could not save index", self.key)
        self.file.close()


def run_backfill(space: Space, embedder: Embedder, *, checkpoint: Callable[[], None] = lambda: None,
                 progress: Callable[[dict], None] = lambda _p: None, batch_size: int = 16,
                 max_items: Optional[int] = None, should_yield: Callable[[], bool] = lambda: False) -> dict:
    """Embed media missing from ``space``. Resumable: state lives in the rows."""
    if embedder.spec.key != space.key:
        raise ValueError("Embedder does not produce vectors for this space")
    space.ensure_ready()
    done = failed = 0
    failed_ids: set[int] = set()  # retried on the next run, not in a tight loop
    yielded = False
    total_pending = space.coverage()
    while max_items is None or done + failed < max_items:
        checkpoint()
        if should_yield():
            yielded = True
            break
        take = batch_size if max_items is None else min(batch_size, max_items - done - failed)
        rows = space.pending(take, exclude=failed_ids)
        if not rows:
            break
        with diagnostics.span("embed", space.key, items=len(rows)):
            results = embedder.embed_media(rows)
        good, bad = [], []
        for row, result in zip(rows, results):
            if isinstance(result, BaseException) or result is None:
                bad.append((row["id"], f"{type(result).__name__}: {result}"))
            else:
                good.append((row["id"], result))
        checkpoint()
        done += space.add(good)
        space._record_failures(bad)
        failed_ids.update(mid for mid, _ in bad)
        failed += len(bad)
        space.maybe_save()
        progress({"embedded": done, "failed": failed, "filled": total_pending["filled"] + done,
                  "total": total_pending["total"]})
    space.maybe_save(force=True)
    return {"embedded": done, "failed": failed, "yielded": yielded, "coverage": space.coverage()}


class VectorSpaces:
    """Registry of embedding spaces for one data directory."""

    def __init__(self, db, data_dir: Path, *, media_dtype: str = "f16", face_dtype: str = "i8"):
        self.db, self.data_dir = db, Path(data_dir)
        self.media_dtype, self.face_dtype = media_dtype, face_dtype
        self._spaces: dict[str, Space] = {}
        self._lock = threading.Lock()

    def register(self, spec: ModelSpec) -> Space:
        file_rel = f"vectors/{spec.slug}.f32"
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO embedding_models(key,model_id,version,dim,subject,role,file) VALUES (?,?,?,?,?,?,?)",
                (spec.key, spec.model_id, spec.version, spec.dim, spec.subject, spec.role, file_rel))
        return self.get(spec.key)

    def rows(self) -> list[dict]:
        return self.db.all("SELECT * FROM embedding_models ORDER BY created_at, key")

    def get(self, key: str) -> Space:
        with self._lock:
            space = self._spaces.get(key)
            if space is not None:
                return space
            row = self.db.one("SELECT * FROM embedding_models WHERE key=?", (key,))
            if row is None:
                raise KeyError(f"Embedding space {key} is not registered")
            spec = KNOWN.get(key) or ModelSpec(row["model_id"], row["version"], row["dim"], row["subject"], row["role"])
            owned = row["file"].startswith("vectors/")
            dtype = self.face_dtype if spec.subject == "face" else self.media_dtype
            space = self._spaces[key] = Space(self.db, self.data_dir, spec, row["file"], owned=owned, dtype=dtype)
            return space

    def by_role(self, role: str) -> list[Space]:
        return [self.get(r["key"]) for r in self.rows() if r["role"] == role]

    def active(self, role: str, *, prefer: Optional[Iterable[str]] = None, min_ratio: float = 0.95) -> Optional[Space]:
        """Newest space for a role once it is (nearly) complete, else the best-covered one.

        ``prefer`` limits the choice to keys whose model can currently embed queries.
        """
        allowed = set(prefer) if prefer is not None else None
        candidates = [s for s in self.by_role(role) if allowed is None or s.key in allowed]
        if not candidates:
            return None
        # A model the user pinned (model-upgrade flow: build side by side, switch or roll back explicitly).
        pinned = (self.db.settings().get("active_models") or {}).get(role)
        if pinned:
            for space in candidates:
                if space.key == pinned:
                    return space
        # Plugin models are only ever used when pinned: installing a plugin must not change search by itself.
        candidates = [s for s in candidates if not s.spec.model_id.startswith("plugin-")] or candidates
        covered = [(s, s.coverage()) for s in candidates]
        ready = [s for s, c in covered if c["total"] and c["ratio"] >= min_ratio]
        if ready:
            return ready[-1]
        best = max(covered, key=lambda sc: (sc[1]["filled"], candidates.index(sc[0])))
        return best[0] if best[1]["filled"] else candidates[-1]

    def status(self) -> list[dict]:
        out = []
        for row in self.rows():
            space = self.get(row["key"])
            out.append({"key": row["key"], "model_id": row["model_id"], "version": row["version"],
                        "dim": row["dim"], "subject": row["subject"], "role": row["role"],
                        "coverage": space.coverage(), "index_size": len(space.ann) if space._ready else None,
                        "index_ready": space._ready})
        return out

    def forget(self, key: str, *, drop_index: bool = False) -> None:
        """Close a cached space (e.g. after its backing file was replaced)."""
        with self._lock:
            space = self._spaces.pop(key, None)
        if space is not None:
            space.file.close()
            if drop_index:
                for path in (space.ann.path, space.ann.meta_path):
                    path.unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            spaces, self._spaces = list(self._spaces.values()), {}
        for space in spaces:
            space.close()
