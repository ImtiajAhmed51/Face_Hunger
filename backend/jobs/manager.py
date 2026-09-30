"""Persistent priority job queue with pause/resume/cancel and crash recovery.

Jobs live in the ``jobs`` table (migration 8 added kind/priority/payload/
progress). Lower ``priority`` runs first. One runner thread executes one job
at a time, so heavy work (models, SQLite writes) never competes with itself.

Handlers receive a :class:`JobContext` and must call ``ctx.checkpoint()``
often (at least every ~0.5 s of work): it blocks while paused and raises
:class:`Cancelled` on cancel, which is how cancel meets its 2 s budget. A
handler that sees ``ctx.should_yield()`` may return early with
``{"yielded": True}``; the job goes back to the queue with its progress and
resumes after the more urgent work. Handlers must be idempotent: after a
crash, running jobs are re-queued and simply run again.

Legacy full-library scans (kind ``index``) are still driven by
:class:`backend.worker.Worker`; they share the table and appear in listings
and the SSE stream but are not scheduled here.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PRIORITY = {"urgent": 10, "normal": 50, "background": 80}
ACTIVE = ("queued", "running", "paused")
FINISHED = ("completed", "failed", "cancelled", "interrupted")
MAX_ATTEMPTS = 3


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JobContext:
    def __init__(self, manager: "JobManager", job: dict):
        self.manager, self.id = manager, job["id"]
        self.kind = job["kind"]
        self.priority = job["priority"]
        self.payload = json.loads(job.get("payload") or "{}")
        self.progress_state = json.loads(job.get("progress") or "{}")
        self._last_write = 0.0

    def checkpoint(self) -> None:
        self.manager._checkpoint(self.id)

    def cancelled(self) -> bool:
        return self.id in self.manager._cancel

    def should_yield(self) -> bool:
        return self.manager._has_more_urgent(self.priority, self.id)

    def progress(self, force: bool = False, **fields) -> None:
        """Record progress; persisted at most 4x/second unless forced."""
        self.progress_state.update(fields)
        now = time.monotonic()
        if force or now - self._last_write >= 0.25:
            self._last_write = now
            self.manager._write(self.id, progress=json.dumps(self.progress_state),
                                processed=int(self.progress_state.get("processed", 0) or 0),
                                total=int(self.progress_state.get("total", 0) or 0),
                                current_file=self.progress_state.get("current"))


Handler = Callable[[JobContext], Optional[dict]]


class JobManager:
    def __init__(self, db):
        self.db = db
        self.handlers: dict[str, Handler] = {}
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._running_id: Optional[int] = None
        self._cancel: set[int] = set()
        self._pause: set[int] = set()
        self.recovered = self._recover()

    # -- registration / lifecycle -------------------------------------------
    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def start(self) -> None:
        with self._cond:
            if self._thread is None or not self._thread.is_alive():
                self._stop = False
                self._thread = threading.Thread(target=self._loop, name="job-runner", daemon=True)
                self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _recover(self) -> list[int]:
        """After a crash/kill: running jobs go back to the queue (bounded retries)."""
        recovered = []
        with self.db.connect() as conn:
            for row in conn.execute("SELECT id, attempts FROM jobs WHERE kind != 'index' AND status='running'").fetchall():
                if row["attempts"] + 1 >= MAX_ATTEMPTS:
                    conn.execute("UPDATE jobs SET status='failed', phase='failed', error=?, finished_at=?, updated_at=? "
                                 "WHERE id=?", ("Interrupted too many times", _now(), _now(), row["id"]))
                else:
                    conn.execute("UPDATE jobs SET status='queued', phase='recovered', attempts=attempts+1, updated_at=? "
                                 "WHERE id=?", (_now(), row["id"]))
                    recovered.append(row["id"])
        return recovered

    # -- queue API ------------------------------------------------------------
    def enqueue(self, kind: str, payload: Optional[dict] = None, *, priority: int = PRIORITY["normal"],
                dedupe_key: Optional[str] = None, merge: Optional[Callable[[dict, dict], dict]] = None) -> dict:
        """Add a job. With ``dedupe_key``, an existing queued (or paused) job with that key is
        reused; ``merge(old_payload, new_payload)`` combines payloads (e.g. file lists)."""
        if kind not in self.handlers:
            raise ValueError(f"Unknown job kind: {kind}")
        payload = payload or {}
        with self._cond:
            with self.db.connect() as conn:
                existing = None
                if dedupe_key:
                    existing = conn.execute(
                        "SELECT * FROM jobs WHERE dedupe_key=? AND status IN ('queued','paused') ORDER BY id LIMIT 1",
                        (dedupe_key,)).fetchone()
                if existing is not None:
                    old = json.loads(existing["payload"] or "{}")
                    new = merge(old, payload) if merge else old
                    conn.execute("UPDATE jobs SET payload=?, priority=MIN(priority, ?), updated_at=? WHERE id=?",
                                 (json.dumps(new), priority, _now(), existing["id"]))
                    job_id = existing["id"]
                else:
                    job_id = conn.execute(
                        "INSERT INTO jobs(kind, priority, payload, dedupe_key, status, phase, updated_at) "
                        "VALUES (?,?,?,?, 'queued', 'queued', ?)",
                        (kind, int(priority), json.dumps(payload), dedupe_key, _now())).lastrowid
            self._cond.notify_all()
        return self.get(job_id)

    def get(self, job_id: int) -> Optional[dict]:
        return self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list(self, *, status: Optional[str] = None, kind: Optional[str] = None, limit: int = 50) -> list[dict]:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
        return self.db.all(sql + " ORDER BY CASE WHEN status IN ('running','paused','queued') THEN 0 ELSE 1 END, "
                           "priority, id DESC LIMIT ?", (*params, max(1, min(limit, 500))))

    def control(self, job_id: int, action: str) -> dict:
        if action not in ("pause", "resume", "cancel"):
            raise ValueError("action must be pause, resume or cancel")
        with self._cond:
            job = self.get(job_id)
            if job is None:
                raise KeyError("Job not found")
            status = job["status"]
            if status in FINISHED:
                return job
            if action == "cancel":
                if status == "running" or job_id == self._running_id:
                    self._cancel.add(job_id)
                    self._pause.discard(job_id)
                    self._write(job_id, phase="cancelling")
                else:
                    self._finish(job_id, "cancelled")
            elif action == "pause":
                if status == "running":
                    self._pause.add(job_id)
                self._write(job_id, status="paused", phase="paused")
            else:
                self._pause.discard(job_id)
                self._write(job_id, status="running" if job_id == self._running_id else "queued",
                            phase="running" if job_id == self._running_id else "queued")
            self._cond.notify_all()
        return self.get(job_id)

    def wait(self, job_id: int, timeout: float = 30.0, statuses=FINISHED) -> dict:
        """Block until a job reaches one of ``statuses`` (tests and CLI use)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job and job["status"] in statuses:
                return job
            with self._cond:
                self._cond.wait(0.05)
        raise TimeoutError(f"Job {job_id} still {self.get(job_id)['status']}")

    # -- internals -------------------------------------------------------------
    def _write(self, job_id: int, **fields) -> None:
        fields["updated_at"] = _now()
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                         (*fields.values(), job_id))

    def _finish(self, job_id: int, status: str, error: Optional[str] = None) -> None:
        self._write(job_id, status=status, phase=status, error=error, current_file=None, finished_at=_now())

    def _checkpoint(self, job_id: int) -> None:
        with self._cond:
            while job_id in self._pause and job_id not in self._cancel and not self._stop:
                self._cond.wait(0.1)
            if job_id in self._cancel:
                raise Cancelled("Cancelled")
            if self._stop:
                raise _Shutdown()

    def _has_more_urgent(self, priority: int, job_id: int) -> bool:
        row = self.db.one("SELECT 1 AS x FROM jobs WHERE status='queued' AND kind != 'index' AND priority < ? AND id != ? LIMIT 1",
                          (priority, job_id))
        return row is not None

    def _next(self) -> Optional[dict]:
        kinds = list(self.handlers)
        if not kinds:
            return None
        return self.db.one(
            f"SELECT * FROM jobs WHERE status='queued' AND kind IN ({','.join('?' * len(kinds))}) ORDER BY priority, id LIMIT 1",
            tuple(kinds))

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._stop:
                    return
                job = self._next()
                if job is None:
                    self._cond.wait(0.5)
                    continue
                self._running_id = job["id"]
                self._write(job["id"], status="running", phase="running", started_at=job.get("started_at") or _now())
            try:
                self._execute(job)
            finally:
                with self._cond:
                    self._running_id = None
                    self._cancel.discard(job["id"])
                    self._pause.discard(job["id"])
                    self._cond.notify_all()

    def _execute(self, job: dict) -> None:
        ctx = JobContext(self, job)
        try:
            result = self.handlers[job["kind"]](ctx) or {}
        except Cancelled:
            ctx.progress(force=True)
            self._finish(job["id"], "cancelled")
            return
        except _Shutdown:
            ctx.progress(force=True)
            self._write(job["id"], status="queued", phase="interrupted")
            return
        except Exception as exc:
            logger.exception("Job %s (%s) failed", job["id"], job["kind"])
            ctx.progress(force=True)
            self._finish(job["id"], "failed", f"{type(exc).__name__}: {exc}")
            return
        ctx.progress(force=True, **{k: v for k, v in result.items() if k != "yielded"})
        if result.get("yielded"):
            self._write(job["id"], status="queued", phase="yielded")
        else:
            self._finish(job["id"], "completed")


class _Shutdown(Exception):
    """Raised at a checkpoint during app shutdown: the job is re-queued, not failed."""


def job_row(row: Optional[dict]) -> Optional[dict]:
    """API shape: the legacy index-job fields plus queue metadata."""
    if not row:
        return None
    return {
        "id": row["id"],
        "kind": row.get("kind") or "index",
        "library_id": row.get("library_id"),
        "status": row["status"],
        "phase": row.get("phase") or row["status"],
        "priority": row.get("priority", 50),
        "total": int(row.get("total") or 0),
        "processed": int(row.get("processed") or 0),
        "faces": int(row.get("faces") or 0),
        "people": int(row.get("people") or 0),
        "skipped": int(row.get("skipped") or 0),
        "failed": int(row.get("failed") or 0),
        "current_file": row.get("current_file"),
        "error": row.get("error"),
        "payload": json.loads(row.get("payload") or "{}"),
        "progress": json.loads(row.get("progress") or "{}"),
        "attempts": int(row.get("attempts") or 0),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
    }
