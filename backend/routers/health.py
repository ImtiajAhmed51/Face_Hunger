"""Health, library integrity and backup/restore endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..deps import services
from ..jobs.manager import PRIORITY, job_row
from ..ops import backup as backup_ops
from ..ops import health as health_ops
from ..schemas import BackupExportBody, BackupRestoreBody, IntegrityCheckBody
from ..services.presenters import _require_csrf

router = APIRouter()
BACKUP_NAME = re.compile(r"^face-hunger-backup-[0-9TZ-]+\.zip$")


@router.get("/api/health")
def health():
    """Liveness/readiness: database, disk, job runner, watcher, models, config warnings."""
    return health_ops.summary(services())


@router.get("/api/health/library")
def library_health():
    s = services()
    latest = s.db.one("SELECT * FROM jobs WHERE kind='integrity_check' ORDER BY id DESC LIMIT 1")
    return {
        "counts": health_ops.library_counts(s.db),
        "storage": health_ops.storage(s),
        "integrity": health_ops.last_integrity(s),
        "integrity_job": job_row(latest),
        "embeddings": s.vectors.status(),
        "restored": s.restored,
    }


@router.post("/api/health/check")
def start_integrity_check(request: Request, body: IntegrityCheckBody | None = None):
    _require_csrf(request)
    payload = {"verify_sample": (body or IntegrityCheckBody()).verify_sample}
    return job_row(services().jobs.enqueue("integrity_check", payload, priority=PRIORITY["normal"],
                                           dedupe_key="integrity_check"))


@router.get("/api/backup")
def list_backups():
    s = services()
    latest = s.db.all("SELECT * FROM jobs WHERE kind IN ('backup_export','backup_restore') ORDER BY id DESC LIMIT 5")
    return {"items": backup_ops.list_backups(s.config.data_dir), "jobs": [job_row(r) for r in latest],
            "restored": s.restored}


@router.post("/api/backup/export")
def start_backup(request: Request, body: BackupExportBody | None = None):
    _require_csrf(request)
    payload = {"include_thumbnails": bool(body and body.include_thumbnails)}
    return job_row(services().jobs.enqueue("backup_export", payload, priority=PRIORITY["normal"],
                                           dedupe_key="backup_export"))


def _backup_path(name: str):
    if not BACKUP_NAME.match(name):
        raise HTTPException(400, "Invalid backup name")
    path = services().config.data_dir / "exports" / name
    if not path.is_file():
        raise HTTPException(404, "Backup not found")
    return path


@router.get("/api/backup/files/{name}")
def download_backup(name: str):
    return FileResponse(_backup_path(name), filename=name, media_type="application/zip")


@router.post("/api/backup/restore")
def restore_backup(body: BackupRestoreBody, request: Request):
    """Verify a backup in data/exports and stage it; it is applied on the next restart.
    Current data is moved to data/backups/pre-restore-<time>/, never deleted."""
    _require_csrf(request)
    _backup_path(body.name)
    return job_row(services().jobs.enqueue("backup_restore", {"name": body.name}, priority=PRIORITY["urgent"]))
