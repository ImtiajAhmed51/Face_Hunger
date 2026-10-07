"""Opt-in local diagnostics: switch, summary, redacted report. Nothing is sent anywhere."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..deps import db, services
from ..ops import diagnostics
from ..schemas import DiagnosticsClientBody, DiagnosticsToggleBody
from ..services.presenters import _require_csrf

router = APIRouter()


@router.get("/api/diagnostics")
def diagnostics_summary():
    return services().diagnostics.summary()


@router.patch("/api/diagnostics")
def diagnostics_toggle(body: DiagnosticsToggleBody, request: Request):
    _require_csrf(request)
    with db.connect() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES ('diagnostics_enabled', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(bool(body.enabled)),))
    services().diagnostics.set_enabled(bool(body.enabled))
    return services().diagnostics.summary()


@router.delete("/api/diagnostics")
def diagnostics_clear(request: Request):
    """Delete everything recorded so far."""
    _require_csrf(request)
    services().diagnostics.clear()
    return services().diagnostics.summary()


@router.post("/api/diagnostics/client")
def diagnostics_client(body: DiagnosticsClientBody, request: Request):
    """Render timings measured by the page (route name and milliseconds only). Ignored when off."""
    _require_csrf(request)
    if not diagnostics.enabled():
        raise HTTPException(409, "Diagnostics are off")
    diagnostics.record("render", body.name, body.ms)
    return {"ok": True}


def build_report() -> dict:
    s = services()
    secrets = [str(Path.home()), str(Path(s.config.data_dir).resolve()), str(Path(s.config.model_dir).resolve())]
    secrets += [r["path"] for r in db.all("SELECT path FROM libraries")]
    secrets += [r["name"] for r in db.all("SELECT name FROM people WHERE name IS NOT NULL")]
    secrets += [r["name"] for r in db.all("SELECT name FROM albums")]
    counts = db.one("""SELECT COUNT(*) AS items, COALESCE(SUM(kind='video'), 0) AS videos FROM media WHERE deleted_at IS NULL""")
    library = {"items": counts["items"], "videos": counts["videos"], "faces": db.one("SELECT COUNT(*) AS n FROM faces")["n"],
               "models": [space["key"] for space in s.vectors.status()]}
    return s.diagnostics.report(secrets=secrets, library=library)


@router.get("/api/diagnostics/report")
def diagnostics_report():
    """The redacted report as a download. Created only when the user asks for it."""
    body = json.dumps(build_report(), indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="face-hunger-diagnostics.json"', "Cache-Control": "no-store"})
