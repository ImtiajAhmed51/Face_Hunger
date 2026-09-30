"""Jobs API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..deps import worker
from ..schemas import IndexBody
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
