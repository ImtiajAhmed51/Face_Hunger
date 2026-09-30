"""Shared builders for tests that need library rows without real media or models."""

from __future__ import annotations

import time

import numpy as np

from backend.vectors.specs import ModelSpec

FAKE_VISUAL = ModelSpec("fake-visual", "1", 32, "media", "visual")
FAKE_VISUAL_V2 = ModelSpec("fake-visual", "2", 32, "media", "visual")
FAKE_TEXT = ModelSpec("fake-siglip", "1", 32, "media", "text_image")


def fake_vector(media_id: int, dim: int = 32, salt: int = 0) -> np.ndarray:
    rng = np.random.default_rng(media_id * 7919 + salt)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    def __init__(self, spec: ModelSpec, delay: float = 0.0, fail_ids=(), salt: int = 0):
        self.spec, self.delay, self.fail_ids, self.salt = spec, delay, set(fail_ids), salt
        self.calls = 0

    def embed_media(self, rows):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return [ValueError("undecodable") if r["id"] in self.fail_ids else fake_vector(r["id"], self.spec.dim, self.salt)
                for r in rows]


def add_media(db, n: int, *, library_path: str = "/lib", kind: str = "photo", start_date: str = "2020-01-01",
              status: str = "indexed") -> list[int]:
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO libraries(path, name) VALUES (?, 'lib')", (library_path,))
        lib = conn.execute("SELECT id FROM libraries WHERE path=?", (library_path,)).fetchone()[0]
        first = conn.execute("SELECT COALESCE(MAX(id), 0) FROM media").fetchone()[0] + 1
        rows = []
        for i in range(first, first + n):
            day = (i % 28) + 1
            month = (i // 28) % 12 + 1
            year = int(start_date[:4]) + (i // 336) % 5
            rows.append((i, lib, f"{library_path}/img_{i}.jpg", f"img_{i}.jpg", kind if kind != "mixed" else
                         ("video" if i % 5 == 0 else "photo"), 1000 + i, i, f"{year:04d}-{month:02d}-{day:02d}T12:00:00",
                         status))
        conn.executemany(
            "INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
            rows)
    return [r[0] for r in rows]
