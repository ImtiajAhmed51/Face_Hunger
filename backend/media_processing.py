"""Oriented images, full-length sampled videos, and atomically written JPEGs."""

import math
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

_heif_lock = threading.Lock()
_heif_ready = False


def _image_open(path):
    global _heif_ready
    from PIL import Image

    with _heif_lock:
        if not _heif_ready:
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
                _heif_ready = True
            except ImportError:
                if Path(path).suffix.lower() in (".heic", ".heif"):
                    raise RuntimeError("HEIC/HEIF images require the local pillow-heif dependency")
    return Image.open(path)


def load_image(path):
    """Return EXIF-oriented uint8 BGR pixels, including registered HEIC support."""
    from PIL import ImageOps

    with _image_open(path) as image:
        oriented = ImageOps.exif_transpose(image).convert("RGB")
        return np.ascontiguousarray(np.asarray(oriented)[:, :, ::-1])


def image_metadata(path):
    with _image_open(path) as image:
        exif = image.getexif()
        stamp = exif.get(36867) or exif.get(306)
        captured = None
        if stamp:
            try:
                captured = datetime.strptime(str(stamp).strip(), "%Y:%m:%d %H:%M:%S").isoformat()
            except ValueError:
                pass
        return {"captured_at": captured}


_FFMPEG_CANDIDATES = (
    "ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)
_FFPROBE_CANDIDATES = (
    "ffprobe",
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    "/usr/bin/ffprobe",
)


def _resolve_bin(names):
    """Find an executable by name or absolute path (covers Homebrew outside PATH)."""
    import os
    import shutil
    from pathlib import Path as _Path

    for name in names:
        if os.path.sep in name or name.startswith("/"):
            if _Path(name).is_file() and os.access(name, os.X_OK):
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return None


def _ffmpeg_bin():
    return _resolve_bin(_FFMPEG_CANDIDATES)


def _ffprobe_bin():
    return _resolve_bin(_FFPROBE_CANDIDATES)


def _capture(path):
    """Open with OpenCV, trying several backends (macOS / ffmpeg / default)."""
    import cv2
    from pathlib import Path as _Path

    path = _Path(path)
    if not path.is_file():
        raise ValueError(f"Video file not found: {path}")

    backends = []
    for name in ("CAP_AVFOUNDATION", "CAP_FFMPEG", "CAP_ANY"):
        if hasattr(cv2, name):
            backends.append(getattr(cv2, name))
    backends.append(None)  # default constructor

    last_err = None
    for backend in backends:
        try:
            capture = (
                cv2.VideoCapture(str(path), backend)
                if backend is not None
                else cv2.VideoCapture(str(path))
            )
        except Exception as exc:
            last_err = exc
            continue
        if capture is not None and capture.isOpened():
            # Confirm at least one frame is readable
            pos = capture.get(cv2.CAP_PROP_POS_FRAMES)
            ok, frame = capture.read()
            if ok and frame is not None and frame.size:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0 if pos == 0 else pos)
                if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
                    capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
                return capture
            capture.release()
        elif capture is not None:
            capture.release()
    hint = f" ({last_err})" if last_err else ""
    raise ValueError(
        f"Cannot open video with OpenCV{hint}: {path}. "
        "Will try ffmpeg if available."
    )


def _ffprobe_metadata(path):
    """Duration / size via ffprobe — works for codecs OpenCV rejects."""
    import json
    import subprocess

    ffprobe = _ffprobe_bin()
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_streams", "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        data = json.loads(proc.stdout)
    except Exception:
        return None
    width = height = None
    fps = None
    duration = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        width = int(stream["width"]) if stream.get("width") else width
        height = int(stream["height"]) if stream.get("height") else height
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
        try:
            num, den = rate.split("/")
            val = float(num) / float(den) if float(den) else 0.0
            if val > 0:
                fps = val
        except Exception:
            pass
        if stream.get("duration"):
            try:
                duration = float(stream["duration"])
            except Exception:
                pass
        break
    if duration is None:
        try:
            duration = float((data.get("format") or {}).get("duration") or 0) or None
        except Exception:
            duration = None
    if not width or not height:
        return None
    return {"width": width, "height": height, "duration": duration, "fps": fps or 25.0,
            "captured_at": None}


