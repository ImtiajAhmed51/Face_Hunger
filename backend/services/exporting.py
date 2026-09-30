"""Streaming ZIP export of original media."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ..deps import db


def _stream_export(media_ids: list[int], filename: str):
    if len(media_ids) > 5000:
        raise HTTPException(400, "At most 5000 media items per export")
    if not media_ids:
        raise HTTPException(400, "No media to export")

    def generate():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            manifest = []
            for mid in media_ids:
                row = db.one("SELECT * FROM media WHERE id=?", (mid,))
                if not row or row.get("deleted_at") or row.get("missing"):
                    continue
                path = Path(row["path"])
                if not path.is_file():
                    continue
                arcname = f"{row['kind']}s/{path.name}"
                # avoid collisions
                base, ext = path.stem, path.suffix
                counter = 1
                while arcname in {m["archive"] for m in manifest}:
                    arcname = f"{row['kind']}s/{base}_{counter}{ext}"
                    counter += 1
                try:
                    zf.write(path, arcname)
                    manifest.append({
                        "id": mid,
                        "name": row["name"],
                        "kind": row["kind"],
                        "archive": arcname,
                        "path": str(path),
                    })
                except Exception:
                    continue
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        buf.seek(0)
        yield from buf

    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

