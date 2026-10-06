"""File-system watcher: new/changed/removed media in a library -> urgent ingest job.

Events are debounced per path (``quiet_seconds`` without further events and
a stable size) so half-copied files are not indexed. All paths that become
ready together are merged into the single queued ``ingest`` job.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from .handlers import ignorable, merge_paths
from .manager import PRIORITY

logger = logging.getLogger(__name__)


class LibraryWatcher:
    def __init__(self, services, *, quiet_seconds: float = 0.75):
        self.s = services
        self.quiet = quiet_seconds
        self._pending: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._observer = None
        self._watches: dict[str, object] = {}
        self._stop = threading.Event()
        self._flusher: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._observer is not None

    def start(self) -> None:
        if self._observer is not None:
            return
        try:
            from watchdog.observers import Observer
        except ImportError:
            self.error = "watchdog is not installed; new files are picked up by manual scans only"
            logger.warning(self.error)
            return
        self._observer = Observer()
        self._observer.daemon = True
        self._observer.start()
        self._stop.clear()
        self._flusher = threading.Thread(target=self._flush_loop, name="watch-flush", daemon=True)
        self._flusher.start()
        self.refresh()

    def stop(self) -> None:
        self._stop.set()
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(3)
        self._watches.clear()

    def refresh(self) -> None:
        """Watch exactly the registered libraries (call after adding/removing one)."""
        if self._observer is None:
            return
        from watchdog.events import FileSystemEventHandler

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                for attr in ("src_path", "dest_path"):
                    path = getattr(event, attr, None)
                    if path:
                        watcher.notice(path)

        wanted = {}
        for lib in self.s.db.all("SELECT path FROM libraries"):
            root = Path(lib["path"])
            if root.is_dir():
                wanted[str(root.resolve())] = root
        for key in list(self._watches):
            if key not in wanted:
                self._observer.unschedule(self._watches.pop(key))
        for key, root in wanted.items():
            if key not in self._watches:
                try:
                    self._watches[key] = self._observer.schedule(_Handler(), str(root), recursive=True)
                except Exception as exc:
                    logger.warning("Cannot watch %s: %s", root, exc)

    def notice(self, raw: str) -> None:
        path = Path(raw)
        if path.suffix.lower() == ".xmp" and not path.name.startswith("."):
            with self._lock:  # sidecar edited by us or by another application
                self._pending[str(path)] = (time.monotonic(), path.stat().st_size if path.is_file() else -1)
            return
        if ignorable(path):
            return
        try:
            if path.resolve().is_relative_to(self.s.config.data_dir):
                return
        except OSError:
            return
        size = path.stat().st_size if path.is_file() else -1
        with self._lock:
            self._pending[str(path)] = (time.monotonic(), size)

    def _flush_loop(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self.flush()
            except Exception:
                logger.exception("watcher flush failed")

    def flush(self, *, force: bool = False) -> list[str]:
        now = time.monotonic()
        ready = []
        with self._lock:
            for raw, (seen, size) in list(self._pending.items()):
                if not force and now - seen < self.quiet:
                    continue
                path = Path(raw)
                current = path.stat().st_size if path.is_file() else -1
                if current != size and not force:  # still being written
                    self._pending[raw] = (now, current)
                    continue
                ready.append(raw)
                del self._pending[raw]
        sidecars = [r for r in ready if r.lower().endswith(".xmp")]
        media = [r for r in ready if not r.lower().endswith(".xmp")]
        if sidecars:
            self.s.jobs.enqueue("sidecar_sync", {"paths": sidecars}, priority=PRIORITY["urgent"],
                                dedupe_key="sidecar_sync", merge=merge_paths)
        if media:
            self.s.jobs.enqueue("ingest", {"paths": media}, priority=PRIORITY["urgent"],
                                dedupe_key="ingest", merge=merge_paths)
        return ready

    def status(self) -> dict:
        with self._lock:
            pending = len(self._pending)
        return {"running": self.running, "watching": sorted(self._watches), "pending": pending, "error": self.error}
