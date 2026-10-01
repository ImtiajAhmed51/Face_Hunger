#!/usr/bin/env python3
"""A small, self-contained library for UI tests (Playwright) and manual checks.

    python scripts/make_ui_fixture.py --root /tmp/lfs-ui
    LFS_DATA_DIR=/tmp/lfs-ui/data LFS_ALLOWED_ROOTS=/tmp/lfs-ui LFS_WATCH=false LFS_PORT=8778 python -m backend

Creates 20 exact-duplicate groups (an original plus 1-2 byte-identical copies),
a 3-shot burst, dated and geotagged photos, two people with faces, an event
and an album, all without needing the face models.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db import Database  # noqa: E402
from backend.duplicates import content_hash, image_phash  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    if root.exists():
        shutil.rmtree(root)
    lib = root / "library"
    data = root / "data"
    for sub in ("camera", "backup", "trip"):
        (lib / sub).mkdir(parents=True)
    (data / "thumbnails").mkdir(parents=True)
    db = Database(data / "index.sqlite")
    rng = np.random.default_rng(7)
    rows = []

    def add(path: Path, img: Image.Image, when: str, **extra):
        thumb_img = img.copy()
        thumb_img.thumbnail((640, 640))
        rows.append({"path": str(path), "name": path.name, "size": path.stat().st_size, "hash": content_hash(path),
                     "phash": image_phash(np.asarray(img.convert("RGB"))[:, :, ::-1].copy()), "w": img.width, "h": img.height,
                     "when": when, "thumb": thumb_img, **extra})

    for g in range(20):
        base = rng.integers(40, 215, 3)
        arr = np.clip(rng.normal(base, 40, (360, 480, 3)), 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        original = lib / "camera" / f"IMG_{g:04d}.jpg"
        img.save(original, quality=88)
        add(original, img, f"2023-0{1 + g % 9}-1{g % 9}T10:00:00")
        for k, copy in enumerate([lib / "backup" / f"IMG_{g:04d}.jpg"] + ([lib / "backup" / f"IMG_{g:04d}-1.jpg"] if g % 3 == 0 else [])):
            shutil.copy(original, copy)
            add(copy, img, f"2023-0{1 + g % 9}-1{g % 9}T10:00:00")
    burst = np.clip(rng.normal(120, 50, (360, 480, 3)), 0, 255).astype(np.uint8)
    for i in range(3):
        img = Image.fromarray(np.roll(burst, i * 2, axis=1))
        p = lib / "trip" / f"BURST_{i}.jpg"
        img.save(p, quality=85 + i * 4)
        add(p, img, f"2024-07-10T12:00:0{i}", gps=(21.43, 92.0), camera=("Canon", "EOS R5"))
    with db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'UI fixture')", (str(lib),))
        for i, r in enumerate(rows, start=1):
            gps = r.get("gps", (None, None))
            cam = r.get("camera", (None, None))
            conn.execute(
                "INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, date_source, content_hash, phash,"
                " width, height, gps_lat, gps_lon, camera_make, camera_model, status, meta_version)"
                " VALUES (?,1,?,?,'photo',?,1,?,'exif',?,?,?,?,?,?,?,?,'indexed',1)",
                (i, r["path"], r["name"], r["size"], r["when"], r["hash"], r["phash"], r["w"], r["h"], *gps, *cam))
            r["thumb"].save(data / "thumbnails" / f"media-{i}.jpg", quality=80)
    print(f"{len(rows)} media in {lib}; data dir {data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
