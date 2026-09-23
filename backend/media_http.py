"""Small, independently testable media delivery helpers."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from starlette.responses import FileResponse

_preview_lock = threading.Lock()
MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def media_response(path: Path, filename: str | None = None) -> FileResponse:
    """Let Starlette handle byte ranges, validators, HEAD and streamed file IO."""
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(
        path, media_type=media_type, filename=filename or path.name,
        content_disposition_type="inline", headers={"Cache-Control": "private, no-cache"},
    )


def hover_clip(source: Path, cache: Path, ffmpeg: str | None) -> Path:
    """Generate at most four silent seconds, never return the original video.

    One encoder at a time, bounded runtime/output/cache, atomic publication.
    Contention or unavailable codecs simply leaves the client's thumbnail visible.
    """
    if not ffmpeg:
        raise RuntimeError("Video previews require ffmpeg")
    stat = source.stat()
    key = hashlib.sha256(f"{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{key}.mp4"
    if target.is_file():
        return target
    if not _preview_lock.acquire(blocking=False):
        raise RuntimeError("Preview encoder is busy")
    temp: Path | None = None
    try:
        if target.is_file():
            return target
        fd, name = tempfile.mkstemp(suffix=".mp4", prefix="preview-", dir=cache)
        os.close(fd)
        temp = Path(name)
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-threads", "1",
             "-i", str(source), "-t", "4", "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=12",
             "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-fs", str(MAX_PREVIEW_BYTES), str(temp)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=True,
        )
        if not 0 < temp.stat().st_size <= MAX_PREVIEW_BYTES:
            raise RuntimeError("Preview exceeded size budget")
        os.replace(temp, target)
        # The cache contains only generated clips; originals are never modified.
        clips = sorted(cache.glob("*.mp4"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for old in clips[128:]:
            if old != target:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass
        return target
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        _preview_lock.release()
