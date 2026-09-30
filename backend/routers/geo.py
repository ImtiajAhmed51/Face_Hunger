"""Timeline and map endpoints (all local; no tile servers are ever contacted)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..deps import db
from ..media_http import media_response
from ..services import geo
from ..services.presenters import _settings

router = APIRouter()


@router.get("/api/timeline")
def timeline(kind: Optional[str] = None):
    """Media counts per capture day, newest first (same ordering as /api/media?sort=date)."""
    return geo.timeline_days(db, kind)


@router.get("/api/map/points")
def map_points(kind: Optional[str] = None):
    return geo.map_points(db, kind)


def _pmtiles_path() -> Optional[Path]:
    settings = _settings()
    raw = (settings.get("map_pmtiles_path") or "").strip()
    if not settings.get("map_tiles_enabled") or not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


@router.get("/api/map/config")
def map_config():
    settings = _settings()
    path = _pmtiles_path()
    info, error = None, None
    if settings.get("map_tiles_enabled") and (settings.get("map_pmtiles_path") or "").strip():
        if path is None:
            error = "The configured PMTiles file was not found."
        else:
            try:
                info = geo.pmtiles_info(path)
            except (OSError, ValueError) as exc:
                error = str(exc)
    return {"tiles_enabled": bool(settings.get("map_tiles_enabled")), "pmtiles": info,
            "pmtiles_url": "/api/map/tiles.pmtiles" if info else None, "error": error}


@router.get("/api/map/tiles.pmtiles")
def map_tiles():
    """The user's own PMTiles file, with HTTP Range support (read by pmtiles.js in the browser)."""
    path = _pmtiles_path()
    if path is None:
        raise HTTPException(404, "Map tiles are disabled or not configured")
    return media_response(path)
