#!/usr/bin/env python3
"""How long does an unchanged library take to re-scan, and how much memory does it use?

    python scripts/bench_share_scan.py --files 100000

Creates that many 1-byte ".jpg" files in 1,000 folders, registers them as an already-indexed
network library (so no decoding happens), then times a full scan and a quick scan of the unchanged
tree. With --path it scans an existing folder read-only instead (nothing is written there): only the
directory walk is timed, since that folder is not in any index.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Config  # noqa: E402
from backend.netlib import relative_identity  # noqa: E402
from backend.netscan import walk  # noqa: E402
from backend.services.container import Services  # noqa: E402


class NoEngine:
    def status(self):
        return {"state": "ready", "provider": "CPU"}

    def configure(self, **_):
        pass

    def load(self):
        return self.status()


def peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1e6 if sys.platform == "darwin" else 1e3), 1)


def walk_only(path: str) -> dict:
    started, files, folders = time.perf_counter(), 0, 0

    def on_dir(*_):
        nonlocal folders
        folders += 1

    for _ in walk(Path(path), network=True, on_dir=on_dir):
        files += 1
    seconds = time.perf_counter() - started
    return {"files": files, "folders": folders, "seconds": round(seconds, 2), "files_per_s": round(files / seconds) if seconds else None,
            "peak_rss_mb": peak_mb()}


def bench(files: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        share = root / "share"
        (root / "fe").mkdir()
        started = time.perf_counter()
        per_dir = max(1, files // 1000)
        for index in range(files):
            folder = share / f"{index // per_dir // 40:03d}" / f"{index // per_dir:05d}"
            if index % per_dir == 0:
                folder.mkdir(parents=True, exist_ok=True)
            with open(folder / f"IMG_{index:06d}.jpg", "wb") as handle:
                handle.write(b"x")
        created = time.perf_counter() - started
        services = Services(Config(data_dir=root / "data", model_dir=root / "models", frontend_dir=root / "fe", allowed_roots=str(root), watch=False),
                            engine=NoEngine())
        try:
            with services.db.connect() as conn:
                conn.execute("INSERT INTO libraries(id, path, name, root_kind) VALUES (1, ?, 'share', 'network')", (str(share),))
                rows = []
                for folder, _dirs, names in os.walk(share):
                    for name in names:
                        path = os.path.join(folder, name)
                        info = os.stat(path)
                        rows.append((path, name, info.st_size, info.st_mtime_ns, *relative_identity(str(share), path)))
                conn.executemany("INSERT INTO media(library_id, path, name, kind, size, mtime_ns, status, rel_path, rel_key, indexed_at) "
                                 "VALUES (1,?,?,'photo',?,?,'indexed',?,?,CURRENT_TIMESTAMP)", rows)
            baseline = peak_mb()
            results = {"files": files, "create_seconds": round(created, 1), "rss_before_mb": baseline}
            for mode in ("full", "quick"):
                started = time.perf_counter()
                services.worker.start(1, quick=mode == "quick")
                services.worker._thread.join()
                job = services.worker.latest()
                stats = json.loads(services.db.one("SELECT last_scan_stats FROM libraries WHERE id=1")["last_scan_stats"])
                results[mode] = {"status": job["status"], "seconds": round(time.perf_counter() - started, 2), "changed": stats["changed"],
                                 "skipped": job["skipped"], "peak_rss_mb": peak_mb()}
            results["missing_marked"] = services.db.one("SELECT COUNT(*) AS n FROM media WHERE missing=1")["n"]
            return results
        finally:
            services.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=100_000)
    parser.add_argument("--path", help="walk an existing folder read-only instead")
    args = parser.parse_args()
    print(json.dumps(walk_only(args.path) if args.path else bench(args.files), indent=1))
