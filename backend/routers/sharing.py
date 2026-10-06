"""Share safely: anonymize faces, strip metadata, export new files."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ..deps import db, services
from ..jobs.manager import PRIORITY, job_row
from ..schemas import ShareBody, SharePeopleBody, SharePreviewBody
from ..services.presenters import _require_csrf
from ..services.sharing import ShareError, normalize_options

router = APIRouter()


@router.post("/api/share/people")
def share_people(body: SharePeopleBody, request: Request):
    """People (and the number of unknown faces) in a selection, to choose who stays visible."""
    _require_csrf(request)
    return services().sharing.people_in(body.media_ids[:5000])


@router.post("/api/share/preview")
def share_preview(body: SharePreviewBody, request: Request):
    """The "after" image for one photo (compare with /api/media/{id}/preview)."""
    _require_csrf(request)
    try:
        data, report = services().sharing.render(body.media_id, body.options.model_dump(), max_side=1600)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ShareError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(data, media_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Faces-Anonymized": str(report["faces_anonymized"]),
        "X-Verified": "1" if report["verified"] else "0"})


@router.post("/api/share")
def share(body: ShareBody, request: Request):
    """Queue an export of new, anonymized, metadata-free files (zip download or a folder)."""
    _require_csrf(request)
    payload = {**body.options.model_dump(), "media_ids": list(dict.fromkeys(body.media_ids))}
    if not payload["media_ids"]:
        raise HTTPException(400, "media_ids required")
    try:
        options = normalize_options(payload)
        if options["destination"]["type"] == "folder":
            services().sharing._folder(options["destination"]["path"])
    except ShareError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job_row(services().jobs.enqueue("share_export", payload, priority=PRIORITY["normal"]))


@router.get("/api/share/{job_id}/download")
def share_download(job_id: int):
    row = db.one("SELECT * FROM jobs WHERE id=? AND kind='share_export'", (job_id,))
    if not row or row["status"] != "completed":
        raise HTTPException(404, "Export not ready")
    path = services().config.data_dir / "exports" / f"share-{job_id}.zip"
    if not path.is_file() or not re.fullmatch(r"share-\d+\.zip", path.name):
        raise HTTPException(404, "Export not found")
    return FileResponse(path, media_type="application/zip", filename="face-hunger-share.zip")
