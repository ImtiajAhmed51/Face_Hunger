"""Append-only float32 vector file with per-record SHA-256 (stored by the caller).

Same on-disk format as ``embeddings.bin`` / ``media_embeddings.bin`` but for
any dimension. A process killed mid-write can leave a partial trailing record;
because the database row pointing at a record is only committed after the
bytes are fsynced, that tail is never referenced and is truncated on open.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import numpy as np

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def unit(vector, dim: int) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float32).reshape(-1)
    if value.shape != (dim,) or not np.isfinite(value).all():
        raise ValueError(f"Vector must contain exactly {dim} finite numbers")
    norm = float(np.linalg.norm(value.astype(np.float64)))
    if not np.isfinite(norm) or norm < 1e-12:
        raise ValueError("Vector has zero or invalid norm")
    return np.ascontiguousarray(value / norm, dtype=np.float32)


class VectorFile:
    def __init__(self, path, dim: int, *, readonly: bool = False):
        self.path = Path(path).resolve()
        self.dim = int(dim)
        self.record = self.dim * 4
        self.readonly = readonly
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _guard:
            self._lock = _locks.setdefault(str(self.path), threading.RLock())
        if readonly:
            # Another component owns (and appends to) this file: never truncate it.
            self.path.touch(mode=0o600, exist_ok=True)
            self._fd = os.open(self.path, os.O_RDONLY)
            self.repaired_bytes = 0
        else:
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            self.repaired_bytes = self._repair_tail()

    def _repair_tail(self) -> int:
        with self._lock:
            size = os.fstat(self._fd).st_size
            extra = size % self.record
            if extra:
                os.ftruncate(self._fd, size - extra)
                os.fsync(self._fd)
            return extra

    @property
    def records(self) -> int:
        return os.fstat(self._fd).st_size // self.record

    def append_many(self, vectors) -> list[tuple[int, str]]:
        """Append vectors with one fsync; returns (offset, sha256) per vector."""
        if self.readonly:
            raise ValueError(f"{self.path.name} is read-only here")
        payloads = [unit(v, self.dim).astype("<f4", copy=False).tobytes() for v in vectors]
        if not payloads:
            return []
        with self._lock:
            start = os.lseek(self._fd, 0, os.SEEK_END)
            if start % self.record:
                raise ValueError("Vector file has a truncated trailing record")
            blob = b"".join(payloads)
            try:
                view = memoryview(blob)
                pos = start
                while view:
                    written = os.pwrite(self._fd, view, pos) if hasattr(os, "pwrite") else os.write(self._fd, view)
                    if written <= 0:
                        raise OSError("Could not write vectors")
                    view, pos = view[written:], pos + written
                os.fsync(self._fd)
            except BaseException:
                os.ftruncate(self._fd, start)
                raise
        return [(start + i * self.record, hashlib.sha256(p).hexdigest()) for i, p in enumerate(payloads)]

    def append(self, vector) -> tuple[int, str]:
        return self.append_many([vector])[0]

    def read(self, offset, sha, *, verify: bool = True) -> np.ndarray:
        if isinstance(offset, bool) or not isinstance(offset, (int, np.integer)):
            raise ValueError("Vector offset must be an integer")
        if offset < 0 or offset % self.record:
            raise ValueError("Vector offset is not record-aligned")
        with self._lock:
            data = os.pread(self._fd, self.record, int(offset))
        if len(data) != self.record:
            raise ValueError(f"Missing or truncated vector at {offset}")
        if verify and hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"Vector checksum mismatch at {offset}")
        return np.frombuffer(data, dtype="<f4").astype(np.float32, copy=True)

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
