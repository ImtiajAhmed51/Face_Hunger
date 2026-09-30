"""usearch HNSW index with atomic persistence and a catch-up watermark.

The index is a cache: the vector file + database rows are the source of
truth. ``meta`` records the highest row id folded into the saved index and
a checksum of the rows at or below it, so on load we either apply only the
new rows (the common case) or rebuild when earlier rows changed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable, Optional

import numpy as np


class AnnIndex:
    def __init__(self, path: Path, dim: int, dtype: str = "f16"):
        from usearch.index import Index

        self._Index = Index
        self.path = Path(path)
        self.meta_path = self.path.with_suffix(".meta.json")
        self.dim = int(dim)
        self.dtype = dtype
        self.lock = threading.RLock()
        self.meta: dict = {}
        self.dirty = 0
        self.index = self._new()

    def _new(self):
        return self._Index(ndim=self.dim, metric="cos", dtype=self.dtype, connectivity=16,
                           expansion_add=64, expansion_search=96)

    # -- persistence -----------------------------------------------------
    def load(self) -> bool:
        with self.lock:
            if not (self.path.is_file() and self.meta_path.is_file()):
                return False
            try:
                meta = json.loads(self.meta_path.read_text())
                if meta.get("dim") != self.dim or meta.get("dtype") != self.dtype:
                    return False
                index = self._Index.restore(str(self.path))
                if index is None or index.ndim != self.dim or len(index) != meta.get("count"):
                    return False
            except Exception:
                return False
            self.index, self.meta, self.dirty = index, meta, 0
            return True

    def save(self, **meta) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".usearch.tmp")
            self.index.save(str(tmp))
            os.replace(tmp, self.path)
            self.meta = {**self.meta, **meta, "dim": self.dim, "dtype": self.dtype, "count": len(self.index)}
            tmp_meta = self.meta_path.with_suffix(".tmp")
            tmp_meta.write_text(json.dumps(self.meta))
            os.replace(tmp_meta, self.meta_path)
            self.dirty = 0

    def reset(self) -> None:
        with self.lock:
            self.index, self.meta, self.dirty = self._new(), {}, 0

    # -- mutation --------------------------------------------------------
    def upsert(self, keys: Iterable[int], vectors: np.ndarray) -> None:
        keys = np.asarray(list(keys), dtype=np.uint64)
        if not len(keys):
            return
        vectors = np.asarray(vectors, dtype=np.float32).reshape(len(keys), self.dim)
        with self.lock:
            present = keys[self.index.contains(keys)] if len(self.index) else keys[:0]
            if len(present):
                self.index.remove(present)
            self.index.add(keys, vectors, threads=0)
            self.dirty += len(keys)

    def remove(self, keys: Iterable[int]) -> int:
        keys = np.asarray(list(keys), dtype=np.uint64)
        with self.lock:
            if not len(keys) or not len(self.index):
                return 0
            present = keys[self.index.contains(keys)]
            if len(present):
                self.index.remove(present)
                self.dirty += len(present)
            return int(len(present))

    # -- queries ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self.index)

    def keys(self) -> np.ndarray:
        with self.lock:
            return np.asarray(self.index.keys, dtype=np.uint64) if len(self.index) else np.zeros(0, np.uint64)

    def search(self, vector: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        """Return (keys, cosine similarities) best-first."""
        with self.lock:
            n = len(self.index)
            if not n:
                return np.zeros(0, np.int64), np.zeros(0, np.float32)
            matches = self.index.search(np.asarray(vector, dtype=np.float32), min(int(k), n))
            return np.asarray(matches.keys, dtype=np.int64), 1.0 - np.asarray(matches.distances, dtype=np.float32)

    def get(self, keys: Iterable[int]) -> tuple[np.ndarray, np.ndarray]:
        """Vectors for the keys present in the index: (found_keys, matrix)."""
        keys = np.asarray(list(keys), dtype=np.uint64)
        with self.lock:
            if not len(keys) or not len(self.index):
                return np.zeros(0, np.int64), np.zeros((0, self.dim), np.float32)
            keys = keys[self.index.contains(keys)]
            if not len(keys):
                return np.zeros(0, np.int64), np.zeros((0, self.dim), np.float32)
            got = self.index.get(keys, dtype=np.float32)
        if isinstance(got, np.ndarray) and got.ndim == 1:
            got = [got]
        pairs = [(int(k), v) for k, v in zip(keys, got) if v is not None]
        if not pairs:
            return np.zeros(0, np.int64), np.zeros((0, self.dim), np.float32)
        return np.array([p[0] for p in pairs], np.int64), np.stack([p[1] for p in pairs]).astype(np.float32)

    def exact(self, vector: np.ndarray, keys: Iterable[int]) -> tuple[np.ndarray, np.ndarray]:
        """Exact cosine against a (filtered) key set, best-first."""
        found, matrix = self.get(keys)
        if not len(found):
            return found, np.zeros(0, np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms < 1e-12] = 1.0
        sims = (matrix @ np.asarray(vector, dtype=np.float32)) / norms
        order = np.argsort(-sims, kind="stable")
        return found[order], sims[order].astype(np.float32)


def checksum(rows: Optional[tuple]) -> str:
    return "" if not rows else ":".join(str(x or 0) for x in rows)
