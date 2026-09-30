#!/usr/bin/env python3
"""Create a synthetic library database for UI performance checks (no media files).

    python scripts/make_synthetic_library.py --data-dir /tmp/lfs-synth --items 100000 --geo 50000
    LFS_DATA_DIR=/tmp/lfs-synth LFS_PORT=8777 LFS_WATCH=false python -m backend

Rows get capture dates spread over ten years (bursty, like real libraries)
and half of them GPS around 40 cities. Thumbnails do not exist, so tiles show
their placeholder; that is deliberate: the benchmark measures layout and
scrolling, not image decoding.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db import Database  # noqa: E402

CITIES = [(51.5, -0.12), (48.86, 2.35), (40.71, -74.0), (35.68, 139.69), (23.81, 90.41), (22.33, 91.83),
          (-33.87, 151.21), (37.77, -122.42), (52.52, 13.4), (41.9, 12.5), (19.43, -99.13), (-23.55, -46.63),
          (1.35, 103.82), (25.2, 55.27), (55.75, 37.62), (-1.29, 36.82), (30.04, 31.24), (13.75, 100.5),
          (28.61, 77.21), (19.08, 72.88), (34.05, -118.24), (43.65, -79.38), (59.33, 18.07), (60.17, 24.94),
          (64.14, -21.94), (-34.6, -58.38), (39.9, 116.4), (31.23, 121.47), (37.57, 126.98), (14.6, 120.98),
          (-6.2, 106.85), (3.14, 101.69), (27.72, 85.32), (6.93, 79.85), (21.03, 105.85), (45.46, 9.19),
          (40.42, -3.7), (38.72, -9.14), (47.5, 19.04), (50.08, 14.44)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--items", type=int, default=100_000)
    parser.add_argument("--geo", type=int, default=50_000)
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args()
    rng = np.random.default_rng(args.seed)
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    db = Database(data / "index.sqlite")
    start = datetime(2016, 1, 1)
    # Bursty days: most days empty, some with dozens (trips, parties).
    day_offsets = np.sort(rng.choice(3650, size=args.items, p=_day_weights(rng)))[::-1]
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO libraries(id, path, name) VALUES (1, '/synthetic', 'synthetic')")
        rows = []
        geo_ids = set(rng.choice(args.items, size=min(args.geo, args.items), replace=False).tolist())
        for i in range(args.items):
            when = start + timedelta(days=int(day_offsets[i]), seconds=int(rng.integers(0, 86400)))
            lat = lon = None
            if i in geo_ids:
                clat, clon = CITIES[int(rng.integers(len(CITIES)))]
                lat, lon = clat + rng.normal(0, 0.08), clon + rng.normal(0, 0.08)
            kind = "video" if i % 12 == 0 else "photo"
            rows.append((i + 1, f"/synthetic/{i + 1}.jpg", f"IMG_{i + 1:06d}.jpg", kind, 2_000_000 + i, i,
                         when.isoformat(), "exif", lat, lon, 4032, 3024, 12.0 if kind == "video" else None))
        conn.executemany(
            "INSERT OR REPLACE INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, date_source, "
            "gps_lat, gps_lon, width, height, duration, status, meta_version) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,'indexed',1)",
            rows)
    print(f"{args.items} items ({len(geo_ids)} geotagged) in {data}")
    return 0


def _day_weights(rng) -> np.ndarray:
    weights = rng.pareto(1.2, 3650) + 0.05
    weights[rng.random(3650) < 0.55] = 0.0005  # quiet days
    return weights / weights.sum()


if __name__ == "__main__":
    raise SystemExit(main())
