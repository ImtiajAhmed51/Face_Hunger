#!/usr/bin/env python3
"""Hybrid-search latency on a synthetic library (no models or media needed).

    python scripts/bench_search.py --items 100000

Builds a throw-away data dir with N media rows, faces for 500 people, a
768-D text space and a 384-D visual space, then times a mix of queries
(text, filters, people, dates, similar-media and combinations).
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Config  # noqa: E402
from backend.services.container import Services  # noqa: E402
from backend.vectors.specs import ModelSpec  # noqa: E402

TEXT = ModelSpec("bench-siglip", "1", 768, "media", "text_image")
VISUAL = ModelSpec("bench-dino", "1", 384, "media", "visual")


class _NoFaces:
    detection_size, multi_scale = 640, False

    def status(self):
        return {"state": "unloaded"}


class TextEncoder:
    spec = TEXT

    def __init__(self, seed_vectors):
        self.seed_vectors = seed_vectors

    def embed_texts(self, texts):
        rng = np.random.default_rng(abs(hash(texts[0])) % 2**32)
        v = self.seed_vectors[rng.integers(len(self.seed_vectors))] + 0.3 * rng.standard_normal(TEXT.dim)
        return (v / np.linalg.norm(v))[None].astype(np.float32)


def unit_rows(rng, n, dim):
    x = rng.standard_normal((n, dim)).astype(np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def build(root: Path, n: int, rng) -> tuple[Services, dict]:
    timings = {}
    cfg = Config(data_dir=root / "data", model_dir=root / "models", frontend_dir=root / "none", allowed_roots=str(root))
    s = Services(cfg, engine=_NoFaces())
    t = time.perf_counter()
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(path, name) VALUES ('/bench', 'bench')")
        conn.executemany(
            "INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, status) VALUES (?,1,?,?,?,?,?,?,'indexed')",
            [(i, f"/bench/{i}.jpg", f"{i}.jpg", "video" if i % 10 == 0 else "photo", 1000 + i, i,
              f"{2015 + i % 10}-{1 + i % 12:02d}-{1 + i % 28:02d}T12:00:00") for i in range(1, n + 1)])
        conn.executemany("INSERT INTO people(id, name, face_count) VALUES (?,?,?)",
                         [(p, f"Person {p}", 100) for p in range(1, 501)])
        faces = [(int(m), int(p), rng.random()) for m, p in zip(rng.integers(1, n + 1, int(n * 0.6)),
                                                                rng.integers(1, 501, int(n * 0.6)))]
        conn.executemany("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha, quality)"
                         " VALUES (?,?,'[0,0,1,1]',0.9,0,'x',?)", faces)
    timings["rows_s"] = round(time.perf_counter() - t, 1)
    text_vecs = unit_rows(rng, n, TEXT.dim)
    for spec, vecs, key in ((TEXT, text_vecs, "text_index_build_s"), (VISUAL, unit_rows(rng, n, VISUAL.dim), "visual_index_build_s")):
        space = s.vectors.register(spec)
        space.ensure_ready()
        t = time.perf_counter()
        for start in range(0, n, 10000):
            space.add([(i + 1, vecs[i]) for i in range(start, min(n, start + 10000))])
        timings[key] = round(time.perf_counter() - t, 1)
    s.models.text_encoder = lambda: TextEncoder(text_vecs)
    return s, timings


QUERIES = [
    ("text", lambda r, n: {"text": f"q{r.integers(1e9)}"}),
    ("text+kind", lambda r, n: {"text": f"q{r.integers(1e9)}", "kind": "photo"}),
    ("text+person", lambda r, n: {"text": f"q{r.integers(1e9)}", "people": [int(r.integers(1, 501))]}),
    ("text+year", lambda r, n: {"text": f"q{r.integers(1e9)}", "date_from": "2018-01-01", "date_to": "2019-12-31"}),
    ("text+2people+quality", lambda r, n: {"text": f"q{r.integers(1e9)}", "people": [int(r.integers(1, 501)), int(r.integers(1, 501))],
                                           "min_quality": 0.3}),
    ("filters: person+kind", lambda r, n: {"people": [int(r.integers(1, 501))], "kind": "photo"}),
    ("filters: date range", lambda r, n: {"date_from": "2020-03-01", "date_to": "2020-06-30"}),
    ("similar media", lambda r, n: {"similar_media_id": int(r.integers(1, n + 1))}),
    ("text+similar+recency", lambda r, n: {"text": f"q{r.integers(1e9)}", "similar_media_id": int(r.integers(1, n + 1)),
                                           "weights": {"recency": 0.3}}),
]


def bench(n: int = 100_000, rounds: int = 20, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    root = Path(tempfile.mkdtemp(prefix="lfs-bench-"))
    try:
        s, timings = build(root, n, rng)
        for _, make in QUERIES:  # warm caches (SQLite pages, index)
            s.search.run(make(rng, n))
        per_kind, all_ms = {}, []
        for _ in range(rounds):
            for name, make in QUERIES:
                t = time.perf_counter()
                s.search.run(make(rng, n))
                ms = (time.perf_counter() - t) * 1000
                per_kind.setdefault(name, []).append(ms)
                all_ms.append(ms)
        q = lambda xs, p: float(np.percentile(xs, p))  # noqa: E731
        result = {"items": n, "queries": len(all_ms), **timings,
                  "p50_ms": round(q(all_ms, 50), 1), "p95_ms": round(q(all_ms, 95), 1), "max_ms": round(max(all_ms), 1),
                  "by_query_p95_ms": {k: round(q(v, 95), 1) for k, v in per_kind.items()},
                  "by_query_median_ms": {k: round(statistics.median(v), 1) for k, v in per_kind.items()}}
        s.vectors._spaces.clear()  # skip persisting throw-away indexes
        s.close()
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=100_000)
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(bench(args.items, args.rounds), indent=2))
