"""Still-image decoding for every supported format, always EXIF-oriented.

Formats: JPEG, PNG, WebP, TIFF, BMP (Pillow), HEIC/HEIF (pillow-heif), AVIF
(pillow-avif-plugin; Pillow's built-in plugin as a fallback), and camera RAW
(CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2, PEF, SRW via the optional `rawpy`
extra; LibRaw is LGPL and dynamically linked by the rawpy wheel).

Progressive decoding: RAW files carry an embedded camera JPEG. That preview is
decoded first (tens of milliseconds) and is what indexing, thumbnails and the
viewer use; the full demosaic only runs when explicitly requested. JPEGs
decoded for a small target size use Pillow's draft mode (DCT scaling), which
is several times faster than decoding the full frame.

Orientation is applied exactly once, here: EXIF Orientation for Pillow
formats, LibRaw's ``flip`` for RAW demosaics and embedded previews that have
no orientation of their own. Everything downstream (thumbnails, face boxes,
face crops, embeddings, the viewer) sees upright pixels. Originals are only
ever read.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Optional

import numpy as np

RAW_SUFFIXES = frozenset({".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"})
# Formats browsers cannot show natively: the viewer always uses a converted preview for these.
NON_WEB_SUFFIXES = RAW_SUFFIXES | {".heic", ".heif", ".tif", ".tiff", ".bmp"}
PREVIEW_MAX = 2560
MIN_EMBEDDED = 1600  # embedded RAW previews smaller than this are not good enough for indexing
RAW_INDEX_MAX = 4096  # RAW decodes for indexing are capped here (detection runs at <= 960 px)

_register_lock = threading.Lock()
_registered = False


def _pil():
    """Pillow with HEIF (and AVIF, when only available as a plugin) registered once."""
    global _registered
    from PIL import Image

    with _register_lock:
        if not _registered:
            try:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            except ImportError:
                pass
            try:
                # Pillow's own AVIF plugin can be present without any codec (features.check()
                # still says True), so prefer the plugin that bundles libavif + dav1d/aom.
                import pillow_avif  # noqa: F401  (registers itself for AVIF)
            except ImportError:
                pass
            _registered = True
    return Image


def is_raw(path) -> bool:
    return Path(path).suffix.lower() in RAW_SUFFIXES


def _rawpy():
    try:
        import rawpy
    except ImportError as exc:
        raise RuntimeError("Camera RAW support needs the optional extra: pip install 'face-hunger[raw]'") from exc
    return rawpy


# LibRaw flip codes -> PIL transpose to make pixels upright.
_FLIP = {3: "ROTATE_180", 5: "ROTATE_90", 6: "ROTATE_270"}


def _apply_flip(image, flip: int):
    from PIL import Image

    method = _FLIP.get(int(flip or 0))
    return image.transpose(getattr(Image.Transpose, method)) if method else image


def _draft(image, max_side: int) -> None:
    """DCT-scaled JPEG decode to the smallest scale whose long side is >= max_side.

    Pillow requires *both* dimensions to be at least the requested size, so the
    request must follow the image's aspect ratio (a square box would force a
    full-resolution decode for any non-square photo)."""
    w, h = image.size
    if max(w, h) <= max_side:
        return
    scale = max_side / max(w, h)
    image.draft("RGB", (max(1, int(w * scale)), max(1, int(h * scale))))


def raw_preview(path, *, min_side: int = 0, max_side: Optional[int] = None):
    """Embedded camera preview, upright, or None if absent/too small.

    ``max_side`` enables DCT-scaled decoding of the (often full-resolution) JPEG."""
    rawpy = _rawpy()
    Image = _pil()
    from PIL import ImageOps

    with rawpy.imread(str(path)) as raw:
        flip = raw.sizes.flip
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            return None
    if thumb.format == rawpy.ThumbFormat.JPEG:
        image = Image.open(io.BytesIO(thumb.data))
        orientation_tag = image.getexif().get(0x0112, 1)
        if max_side:
            _draft(image, max(max_side, min_side))
        image.load()
        if orientation_tag not in (None, 1):
            image.getexif()[0x0112] = orientation_tag
            image = ImageOps.exif_transpose(image)
        else:
            image = _apply_flip(image, flip)
    else:
        image = _apply_flip(Image.fromarray(np.asarray(thumb.data)), flip)
    image = image.convert("RGB")
    if min_side and max(image.size) < min_side:
        return None
    return image


def raw_full(path, *, half_size: bool = False):
    """Full demosaic (LibRaw applies the orientation itself)."""
    rawpy = _rawpy()
    Image = _pil()
    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, half_size=half_size, output_bps=8, no_auto_bright=False)
    return Image.fromarray(np.ascontiguousarray(rgb))


def open_image(path, *, max_side: Optional[int] = None, full: bool = False):
    """Upright RGB PIL image. ``max_side`` allows fast reduced decodes; ``full`` forces
    a full RAW demosaic instead of the embedded preview."""
    Image = _pil()
    from PIL import ImageOps

    path = Path(path)
    if is_raw(path):
        max_side = max_side or RAW_INDEX_MAX
        image = None if full else raw_preview(path, min_side=min(max_side, MIN_EMBEDDED), max_side=max_side)
        if image is None:
            image = raw_full(path, half_size=not full)
    else:
        with Image.open(path) as source:
            if max_side and source.format == "JPEG":
                _draft(source, max_side)  # DCT-scaled decode, >= requested size
            image = ImageOps.exif_transpose(source)
            image = image.convert("RGB") if image.mode != "RGB" else image.copy()
    # Allow ~10% slack over the target instead of paying for a full resample; large
    # reductions go through the fast integer reduce() first (reducing_gap).
    if max_side and max(image.size) > max_side * 1.1:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS, reducing_gap=3.0)
    return image


def load_bgr(path, *, max_side: Optional[int] = None, full: bool = False) -> np.ndarray:
    rgb = np.asarray(open_image(path, max_side=max_side, full=full))
    return np.ascontiguousarray(rgb[:, :, ::-1])


def exif(path) -> dict:
    """EXIF tags (0th IFD merged with the Exif sub-IFD; GPS under "gps") for any still format.

    RAW: TIFF-based files are read by Pillow directly; otherwise the embedded
    preview's EXIF is used. Returns {} when nothing is readable.
    """
    Image = _pil()
    from PIL import ExifTags

    def collect(image) -> dict:
        data = image.getexif()
        tags = {k: v for k, v in data.items() if k not in (ExifTags.IFD.Exif, ExifTags.IFD.GPSInfo)}
        try:
            tags.update(data.get_ifd(ExifTags.IFD.Exif))
        except Exception:
            pass
        try:
            gps = dict(data.get_ifd(ExifTags.IFD.GPSInfo))
        except Exception:
            gps = {}
        if gps:
            tags["gps"] = gps
        return tags

    path = Path(path)
    try:
        with Image.open(path) as image:
            tags = collect(image)
            if tags:
                return tags
    except Exception:
        pass
    if is_raw(path):
        try:
            rawpy = _rawpy()
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as preview:
                    return collect(preview)
        except Exception:
            pass
    return {}


def dimensions(path) -> tuple[Optional[int], Optional[int]]:
    """Upright (width, height) without decoding pixels where possible."""
    Image = _pil()
    path = Path(path)
    if is_raw(path):
        rawpy = _rawpy()
        with rawpy.imread(str(path)) as raw:
            w, h = raw.sizes.width, raw.sizes.height
            return (h, w) if raw.sizes.flip in (5, 6) else (w, h)
    with Image.open(path) as image:
        w, h = image.size
        # Pillow's TIFF and HEIF readers already report the upright size.
        if image.format in ("TIFF", "HEIF"):
            return w, h
        return (h, w) if image.getexif().get(0x0112, 1) in (5, 6, 7, 8) else (w, h)


def write_jpeg(image, target: Path, *, quality: int = 88) -> Path:
    import os
    import tempfile

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".preview-", suffix=".jpg", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            image.save(handle, format="JPEG", quality=quality, optimize=False, progressive=True)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def preview_path(data_dir: Path, media_id: int, *, full: bool = False) -> Path:
    return Path(data_dir) / "previews" / f"{'full' if full else 'media'}-{media_id}.jpg"


def ensure_preview(source: Path, data_dir: Path, media_id: int, *, full: bool = False) -> Path:
    """Cached, upright viewer JPEG (<= 2560 px; full RAW demosaic capped at 6000 px)."""
    target = preview_path(data_dir, media_id, full=full)
    try:
        if target.is_file() and target.stat().st_mtime_ns >= Path(source).stat().st_mtime_ns:
            return target
    except OSError:
        pass
    image = open_image(source, max_side=6000 if full else PREVIEW_MAX, full=full)
    return write_jpeg(image, target, quality=92 if full else 88)
