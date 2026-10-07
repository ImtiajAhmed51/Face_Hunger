#!/usr/bin/env python3
"""Contact sheets for visual review: python scripts/visual_sheet.py <screen> [state] -> artifacts/visual/_sheets/<screen>-<state>.png"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1] / "artifacts" / "visual"
screen = sys.argv[1]
state = sys.argv[2] if len(sys.argv) > 2 else "populated"
files = sorted((root / screen).glob(f"{state}-*.png"), key=lambda p: (int(p.stem.split("-")[-3].split("x")[0]), p.stem))
if len(sys.argv) > 3:
    files = [f for f in files if any(k in f.stem for k in sys.argv[3].split(","))]
H = 620
tiles = []
for f in files:
    img = Image.open(f).convert("RGB")
    img = img.resize((max(1, round(img.width * H / img.height)), H), Image.LANCZOS)
    tile = Image.new("RGB", (img.width, H + 18), (255, 255, 0))
    tile.paste(img, (0, 18))
    ImageDraw.Draw(tile).text((4, 3), f.stem.replace(state + "-", ""), fill=(0, 0, 0))
    tiles.append(tile)
rows, row, width = [], [], 0
for t in tiles:
    if width + t.width > 2400 and row:
        rows.append(row); row, width = [], 0
    row.append(t); width += t.width + 8
rows.append(row)
sheet = Image.new("RGB", (max(sum(t.width + 8 for t in r) for r in rows), len(rows) * (H + 26)), (40, 40, 40))
for j, r in enumerate(rows):
    x = 0
    for t in r:
        sheet.paste(t, (x, j * (H + 26))); x += t.width + 8
out = root / "_sheets"; out.mkdir(exist_ok=True)
sheet.save(out / f"{screen}-{state}.png")
print(out / f"{screen}-{state}.png", sheet.size)