def video_metadata(path):
    """Prefer OpenCV; fall back to ffprobe. Soft defaults if both fail but ffmpeg exists."""
    try:
        import cv2
        capture = _capture(path)
        try:
            fps, count = capture.get(cv2.CAP_PROP_FPS), capture.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = count / fps if fps > 0 and count > 0 else None
            if duration is not None and not math.isfinite(duration):
                duration = None
            return {"width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)), "duration": duration,
                    "captured_at": None}
        finally:
            capture.release()
    except ValueError:
        pass

    meta = _ffprobe_metadata(path)
    if meta is not None:
        return {
            "width": meta["width"], "height": meta["height"],
            "duration": meta["duration"], "captured_at": None,
        }

    # Last resort: if ffmpeg can run, let frame sampling fill in real size later.
    if _ffmpeg_bin():
        return {"width": 0, "height": 0, "duration": None, "captured_at": None}

    raise ValueError(
        f"Cannot open video (unsupported or corrupt codec): {path}. "
        "ffmpeg not found — install with: brew install ffmpeg "
        "(and restart the server so PATH includes /opt/homebrew/bin)."
    )


def _read_mjpeg_frames(stream):
    """Yield JPEG images from an MJPEG byte stream (ffmpeg image2pipe)."""
    import cv2

    buf = b""
    SOI, EOI = b"\xff\xd8", b"\xff\xd9"
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf = b""
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                buf = buf[start:]
                break
            jpeg = buf[start : end + 2]
            buf = buf[end + 2 :]
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if image is not None and image.size:
                yield np.ascontiguousarray(image)


def _ffmpeg_frames(path, interval, checkpoint=None):
    """Sample frames via ffmpeg MJPEG pipe. Works when OpenCV cannot open the file."""
    import subprocess

    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        raise ValueError(
            f"Cannot open video and ffmpeg was not found: {path}. "
            "Install with: brew install ffmpeg  "
            "(server must see /opt/homebrew/bin — restart after install)."
        )
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Video sampling interval must be positive and finite")
    # MJPEG pipe avoids needing width/height up front (robust for odd containers).
    vf = f"fps=1/{interval}"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-err_detect", "ignore_err",
        "-i", str(path),
        "-vf", vf,
        "-f", "image2pipe", "-vcodec", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    index = 0
    try:
        assert proc.stdout is not None
        for frame in _read_mjpeg_frames(proc.stdout):
            if checkpoint:
                checkpoint()
            stamp = float(index) * float(interval)
            yield stamp, frame
            index += 1
        if index == 0:
            err = (proc.stderr.read() if proc.stderr else b"")[:500]
            detail = err.decode("utf-8", errors="replace").strip()
            raise ValueError(
                f"ffmpeg produced no frames for: {path}"
                + (f" ({detail})" if detail else "")
            )
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def video_frames(path, interval=3.0, checkpoint=None, adaptive=True):
    """Decode sequentially to EOF, yielding (seconds, BGR) at interval spacing.

    Tries OpenCV first (multiple backends). Falls back to ffmpeg for codecs
    OpenCV cannot open (old WMV/AVI/MPG, odd Takeout exports).
    """
    import cv2

    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Video sampling interval must be positive and finite")

    try:
        capture = _capture(path)
    except ValueError:
        yield from _ffmpeg_frames(path, interval, checkpoint)
        return

    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            fps = 25.0
        index, next_sample, previous = 0, 0.0, -1.0
        prev_gray = None
        min_iv = max(0.33, interval / 3.0)
        max_iv = interval * 2.0
        current_iv = float(interval)
        while True:
            if checkpoint:
                checkpoint()
            if not capture.grab():
                break
            stamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if not math.isfinite(stamp) or stamp <= previous:
                stamp = index / fps
            stamp = max(stamp, previous)
            previous = stamp
            index += 1
            if stamp + 1e-6 < next_sample:
                continue
            ok, image = capture.retrieve()
            if not ok or image is None or not image.size:
                if index > 1:
                    break
                raise ValueError(f"Cannot decode video frame near {stamp:.3f}s")
            frame = np.ascontiguousarray(image)
            if adaptive:
                try:
                    small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
                    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                    if prev_gray is not None:
                        diff = float(np.mean(cv2.absdiff(gray, prev_gray))) / 255.0
                        if diff > 0.08:
                            current_iv = max(min_iv, interval * 0.4)
                        elif diff < 0.015:
                            current_iv = min(max_iv, interval * 1.8)
                        else:
                            current_iv = float(interval)
                    prev_gray = gray
                except Exception:
                    current_iv = float(interval)
            yield float(stamp), frame
            next_sample = stamp + current_iv
        if not index:
            raise ValueError("Video contains no decodable frames")
    finally:
        capture.release()


