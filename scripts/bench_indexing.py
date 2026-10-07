#!/usr/bin/env python3
"""Indexing memory at scale: embed N synthetic media rows through the real backfill path
(vector file + ANN index + SQLite bookkeeping) and report resident memory as it goes.

    python scripts/bench_indexing.py --items 500000 --dim 384

The embedder is a stand-in that returns random unit vectors, so the numbers are the
pipeline's own memory (not a model's). Peak RSS is read from the OS.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Config  # noqa: E402
from backend.services.container import Services  # noqa: E402
from backend.vectors.spaces import run_backfill  # noqa: E402
from backend.vectors.specs import ModelSpec  # noqa: E402


def rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1e6 if sys.platform == "darwin" else 1e3)


class RandomEmbedder:
    def __init__(self, spec):
        self.spec = spec
        self.rng = np.random.default_rng(1)

    def embed_media(self, rows):
        x = self.rng.standard_normal((len(rows), self.spec.dim)).astype(np.float32)
        return list(x / np.linalg.norm(x, axis=1, keepdims=True))


class NoEngine:
    def status(self):
        return {"ready": False}


def bench(items: int, dim: int = 384, batch: int = 256) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "fe").mkdir()
        services = Services(Config(data_dir=root / "data", model_dir=root / "models", frontend_dir=root / "fe",
                                   allowed_roots=str(root), watch=False), engine=NoEngine())
        try:
            with services.db.connect() as conn:
                conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, '/synthetic', 'synthetic')")
                conn.executemany("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status) VALUES (?,1,?,?,'photo',1,1,'indexed')",
                                 ((i, f"/synthetic/{i}.jpg", f"{i}.jpg") for i in range(1, items + 1)))
            spec = ModelSpec("bench-visual", "1", dim, "media", "visual")
            space = services.vectors.register(spec)
            samples = [{"items": 0, "rss_mb": round(rss_mb())}]
            started = time.perf_counter()
            step = max(batch, items // 10)

            def progress(p):
                if p["filled"] >= samples[-1]["items"] + step:
                    samples.append({"items": p["filled"], "rss_mb": round(rss_mb())})

            result = run_backfill(space, RandomEmbedder(spec), batch_size=batch, progress=progress)
            seconds = time.perf_counter() - started
            samples.append({"items": result["embedded"], "rss_mb": round(rss_mb())})
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1e6 if sys.platform == "darwin" else 1e3)
            vectors_mb = items * dim * 4 / 1e6
            return {"items": items, "dim": dim, "embedded": result["embedded"], "seconds": round(seconds, 1),
                    "items_per_s": round(result["embedded"] / seconds), "samples": samples, "peak_rss_mb": round(peak),
                    "vector_file_mb": round(vectors_mb), "growth_mb": samples[-1]["rss_mb"] - samples[0]["rss_mb"]}
        finally:
            services.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=500_000)
    parser.add_argument("--dim", type=int, default=384)
    args = parser.parse_args()
    print(json.dumps(bench(args.items, args.dim), indent=1))
