"""Structured logging with per-request ids.

Every log record carries ``request_id`` from a context variable set by the
HTTP middleware (or ``-`` outside requests), so one user action can be traced
through routers, jobs it enqueues and background threads that copy the id.
``LFS_LOG_FORMAT=json`` (default) writes one JSON object per line;
``text`` is friendlier in a terminal.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone

from . import diagnostics

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "request_id"}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in vars(record).items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else
                         logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).disabled = True  # replaced by our access log with request ids


QUIET_PREFIXES = ("/api/media/", "/api/faces/", "/assets/", "/api/jobs/events")
access_log = logging.getLogger("face_hunger.access")


class RequestIdMiddleware:
    """Pure ASGI middleware (streaming-safe): assigns X-Request-ID and logs each request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or {}).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        started = time.perf_counter()
        status = {"code": 500}

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", rid.encode("latin-1"))]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            path = scope.get("path", "")
            level = logging.DEBUG if path.startswith(QUIET_PREFIXES) and status["code"] < 400 else logging.INFO
            access_log.log(level, "%s %s -> %s", scope.get("method"), path, status["code"],
                           extra={"method": scope.get("method"), "path": path, "status": status["code"],
                                  "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
            if diagnostics._active is not None:
                route = getattr(scope.get("route"), "path", None) or "unmatched"
                diagnostics.record("request", f"{scope.get('method')} {route}", (time.perf_counter() - started) * 1000,
                                   status=status["code"])
            request_id.reset(token)
