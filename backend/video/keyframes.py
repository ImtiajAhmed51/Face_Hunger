"""Keyframes by ffmpeg scene-change detection, in one decoding pass.

The filter graph samples the video at ``fps`` (scene scores between samples a
quarter second apart are enough to find cuts), downscales, and selects a frame
when it is the first, when the scene score exceeds ``threshold`` (at least
``min_gap`` seconds after the previous keyframe), or when ``max_gap`` seconds
passed without one. Selected frames are written as small JPEGs and their
timestamps are read from ``showinfo``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_PTS = re.compile(r"pts_time:\s*([0-9.]+)")
_PROGRESS = re.compile(r"out_time_us=(\d+)")


@dataclass
class Keyframe:
    t: float
    jpeg: bytes


class Cancelled(Exception):
    pass


def extract_keyframes(path: Path, *, ffmpeg: str, hwaccel: bool = True, threshold: float = 0.3, fps: float = 4.0, min_gap: float = 1.0,
                      max_gap: float = 30.0, width: int = 480, limit: int = 400, duration: Optional[float] = None,
                      checkpoint: Callable[[], None] = lambda: None,
                      progress: Callable[[float], None] = lambda _t: None) -> list[Keyframe]:
    expr = (f"isnan(prev_selected_t)"
            f"+gt(scene\\,{threshold})*gte(t-prev_selected_t\\,{min_gap})"
            f"+gte(t-prev_selected_t\\,{max_gap})")
    vf = f"fps={fps},scale={width}:-2,select='{expr}',showinfo"
    with tempfile.TemporaryDirectory(prefix="lfs-keyframes-") as tmp:
        out = Path(tmp) / "k%05d.jpg"
        # Hardware decoding (VideoToolbox / VAAPI / D3D11 ...) when available: on a 4K H.264 file
        # it is ~1.5x faster than 2 software threads at ~5% of the CPU. Falls back to software.
        accel = ["-hwaccel", "auto"] if hwaccel else ["-threads", "2"]
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", *accel, "-i", str(path),
               "-an", "-sn", "-vf", vf, "-fps_mode", "vfr", "-q:v", "4", "-frames:v", str(limit),
               "-progress", "pipe:1", "-nostats", str(out)]
        nice = (lambda: os.nice(10)) if hasattr(os, "nice") else None  # background work yields to the UI
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                preexec_fn=nice)
        stderr_lines: list[str] = []
        import threading

        reader = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
        reader.start()
        try:
            for line in proc.stdout:
                match = _PROGRESS.match(line.strip())
                if match:
                    progress(int(match.group(1)) / 1e6)
                try:
                    checkpoint()
                except BaseException:
                    proc.kill()
                    raise
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(5)
        if proc.returncode != 0 and hwaccel:
            return extract_keyframes(path, ffmpeg=ffmpeg, hwaccel=False, threshold=threshold, fps=fps, min_gap=min_gap,
                                     max_gap=max_gap, width=width, limit=limit, duration=duration,
                                     checkpoint=checkpoint, progress=progress)
        if proc.returncode != 0:
            tail = "".join(stderr_lines[-5:]).strip()
            raise RuntimeError(f"ffmpeg scene detection failed: {tail[-300:]}")
        times = [float(m.group(1)) for line in stderr_lines if "Parsed_showinfo" in line and (m := _PTS.search(line))]
        files = sorted(Path(tmp).glob("k*.jpg"))
        return [Keyframe(t, f.read_bytes()) for t, f in zip(times, files)]
