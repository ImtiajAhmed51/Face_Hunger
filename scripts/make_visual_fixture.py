#!/usr/bin/env python3
"""Fixture for visual QA: the UI fixture plus photo-like thumbnails, mixed aspect ratios,
long and Bangla names, an unnamed person, albums, favourites and deleted items.

    python scripts/make_visual_fixture.py --root /tmp/fh-visual
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db import Database  # noqa: E402

PALETTES = [((250, 200, 140), (60, 90, 150)), ((120, 190, 230), (240, 240, 250)), ((30, 60, 40), (190, 220, 150)),
            ((240, 120, 100), (60, 30, 80)), ((250, 240, 220), (200, 160, 120)), ((20, 30, 60), (90, 120, 200)),
            ((255, 255, 255), (210, 225, 240)), ((15, 15, 20), (60, 50, 70))]


def scene(width: int, height: int, seed: int) -> Image.Image:
    """A soft landscape-like picture: sky gradient, sun, two hills. Some are very bright or very dark on purpose."""
    rng = np.random.default_rng(seed)
    top, bottom = PALETTES[seed % len(PALETTES)]
    t = np.linspace(0, 1, height)[:, None, None]
    arr = (np.array(top) * (1 - t) + np.array(bottom) * t) * np.ones((1, width, 1))
    img = Image.fromarray(arr.astype(np.uint8))
    draw = ImageDraw.Draw(img)
    r = min(width, height) // 7
    cx, cy = int(width * rng.uniform(0.2, 0.8)), int(height * rng.uniform(0.15, 0.4))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(int(min(255, c + 60)) for c in top))
    for k, shade in enumerate((0.75, 0.5)):
        base = height * (0.6 + 0.15 * k)
        points = [(0, height)] + [(x, base + np.sin(x / width * 6 + seed + k) * height * 0.08) for x in range(0, width + 8, 8)] + [(width, height)]
        draw.polygon(points, fill=tuple(int(c * shade) for c in bottom))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def face(seed: int) -> Image.Image:
    skin = [(224, 172, 140), (141, 85, 54), (255, 219, 172), (198, 134, 92), (104, 62, 40)][seed % 5]
    img = Image.new("RGB", (160, 160), [(70, 90, 120), (120, 80, 80), (60, 110, 90), (90, 90, 100)][seed % 4])
    draw = ImageDraw.Draw(img)
    draw.ellipse([40, 100, 120, 220], fill=tuple(int(c * 0.6) for c in skin))
    draw.ellipse([48, 28, 112, 108], fill=skin)
    draw.ellipse([64, 58, 72, 66], fill=(30, 30, 30))
    draw.ellipse([88, 58, 96, 66], fill=(30, 30, 30))
    draw.arc([66, 70, 94, 92], 20, 160, fill=(90, 40, 40), width=2)
    return img


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    subprocess.run([sys.executable, str(Path(__file__).with_name("make_ui_fixture.py")), "--root", str(root)], check=True)
    data, lib = root / "data", root / "library"
    db = Database(data / "index.sqlite")
    for row in db.all("SELECT id, kind, path FROM media"):
        if row["kind"] == "photo":
            scene(640, 480, row["id"]).save(data / "thumbnails" / f"media-{row['id']}.jpg", quality=86)
    # Originals too, so the viewer shows a picture rather than test noise. Copies keep their group's picture.
    for row in db.all("SELECT MIN(id) AS id, content_hash, GROUP_CONCAT(path, '|') AS paths FROM media WHERE kind='photo' GROUP BY COALESCE(content_hash, id)"):
        picture = scene(960, 720, row["id"])
        for path in row["paths"].split("|"):
            picture.save(path, quality=88)
    shapes = [(360, 640), (1200, 400), (600, 600), (400, 900), (1600, 900), (640, 480)]
    first = db.one("SELECT MAX(id) AS n FROM media")["n"] + 1
    (lib / "mixed").mkdir(exist_ok=True)
    with db.connect() as conn:
        for i in range(18):
            w, h = shapes[i % len(shapes)]
            mid = first + i
            name = f"DSC_{4000 + i}.jpg" if i % 5 else f"A very long file name from a scanner export {i} - final (edited) copy 2.jpg"
            path = lib / "mixed" / name
            img = scene(w, h, 100 + i)
            img.save(path, quality=85)
            thumb = img.copy()
            thumb.thumbnail((640, 640))
            thumb.save(data / "thumbnails" / f"media-{mid}.jpg", quality=86)
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, date_source, width, height,"
                         " status, meta_version) VALUES (?,1,?,?,'photo',?,1,?,'exif',?,?,'indexed',1)",
                         (mid, str(path), name, path.stat().st_size, f"2025-0{1 + i % 9}-{10 + i}T1{i % 10}:30:00", w, h))
        people = [(3, "Alexandria Bartholomew-Fitzgerald Montgomery III"), (4, "রবীন্দ্রনাথ ঠাকুর"), (5, None), (6, "Li"),
                  (7, "María José Carreño Quiñones"), (8, "Ng")]
        face_id = conn.execute("SELECT MAX(id) FROM faces").fetchone()[0] + 1
        for n, (pid, pname) in enumerate(people):
            count = 2 + n * 3
            conn.execute("INSERT INTO people(id, name, face_count) VALUES (?,?,?)", (pid, pname, count))
            for k in range(count):
                mid = first + (n * 3 + k) % 18
                conn.execute("INSERT INTO faces(id, media_id, person_id, bbox, detection, embedding_offset, embedding_sha, quality, review_state)"
                             " VALUES (?,?,?, '[120,90,110,110]', 0.92, 0, 'x', 0.8, 'confirmed')", (face_id, mid, pid))
                face(pid * 7 + k).save(data / "thumbnails" / f"face-{face_id}.jpg")
                face_id += 1
        for fid in range(1, 9):
            face(fid).save(data / "thumbnails" / f"face-{fid}.jpg")
        conn.execute("UPDATE people SET representative_face_id=(SELECT MIN(id) FROM faces WHERE person_id=people.id)")
        conn.execute("INSERT INTO albums(id, name, cover_media_id) VALUES (2, ?, ?), (3, 'Family', ?), (4, 'ঈদের ছুটি ২০২৫', ?)",
                     ("Summer in Cox's Bazar 2024 - the long weekend with everyone and the dog", first, first + 2, first + 4))
        for album, ids in ((2, range(first, first + 9)), (3, range(first + 3, first + 7)), (4, range(first + 8, first + 14))):
            conn.executemany("INSERT INTO album_media(album_id, media_id, position) VALUES (?,?,?)", [(album, m, p) for p, m in enumerate(ids)])
        conn.executemany("INSERT OR IGNORE INTO favorites(media_id) VALUES (?)", [(first + i,) for i in (0, 1, 4, 5, 9)])
        conn.execute("UPDATE media SET deleted_at=CURRENT_TIMESTAMP WHERE id IN (?, ?)", (first + 16, first + 17))
    print(f"visual fixture in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
