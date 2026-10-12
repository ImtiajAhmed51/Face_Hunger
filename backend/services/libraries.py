"""Library management: where each library lives, whether it is reachable, relinking, polling.

A library whose folder cannot be reached is *offline*. Offline never changes the index: items,
thumbnails, faces and vectors stay searchable from local data, and the library is scanned again
when the folder answers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..netlib import classify, join_root, probe, protect, read_mounts, relative_identity, run_with_timeout

logger = logging.getLogger(__name__)

DEFAULTS = {"scan_videos": True, "poll_minutes": 15, "concurrency": 4, "pause_on_battery": False, "full_scan_hours": 24}


def on_battery() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return "Battery Power" in subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False


class LibraryManager:
    def __init__(self, services, *, mounts=read_mounts, probe_fn=probe, interval: float = 30.0):
        self.s = services
        self.mounts, self.probe, self.interval = mounts, probe_fn, interval
        self.offline_ids: set[int] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        for row in self.s.db.all("SELECT path FROM allowed_roots"):
            self.s.config.allow_root(Path(row["path"]))
        self.offline_ids = {r["id"] for r in self.s.db.all("SELECT id FROM libraries WHERE status='offline'")}
        self.refresh_protection()

    @property
    def db(self):
        return self.s.db

    def refresh_protection(self) -> None:
        """Network library folders are read-only for the whole process (see netlib.install_write_guard)."""
        protect(r["path"] for r in self.db.all("SELECT path FROM libraries WHERE root_kind='network'"))

    # -- consent --------------------------------------------------------------------------
    def allow(self, path: Path) -> None:
        """The user picked this folder in the chooser: remember it as a place libraries may live."""
        path = Path(path)
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO allowed_roots(path) VALUES (?)", (str(path),))
        self.s.config.allow_root(path)

    def recent(self) -> list[str]:
        return [r["path"] for r in self.db.all("SELECT path FROM allowed_roots ORDER BY added_at DESC, rowid DESC LIMIT 8")]

    # -- description ----------------------------------------------------------------------
    @staticmethod
    def options(row: dict) -> dict:
        return {**DEFAULTS, **json.loads(row.get("settings") or "{}")}

    def identify(self, library_id: int) -> dict:
        """Record what kind of place the library is on (and which share), from the mount table."""
        row = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        info = classify(row["path"], self.mounts())
        subpath = None
        if info.get("mount_point"):
            subpath = os.path.relpath(row["path"], info["mount_point"]).replace(os.sep, "/")
            subpath = "" if subpath == "." else subpath
        if info["kind"] == "local" and row.get("root_kind") == "network" and row.get("status") == "offline":
            return row  # an unmounted share looks local: keep what we knew
        with self.db.connect() as conn:
            conn.execute("UPDATE libraries SET root_kind=?, share_protocol=COALESCE(?, share_protocol), share_server=COALESCE(?, share_server), "
                         "share_name=COALESCE(?, share_name), share_subpath=COALESCE(?, share_subpath) WHERE id=?",
                         (info["kind"], info["protocol"], info["server"], info["share"], subpath, library_id))
        self.refresh_protection()
        return self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))

    def describe(self, row: dict) -> dict:
        options = self.options(row)
        counts = self.db.one("""SELECT COUNT(*) AS items, COALESCE(SUM(kind='video'), 0) AS videos, COALESCE(SUM(size), 0) AS bytes,
                                       COALESCE(SUM(status='failed'), 0) AS failed, COALESCE(SUM(missing=1), 0) AS missing
                                FROM media WHERE library_id=? AND deleted_at IS NULL""", (row["id"],))
        ignored = row.get("ignored") or "[]"
        stats = json.loads(row.get("last_scan_stats") or "null")
        next_scan = None
        if row.get("root_kind") == "network" and row.get("status") != "offline" and not row.get("paused") and row.get("last_scan_at"):
            try:
                next_scan = (datetime.fromisoformat(row["last_scan_at"]) + timedelta(minutes=int(options["poll_minutes"]))).isoformat(sep=" ", timespec="seconds")
            except ValueError:
                pass
        return {
            "id": row["id"], "name": row["name"], "path": row["path"],
            "ignored": json.loads(ignored) if isinstance(ignored, str) else ignored,
            "media_count": int(counts["items"]), "video_count": int(counts["videos"]), "bytes": int(counts["bytes"]),
            "failed_count": int(counts["failed"]), "missing_count": int(counts["missing"]),
            "kind": row.get("root_kind") or "local",
            "share": {"protocol": row.get("share_protocol"), "server": row.get("share_server"), "name": row.get("share_name"),
                      "subpath": row.get("share_subpath")} if row.get("share_server") else None,
            "status": row.get("status") or "online", "status_reason": row.get("status_reason"), "status_changed_at": row.get("status_changed_at"),
            "paused": bool(row.get("paused")), "settings": options, "last_scan_at": row.get("last_scan_at"), "last_scan": stats,
            "next_scan_at": next_scan,
        }

    def list(self) -> list[dict]:
        return [self.describe(r) for r in self.db.all("SELECT * FROM libraries ORDER BY id")]

    def update(self, library_id: int, *, name=None, ignored=None, settings=None, paused=None) -> dict:
        row = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        if not row:
            raise KeyError("Library not found")
        options = self.options(row)
        if settings:
            for key, value in settings.items():
                if key in ("scan_videos", "pause_on_battery"):
                    options[key] = bool(value)
                elif key == "poll_minutes":
                    options[key] = max(1, min(1440, int(value)))
                elif key == "concurrency":
                    options[key] = max(1, min(16, int(value)))
        with self.db.connect() as conn:
            conn.execute("UPDATE libraries SET name=COALESCE(?, name), ignored=COALESCE(?, ignored), settings=?, paused=COALESCE(?, paused) WHERE id=?",
                         ((name or "").strip()[:120] or None, None if ignored is None else json.dumps(ignored),
                          json.dumps({k: v for k, v in options.items() if DEFAULTS.get(k) != v}),
                          None if paused is None else int(bool(paused)), library_id))
        return self.describe(self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,)))

    # -- online / offline -----------------------------------------------------------------
    def check(self, row: dict) -> bool:
        online, reason = self.probe(row["path"])
        was_offline = row.get("status") == "offline"
        if online and was_offline:
            with self.db.connect() as conn:
                conn.execute("UPDATE libraries SET status='online', status_reason=NULL, status_changed_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
            self.offline_ids.discard(row["id"])
            logger.info("Library %s is back online", row["id"])
            self.s.watcher.refresh()
        elif not online and not was_offline:
            with self.db.connect() as conn:
                conn.execute("UPDATE libraries SET status='offline', status_reason=?, status_changed_at=CURRENT_TIMESTAMP WHERE id=?",
                             (reason, row["id"]))
            self.offline_ids.add(row["id"])
            logger.warning("Library %s is offline (%s); its items stay searchable", row["id"], reason)
        elif not online:
            self.offline_ids.add(row["id"])
        return online

    def follow_share(self, row: dict) -> bool:
        """The same share mounted somewhere else (``/Volumes/photos-1``)? Point the library at it."""
        if not row.get("share_server") or not row.get("share_name"):
            return False
        for mount in self.mounts():
            if mount["network"] and (mount["server"] or "").lower() == row["share_server"].lower() \
                    and (mount["share"] or "").strip("/").lower() == row["share_name"].strip("/").lower():
                candidate = join_root(mount["mount_point"], row.get("share_subpath") or "")
                if candidate != row["path"] and self.probe(candidate)[0]:
                    report = self.relink(row["id"], candidate, apply=False)
                    if report["checked"] == 0 or report["match_percent"] >= 90:
                        self.relink(row["id"], candidate, apply=True)
                        logger.info("Library %s followed its share to a new mount point", row["id"])
                        return True
        return False

    def tick(self, *, now: Optional[datetime] = None) -> dict:
        """One pass of the monitor: update online/offline, follow remounts, start due scans."""
        now = now or datetime.now()
        report = {"online": [], "offline": [], "scans": [], "relinked": []}
        for row in self.db.all("SELECT * FROM libraries ORDER BY id"):
            was_offline = row.get("status") == "offline"
            online = self.check(row)
            if not online and self.follow_share(row):
                report["relinked"].append(row["id"])
                row = self.db.one("SELECT * FROM libraries WHERE id=?", (row["id"],))
                online = self.check(row)
            report["online" if online else "offline"].append(row["id"])
            if not online or row.get("paused") or self.s.worker.busy:
                continue
            options = self.options(row)
            due = was_offline  # just came back: catch up now
            if row.get("root_kind") == "network":
                last = row.get("last_scan_at")
                try:
                    due = due or last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=int(options["poll_minutes"]))
                except ValueError:
                    due = True
            if due and not (options.get("pause_on_battery") and on_battery()):
                try:
                    stats = json.loads(row.get("last_scan_stats") or "null") or {}
                    full_due = not stats.get("full_at") or now - datetime.fromisoformat(stats["full_at"]) >= timedelta(hours=options["full_scan_hours"])
                except (ValueError, TypeError):
                    full_due = True
                try:
                    self.s.worker.start(row["id"], quick=not full_due and not was_offline)
                    report["scans"].append(row["id"])
                except Exception as exc:
                    logger.info("Scheduled scan of library %s not started: %s", row["id"], exc)
        return report

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:
                    logger.exception("Library monitor pass failed")

        self._thread = threading.Thread(target=loop, daemon=True, name="fh-library-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- relink -----------------------------------------------------------------------------
    def relink(self, library_id: int, new_root: str, *, apply: bool = False, sample: int = 60) -> dict:
        """Point a library at a new folder without re-indexing. Reports how well a sample of files matches first."""
        row = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        if not row:
            raise KeyError("Library not found")
        root = Path(new_root).expanduser()
        online, reason = self.probe(str(root))
        if not online:
            raise ValueError(f"That folder cannot be opened ({reason})")
        root = root.resolve()
        other = self.db.one("SELECT id FROM libraries WHERE path=? AND id!=?", (str(root), library_id))
        if other:
            raise ValueError("Another library already uses that folder")
        rows = self.db.all("SELECT id, path, rel_path, size, mtime_ns FROM media WHERE library_id=? AND deleted_at IS NULL AND missing=0 "
                           "AND status='indexed' ORDER BY (id * 7919) % 1009, id LIMIT ?", (library_id, sample))
        matched = checked = 0
        for item in rows:
            rel_path = item["rel_path"]
            if rel_path is None:
                try:
                    rel_path = relative_identity(row["path"], item["path"])[0]
                except ValueError:
                    continue
            checked += 1
            info, error = run_with_timeout(lambda p=join_root(str(root), rel_path): os.stat(p), 5.0)
            if error is None and info is not None and info.st_size == item["size"] and abs(info.st_mtime_ns - item["mtime_ns"]) < 2_000_000_000:
                matched += 1
        report = {"library_id": library_id, "new_root": str(root), "checked": checked, "matched": matched,
                  "match_percent": round(100.0 * matched / checked, 1) if checked else 100.0, "applied": False}
        if not apply:
            return report
        if self.s.worker.busy:
            raise ValueError("Wait for the running scan to finish before relinking")
        self.allow(root)
        with self.db.connect() as conn:
            last = 0
            while True:
                batch = conn.execute("SELECT id, path, rel_path FROM media WHERE library_id=? AND id>? ORDER BY id LIMIT 2000", (library_id, last)).fetchall()
                if not batch:
                    break
                updates = []
                for media_id, path, rel_path in batch:
                    if rel_path is None:
                        try:
                            rel_path, rel_key = relative_identity(row["path"], path)
                            conn.execute("UPDATE media SET rel_path=?, rel_key=? WHERE id=?", (rel_path, rel_key, media_id))
                        except ValueError:
                            continue
                    updates.append((join_root(str(root), rel_path), media_id))
                conn.executemany("UPDATE OR IGNORE media SET path=? WHERE id=?", updates)
                last = batch[-1][0]
            conn.execute("UPDATE libraries SET path=?, status='online', status_reason=NULL, status_changed_at=CURRENT_TIMESTAMP WHERE id=?",
                         (str(root), library_id))
            conn.execute("DELETE FROM library_dirs WHERE library_id=?", (library_id,))
        self.offline_ids.discard(library_id)
        self.identify(library_id)
        self.s.watcher.refresh()
        self.s.cluster.invalidate()
        return {**report, "applied": True}

    # -- health -----------------------------------------------------------------------------
    def health(self, library_id: int) -> dict:
        row = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        if not row:
            raise KeyError("Library not found")
        stats = json.loads(row.get("last_scan_stats") or "null") or {}
        unreadable = [{"name": r["name"], "path": r["rel_path"] or r["name"], "reason": (r["error"] or "").split(": ", 1)[-1][:200]}
                      for r in self.db.all("SELECT name, rel_path, error FROM media WHERE library_id=? AND status='failed' AND deleted_at IS NULL "
                                           "ORDER BY id DESC LIMIT 200", (library_id,))]
        unreadable += [{"name": os.path.basename(e["path"]) or e["path"], "path": e["path"], "reason": e["reason"]} for e in stats.get("errors", [])]
        slowest = self.db.all("SELECT rel_dir AS folder, files, ROUND(seconds, 3) AS seconds FROM library_dirs WHERE library_id=? "
                              "ORDER BY seconds DESC LIMIT 10", (library_id,))
        return {"library": self.describe(row), "unreadable": unreadable, "slowest_folders": slowest,
                "average_mb_per_s": stats.get("mb_per_s"), "last_scan": stats}

    def test_connection(self, library_id: int, *, max_bytes: int = 16 << 20) -> dict:
        """Latency of listing the library folder and read throughput of one existing file. Read-only."""
        row = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        if not row:
            raise KeyError("Library not found")
        online, reason = self.probe(row["path"])
        if not online:
            return {"online": False, "reason": reason}
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            _, error = run_with_timeout(lambda: os.listdir(row["path"]), 10.0)
            if error is not None:
                return {"online": False, "reason": type(error).__name__}
            timings.append((time.perf_counter() - started) * 1000)
        sample = self.db.one("SELECT path, size FROM media WHERE library_id=? AND deleted_at IS NULL AND missing=0 AND size > 0 "
                             "ORDER BY size DESC LIMIT 1", (library_id,))
        throughput = read_bytes = None
        if sample:
            def read():
                total, started = 0, time.perf_counter()
                with open(sample["path"], "rb") as handle:
                    while total < max_bytes:
                        chunk = handle.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                return total, time.perf_counter() - started
            value, error = run_with_timeout(read, 30.0)
            if error is None and value and value[1] > 0:
                read_bytes, throughput = value[0], round(value[0] / 1e6 / value[1], 2)
        return {"online": True, "latency_ms": round(statistics.median(timings), 2), "latency_max_ms": round(max(timings), 2),
                "throughput_mb_per_s": throughput, "bytes_read": read_bytes, "free_bytes_local": shutil.disk_usage(self.s.config.data_dir).free}
