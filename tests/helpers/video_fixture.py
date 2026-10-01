"""Synthetic videos with known ground truth (faces from insightface's bundled sample images)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

INSIGHT = Path(__import__("insightface").__file__).parent / "data" / "images"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# Person A = Tom Hanks sample, person B = second face from the left in t1.jpg.
TRUTH = {
    "A": [(0.0, 12.0), (32.0, 44.0), (50.0, 60.0)],
    "B": [(20.0, 44.0)],
}
DURATION = 60.0
CUTS = [12.0, 20.0, 32.0, 44.0, 50.0]


def _face_b() -> Image.Image:
    img = Image.open(INSIGHT / "t1.jpg").convert("RGB")
    # Box found once with buffalo_l on t1.jpg (stable sample image), padded.
    x0, y0, x1, y1 = 228, 96, 412, 310
    return img.crop((x0, y0, x1, y1))


def _face_a() -> Image.Image:
    return Image.open(INSIGHT / "Tom_Hanks_54745.png").convert("RGB")


def frame_at(t: float, size=(960, 540)) -> Image.Image:
    w, h = size
    # Background changes per shot so scene detection finds every cut.
    shot = sum(t >= c for c in CUTS)
    palette = [(40, 60, 90), (120, 120, 120), (80, 50, 40), (30, 80, 60), (100, 100, 140), (60, 40, 90)]
    frame = Image.new("RGB", size, palette[shot])
    a = next((True for s, e in TRUTH["A"] if s <= t < e), False)
    b = next((True for s, e in TRUTH["B"] if s <= t < e), False)
    drift = int(20 * np.sin(t / 2))
    face_h = int(h * 0.55)
    if a:
        fa = _face_a()
        fa = fa.resize((int(fa.width * face_h / fa.height), face_h))
        frame.paste(fa, (int(w * (0.12 if b else 0.35)) + drift, int(h * 0.2)))
    if b:
        fb = _face_b()
        fb = fb.resize((int(fb.width * face_h / fb.height), face_h))
        frame.paste(fb, (int(w * (0.58 if a else 0.38)) - drift, int(h * 0.2)))
    return frame


def make_video(path: Path, *, fps: int = 10, size=(960, 540), duration: float = DURATION) -> Path:
    w, h = size
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-g", str(fps * 2),
           "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    cache: dict = {}
    for i in range(int(duration * fps)):
        t = i / fps
        key = (round(t, 1), )
        frame = cache.get(key) or frame_at(t, size)
        proc.stdin.write(np.asarray(frame, dtype=np.uint8).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed to write the fixture video")
    return path


def make_long_video(path: Path, minutes: float = 10.0, size=(1920, 1080)) -> Path:
    """A 10-minute 1080p video: the 60 s fixture looped, upscaled, re-encoded (H.264)."""
    short = make_video(path.with_suffix(".short.mp4"))
    loops = int(np.ceil(minutes))
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", str(loops - 1), "-i", str(short),
           "-t", str(minutes * 60), "-vf", f"scale={size[0]}:{size[1]},fps=30", "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "28", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    short.unlink()
    return path
