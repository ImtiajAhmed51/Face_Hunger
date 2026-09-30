"""Jobs API routes."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..deps import services, worker
from ..jobs.manager import job_row
from ..schemas import IndexBody, JobBody
from ..services.presenters import _job_row, _require_csrf

router = APIRouter()

@router.post("/api/index")
def start_index(body: IndexBody, request: Request):
    _require_csrf(request)
    try:
        job = worker.start(body.library_id, force=body.force, retry_failed=body.retry_failed)
        return _job_row(job)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/index/status")
def index_status():
    return _job_row(worker.latest())


@router.post("/api/index/{job_id}/pause")
def pause_job(job_id: int, request: Request):
    _require_csrf(request)
    try:
        return _job_row(worker.control(job_id, "pause"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/index/{job_id}/resume")
def resume_job(job_id: int, request: Request):
    _require_csrf(request)
    try:
        return _job_row(worker.control(job_id, "resume"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/index/{job_id}/cancel")
def cancel_job(job_id: int, request: Request):
    _require_csrf(request)
    try:
        return _job_row(worker.control(job_id, "cancel"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/index/reconcile")
def start_reconcile(request: Request):
    _require_csrf(request)
    try:
        job = worker.reconcile()
        return _job_row(job)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Generic job queue (priority, pause/resume/cancel, SSE)
# ---------------------------------------------------------------------------

ENQUEUEABLE = {"embed_backfill", "rebuild_index", "ingest"}


@router.get("/api/jobs")
def list_jobs(status: Optional[str] = None, kind: Optional[str] = None, limit: int = 50):
    return {"items": [job_row(r) for r in services().jobs.list(status=status, kind=kind, limit=limit)],
            "watcher": services().watcher.status()}


@router.post("/api/jobs")
def create_job(body: JobBody, request: Request):
    _require_csrf(request)
    if body.kind not in ENQUEUEABLE:
        raise HTTPException(400, f"kind must be one of {sorted(ENQUEUEABLE)}")
    payload = dict(body.payload or {})
    if body.kind == "ingest":
        paths = payload.get("paths")
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise HTTPException(400, "ingest needs payload.paths: [str]")
    if body.kind == "rebuild_index":
        try:
            services().vectors.get(str(payload.get("key")))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
    job = services().jobs.enqueue(body.kind, payload, priority=body.priority,
                                  dedupe_key=body.kind if body.kind == "embed_backfill" else None)
    return job_row(job)


@router.get("/api/jobs/events")
async def job_events(request: Request, max_seconds: float = Query(0, ge=0, le=3600)):
    """Server-sent events: one ``job`` event per changed job (incl. legacy scans), ~4/s max."""
    jobs = services().jobs

    async def stream():
        sent: dict[int, str] = {}
        started = time.monotonic()
        last_beat = started
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            rows = await asyncio.to_thread(
                jobs.db.all,
                "SELECT * FROM jobs WHERE status IN ('queued','running','paused') OR id IN "
                "(SELECT id FROM jobs ORDER BY id DESC LIMIT 20)")
            for row in rows:
                data = json.dumps(job_row(row))
                if sent.get(row["id"]) != data:
                    sent[row["id"]] = data
                    yield f"event: job\nid: {row['id']}\ndata: {data}\n\n"
            now = time.monotonic()
            if now - last_beat > 15:
                last_beat = now
                yield ": keep-alive\n\n"
            if max_seconds and now - started >= max_seconds:
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    row = services().jobs.get(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return job_row(row)


@router.post("/api/jobs/{job_id}/{action}")
def control_job(job_id: int, action: str, request: Request):
    _require_csrf(request)
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(404, "Unknown action")
    row = services().jobs.get(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    try:
        if (row.get("kind") or "index") == "index":
            return job_row(worker.control(job_id, action))
        return job_row(services().jobs.control(job_id, action))
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
