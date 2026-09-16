"""Append-only, checksummed 512-dimensional float32 embedding storage."""

import hashlib
import os
import threading
from pathlib import Path

import numpy as np

DIMENSION = 512
VECTOR_BYTES = DIMENSION * 4
_locks = {}
_locks_guard = threading.Lock()


def normalize(vector):
    value = np.asarray(vector, dtype=np.float32)
    if value.shape != (DIMENSION,) or not np.isfinite(value).all():
        raise ValueError("Embedding must contain exactly 512 finite numbers")
    norm = float(np.linalg.norm(value.astype(np.float64)))
    if not np.isfinite(norm) or norm < 1e-12:
        raise ValueError("Embedding has zero or invalid norm")
    return np.ascontiguousarray(value / norm, dtype=np.float32)


class EmbeddingStore:
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
                raise ValueError("Embedding file has a truncated trailing record; repair before indexing")
            try:
                view = memoryview(data)
                while view:
                    written = os.write(self._fd, view)
                    if written <= 0:
                        raise OSError("Could not write embedding")
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
            raise ValueError(f"Missing or truncated embedding at {offset}")
        if not isinstance(sha, str) or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"Embedding checksum mismatch at {offset}")
        value = np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)
        if not np.isfinite(value).all() or not np.isclose(np.linalg.norm(value), 1.0, atol=1e-4):
            raise ValueError(f"Embedding at {offset} is not a finite unit 512D vector")
        return value

    def iterate(self, db, batch_size=256, active_only=True):
        """Yield (face-row dict, vector), keyset-paginated without loading all faces."""
        if not 1 <= batch_size <= 10000:
            raise ValueError("batch_size must be between 1 and 10000")
        last_id = 0
        active = """
          AND f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
          AND f.review_state != 'rejected'
          AND NOT EXISTS (SELECT 1 FROM exclusions e
                          WHERE e.media_id=f.media_id AND e.person_id=f.person_id)
          AND NOT EXISTS (SELECT 1 FROM rejections r
                          WHERE r.face_id=f.id AND r.person_id=f.person_id)
        """ if active_only else ""
        while True:
            rows = db.all("SELECT f.* FROM faces f JOIN media m ON m.id=f.media_id "
                          "WHERE f.id>? " + active + " ORDER BY f.id LIMIT ?",
                          (last_id, batch_size))
            if not rows:
                return
            for row in rows:
                yield row, self.read(row["embedding_offset"], row["embedding_sha"])
            last_id = rows[-1]["id"]

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
