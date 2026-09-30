"""Face thumbnail fallbacks."""

from __future__ import annotations

import io
from collections import OrderedDict
from pathlib import Path

from fastapi.responses import FileResponse


def _placeholder_face_jpeg() -> bytes:
    """Neutral gray tile so UI never breaks on missing face thumbs."""
    from PIL import Image
    img = Image.new("RGB", (128, 128), (40, 48, 62))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _serve_face_thumb_file(path: Path):
    try:
        if path.is_file() and path.stat().st_size > 0:
            return FileResponse(
                path,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400, immutable"},
            )
    except OSError:
        pass
    return None



_LQIP_CACHE: "OrderedDict[tuple[int, int], str]" = OrderedDict()
_LQIP_LIMIT = 20000


def lqip_data_url(media_id: int, thumbnail: Path) -> str | None:
    """Tiny (<=16 px) JPEG of the cached thumbnail as a data: URL for blur-up placeholders."""
    import base64

    from PIL import Image

    try:
        stat = thumbnail.stat()
    except OSError:
        return None
    key = (media_id, stat.st_mtime_ns)
    cached = _LQIP_CACHE.get(key)
    if cached is not None:
        _LQIP_CACHE.move_to_end(key)
        return cached
    try:
        with Image.open(thumbnail) as image:
            image.draft("RGB", (32, 32))  # DCT-scaled decode: fast, never the full image
            image = image.convert("RGB")
            image.thumbnail((16, 16))
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=40)
    except Exception:
        return None
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    _LQIP_CACHE[key] = url
    while len(_LQIP_CACHE) > _LQIP_LIMIT:
        _LQIP_CACHE.popitem(last=False)
    return url
