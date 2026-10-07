"""Opt-in, local-only performance diagnostics.

Off (the default): ``span``/``record``/``count`` return after one ``is None`` check, nothing is
allocated and no file is created. On: timings go to ``data_dir/diagnostics.sqlite`` (a separate
file, so turning it on never touches the library database). Nothing here opens a socket, and
nothing is ever sent anywhere; the only way out is the redacted report the user exports by hand.

Events carry a kind, a short name (operation, route template, model key) and numbers. They never
carry file paths, file names, people or query text.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

KINDS = ("request", "search", "job", "embed", "model_load", "render", "scan")
MAX_EVENTS = 50_000
FLUSH_EVERY, FLUSH_SECONDS = 64, 5.0
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:@{}\- ]+")
REDACTED = "[redacted]"

_active: Optional["Diagnostics"] = None


class _Noop:
    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set(self, **fields):
        pass


NOOP = _Noop()


def span(kind: str, name: str, **fields):
    """``with span("search", "hybrid"):`` times a block. A shared no-op when diagnostics are off."""
    d = _active
    return NOOP if d is None else _Span(d, kind, name, fields)


def record(kind: str, name: str, ms: float, **fields) -> None:
    d = _active
    if d is not None:
        d.record(kind, name, ms, fields)


def count(name: str, hit: bool) -> None:
    d = _active
    if d is not None:
        d.count(name, hit)


def enabled() -> bool:
    return _active is not None


def safe_name(name: str) -> str:
    """Operation names only: path separators and anything unusual are dropped."""
    return _UNSAFE.sub(".", str(name).replace("/", ".").replace("\\", ".")).strip(". ")[:80] or "unnamed"


def _peak_rss_mb() -> float:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except Exception:
        return 0.0


class _Span:
    __slots__ = ("d", "kind", "name", "fields", "started")

    def __init__(self, d, kind, name, fields):
        self.d, self.kind, self.name, self.fields = d, kind, name, fields

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def set(self, **fields):
        self.fields.update(fields)

    def __exit__(self, exc_type, *_):
        if exc_type is not None:
            self.fields["failed"] = 1
        self.d.record(self.kind, self.name, (time.perf_counter() - self.started) * 1000, self.fields)
        return False


class Diagnostics:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "diagnostics.sqlite"
        self._lock = threading.Lock()
        self._events: list[tuple] = []
        self._counts: dict[str, list[int]] = {}
        self._last_flush = time.monotonic()
        self.on = False

    # -- switch -----------------------------------------------------------------------
    def set_enabled(self, value: bool) -> None:
        global _active
        if value:
            self.on = True
            _active = self
        else:
            self.flush()
            self.on = False
            if _active is self:
                _active = None

    def close(self) -> None:
        self.set_enabled(False)

    # -- recording --------------------------------------------------------------------
    def record(self, kind: str, name: str, ms: float, fields: Optional[dict] = None) -> None:
        if kind not in KINDS:
            return
        numbers = {safe_name(k): round(float(v), 3) for k, v in (fields or {}).items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if kind in ("job", "embed", "scan"):
            numbers["peak_rss_mb"] = _peak_rss_mb()
        event = (datetime.now().isoformat(timespec="seconds"), kind, safe_name(name), round(float(ms), 2), json.dumps(numbers))
        with self._lock:
            self._events.append(event)
            due = len(self._events) >= FLUSH_EVERY or time.monotonic() - self._last_flush > FLUSH_SECONDS
        if due:
            self.flush()

    def count(self, name: str, hit: bool) -> None:
        with self._lock:
            self._counts.setdefault(safe_name(name), [0, 0])[0 if hit else 1] += 1

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, kind TEXT, name TEXT, ms REAL, fields TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_kind ON events(kind, name)")
        conn.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, hits INTEGER NOT NULL, misses INTEGER NOT NULL)")
        return conn

    def flush(self) -> None:
        with self._lock:
            events, self._events = self._events, []
            counts, self._counts = self._counts, {}
            self._last_flush = time.monotonic()
        if not events and not counts:
            return
        conn = self._connect()
        try:
            with conn:
                conn.executemany("INSERT INTO events(at, kind, name, ms, fields) VALUES (?,?,?,?,?)", events)
                for name, (hits, misses) in counts.items():
                    conn.execute("INSERT INTO counters(name, hits, misses) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                                 "hits=hits+excluded.hits, misses=misses+excluded.misses", (name, hits, misses))
                conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_EVENTS,))
        finally:
            conn.close()

    def clear(self) -> None:
        with self._lock:
            self._events, self._counts = [], {}
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    # -- reading ----------------------------------------------------------------------
    def summary(self, slow_limit: int = 25) -> dict:
        base = {"enabled": self.on, "events": 0, "slow": [], "operations": [], "model_loads": [], "caches": [],
                "bytes": 0, "since": None}
        if self.on:
            self.flush()
        if not self.path.is_file():
            return base
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT kind, name, ms FROM events ORDER BY kind, name, ms").fetchall()
            groups: dict[tuple, list[float]] = {}
            for r in rows:
                groups.setdefault((r["kind"], r["name"]), []).append(r["ms"])
            operations = [{"kind": k, "name": n, "count": len(v), "p50_ms": v[len(v) // 2], "p95_ms": v[min(len(v) - 1, int(len(v) * 0.95))],
                           "max_ms": v[-1], "total_ms": round(sum(v), 1)} for (k, n), v in groups.items()]
            slow = [{"at": r["at"], "kind": r["kind"], "name": r["name"], "ms": r["ms"], **json.loads(r["fields"])}
                    for r in conn.execute("SELECT * FROM events WHERE kind != 'model_load' ORDER BY ms DESC LIMIT ?", (slow_limit,))]
            loads = [{"at": r["at"], "name": r["name"], "ms": r["ms"]}
                     for r in conn.execute("SELECT * FROM events WHERE kind = 'model_load' ORDER BY id DESC LIMIT 50")]
            caches = [{"name": r["name"], "hits": r["hits"], "misses": r["misses"],
                       "hit_rate": round(r["hits"] / max(1, r["hits"] + r["misses"]), 4)}
                      for r in conn.execute("SELECT * FROM counters ORDER BY name")]
            since = conn.execute("SELECT MIN(at) FROM events").fetchone()[0]
        finally:
            conn.close()
        return {**base, "events": len(rows), "slow": slow, "model_loads": loads, "caches": caches, "since": since,
                "operations": sorted(operations, key=lambda o: -o["total_ms"]), "bytes": self.path.stat().st_size}

    def report(self, *, secrets: list[str], library: dict) -> dict:
        """What the user can export and share: numbers and operation names, scrubbed once more.

        ``secrets`` are strings that must never appear (library folders, people's names, the home
        folder). The recorder never stores them; this pass is the second lock on the same door.
        """
        summary = self.summary(slow_limit=100)
        report = {
            "format": "face-hunger-diagnostics/1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "note": "Local performance timings only. No file paths, file names, people or search text.",
            "system": {"os": platform.system(), "os_release": platform.release(), "machine": platform.machine(),
                       "python": platform.python_version(), "cpus": os.cpu_count()},
            "library": library,
            **{k: summary[k] for k in ("events", "since", "operations", "slow", "model_loads", "caches")},
        }
        needles = sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True)
        pathlike = re.compile(r"(?:[A-Za-z]:\\|/)(?:[^\s/\\\"']+[/\\])+[^\s\"']*")

        def scrub(value):
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, str):
                lowered = value.lower()
                if any(n.lower() in lowered for n in needles) or pathlike.search(value):
                    return REDACTED
            return value

        return scrub(report)