def frame_at(path, seconds):
    """Read the nearest seekable frame for a stored video face timestamp."""
    import cv2
    import subprocess

    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Frame timestamp must be nonnegative and finite")

    try:
        capture = _capture(path)
    except ValueError:
        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            raise ValueError(f"Cannot open video and ffmpeg is missing: {path}")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", f"{seconds:.3f}",
            "-i", str(path),
            "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "mjpeg",
            "-q:v", "3",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout:
            raise ValueError(f"No decodable video frame at {seconds:.3f}s")
        arr = np.frombuffer(proc.stdout, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None or not image.size:
            raise ValueError(f"No decodable video frame at {seconds:.3f}s")
        return np.ascontiguousarray(image)

    try:
        if seconds and not capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000):
            raise ValueError(f"Video does not support seeking to {seconds:.3f}s")
        ok, image = capture.read()
        if not ok or image is None or not image.size:
            raise ValueError(f"No decodable video frame at {seconds:.3f}s")
        return np.ascontiguousarray(image)
    finally:
        capture.release()


def thumbnail_bytes(bgr, bbox=None, max_size=640):
    from io import BytesIO
    from PIL import Image

    image = np.asarray(bgr)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8 or not image.size:
        raise ValueError("Thumbnail requires a nonempty uint8 BGR image")
    if bbox is not None:
        x, y, width, height = map(float, bbox)
        if not all(math.isfinite(v) for v in (x, y, width, height)) or min(width, height) <= 0:
            raise ValueError("Invalid face bounding box")
        pad = max(width, height) * 0.18
        left, top = max(0, math.floor(x - pad)), max(0, math.floor(y - pad))
        right, bottom = min(image.shape[1], math.ceil(x + width + pad)), min(image.shape[0], math.ceil(y + height + pad))
        image = image[top:bottom, left:right]
        if not image.size:
            raise ValueError("Face bounding box is outside the image")
    rgb = Image.fromarray(np.ascontiguousarray(image[:, :, ::-1]))
    rgb.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    rgb.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def write_thumbnail(path, jpeg):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".thumbnail-", suffix=".jpg", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(jpeg)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(path)


def save_thumbnail(bgr, path, bbox=None, max_size=640):
    return write_thumbnail(path, thumbnail_bytes(bgr, bbox=bbox, max_size=max_size))


def media_thumbnail(bgr, data_dir, media_id):
    return save_thumbnail(bgr, Path(data_dir) / "thumbnails" / f"media-{media_id}.jpg")


def face_thumbnail(bgr, bbox, data_dir, face_id):
    return save_thumbnail(bgr, Path(data_dir) / "thumbnails" / f"face-{face_id}.jpg", bbox, 256)