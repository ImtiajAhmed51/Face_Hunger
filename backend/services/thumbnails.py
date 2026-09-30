"""Face thumbnail fallbacks."""

from __future__ import annotations

import io
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

