"""Append-only, checksummed 768-D DINOv2 embedding storage for media items."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import numpy as np

from .dino import DINO_DIM

VECTOR_BYTES = DINO_DIM * 4
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def normalize(vector) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float32)
    if value.shape != (DINO_DIM,) or not np.isfinite(value).all():
        raise ValueError(f"Media embedding must be {DINO_DIM} finite floats")
    norm = float(np.linalg.norm(value.astype(np.float64)))
    if not np.isfinite(norm) or norm < 1e-12:
        raise ValueError("Media embedding has zero or invalid norm")
    return np.ascontiguousarray(value / norm, dtype=np.float32)


class MediaEmbeddingStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locks_guard:
            self._lock = _locks.setdefault(str(self.path), threading.RLock())
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)

    def append(self, vector):
        data = normalize(vector).astype("<f4", copy=False).tobytes()
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            offset = os.lseek(self._fd, 0, os.SEEK_END)
            if offset % VECTOR_BYTES:
                raise ValueError("Media embedding file has a truncated trailing record")
            try:
                view = memoryview(data)
                while view:
                    written = os.write(self._fd, view)
                    if written <= 0:
                        raise OSError("Could not write media embedding")
                    view = view[written:]
                os.fsync(self._fd)
            except BaseException:
                os.ftruncate(self._fd, offset)
                os.fsync(self._fd)
                raise
        return offset, digest

    def read(self, offset, sha):
        if isinstance(offset, bool) or not isinstance(offset, (int, np.integer)):
            raise ValueError("Embedding offset must be an integer")
        if offset < 0 or offset % VECTOR_BYTES:
            raise ValueError("Embedding offset is not record-aligned")
        with self._lock:
            if hasattr(os, "pread"):
                data = os.pread(self._fd, VECTOR_BYTES, int(offset))
            else:
                os.lseek(self._fd, int(offset), os.SEEK_SET)
                data = os.read(self._fd, VECTOR_BYTES)
        if len(data) != VECTOR_BYTES:
            raise ValueError(f"Missing or truncated media embedding at {offset}")
        if not isinstance(sha, str) or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"Media embedding checksum mismatch at {offset}")
        value = np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)
        if not np.isfinite(value).all() or not np.isclose(np.linalg.norm(value), 1.0, atol=1e-3):
            raise ValueError(f"Media embedding at {offset} is not a finite unit {DINO_DIM}D vector")
        return value

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
