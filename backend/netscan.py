"""Directory walking and read-ahead for library scans, built for slow and flaky shares.

``walk`` lists folders with ``os.scandir`` (one round trip per folder on SMB, file sizes and
times included), retries transient errors, and can skip folders whose modification time has not
changed since the last scan. ``Prefetcher`` reads the next files in the background with a bounded
number of readers and a per-file time limit, so one stuck file cannot freeze a scan. Both only
ever read.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat as stat_mod
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterator, Optional

from .netlib import LibraryOffline, is_junk_dir, is_junk_file, probe, retry_io, run_with_timeout
from .scanner import IMAGE, VIDEO, resolve_inside


def _key(name: str) -> str:
    return (unicodedata.normalize("NFC", name).rstrip(". ") or name).casefold()


def _patterns(ignored) -> list[str]:
    if isinstance(ignored, str):
        ignored = json.loads(ignored)
    return [str(p).replace("\\", "/").strip("/") for p in (ignored or []) if str(p).strip("/")]


def excluded(relative: str, patterns: list[str]) -> bool:
    parts = relative.split("/")
    return any(relative == p or relative.startswith(p.rstrip("/") + "/") or fnmatch.fnmatchcase(relative, p)
               or ("/" not in p and any(fnmatch.fnmatchcase(part, p) for part in parts)) for p in patterns)


def walk(root: Path, ignored=None, *, scan_videos: bool = True, known_dirs: Optional[dict[str, int]] = None,
         checkpoint: Callable[[], None] = lambda: None, on_dir: Callable[[str, int, int, float, bool], None] = lambda *a: None,
         on_error: Callable[[str, BaseException], None] = lambda *a: None, network: bool = False,
         list_timeout: float = 30.0) -> Iterator[tuple[str, str, str, str, int, int]]:
    """Yield ``(path, rel_path, rel_key, kind, size, mtime_ns)`` for every media file under ``root``.

    ``known_dirs`` (rel_dir -> mtime_ns from the previous scan) turns on the quick mode: a folder
    whose mtime is unchanged is not listed again; its files are assumed unchanged. ``on_dir`` is
    called as ``(rel_dir, mtime_ns, files, seconds, listed)`` for every folder that still exists.
    Raises :class:`LibraryOffline` if the root stops answering.
    """
    root = Path(root)
    patterns = _patterns(ignored)
    pending: list[tuple[Path, str, str]] = [(root, "", "")]
    while pending:
        folder, rel_dir, key_dir = pending.pop()
        checkpoint()
        started = time.perf_counter()

        def listing(folder=folder):
            def read():
                info = os.stat(folder)
                with os.scandir(folder) as it:
                    return info.st_mtime_ns, list(it)
            value, error = run_with_timeout(read, list_timeout)
            if error is not None:
                raise error
            return value

        try:
            if known_dirs is not None and rel_dir in known_dirs:
                mtime = retry_io(lambda folder=folder: os.stat(folder).st_mtime_ns) if network else os.stat(folder).st_mtime_ns
                if mtime == known_dirs[rel_dir]:
                    on_dir(rel_dir, mtime, -1, time.perf_counter() - started, False)
                    prefix = rel_dir + "/" if rel_dir else ""
                    children = {d for d in known_dirs if d != rel_dir and d.startswith(prefix) and "/" not in d[len(prefix):]}
                    pending.extend((folder / c[len(prefix):], c, "/".join(_key(p) for p in c.split("/"))) for c in sorted(children, reverse=True))
                    continue
            mtime, entries = retry_io(listing) if network else listing()
        except OSError as exc:
            online, reason = probe(str(root))
            if not online:
                raise LibraryOffline(f"The library folder stopped answering ({reason})") from exc
            if folder == root:
                raise
            on_error(rel_dir, exc)
            continue
        files = 0
        subdirs = []
        for entry in sorted(entries, key=lambda e: e.name):
            name = entry.name
            relative = f"{rel_dir}/{name}" if rel_dir else name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not is_junk_dir(name) and not excluded(relative, patterns):
                        subdirs.append((Path(entry.path), unicodedata.normalize("NFC", relative), f"{key_dir}/{_key(name)}" if key_dir else _key(name)))
                    continue
                if is_junk_file(name) or name.endswith(".lfs_original") or ".lfs_converted" in name or excluded(relative, patterns):
                    continue
                suffix = os.path.splitext(name)[1].lower()
                kind = "photo" if suffix in IMAGE else "video" if suffix in VIDEO else None
                if kind is None or (kind == "video" and not scan_videos):
                    continue
                path = entry.path
                if entry.is_symlink():
                    # Links are followed only when they stay inside the library.
                    try:
                        path = str(resolve_inside(entry.path, root))
                    except ValueError:
                        continue
                    info = os.stat(path)
                else:
                    info = entry.stat(follow_symlinks=False)
                if not stat_mod.S_ISREG(info.st_mode) or info.st_size == 0:
                    continue  # empty: not a picture yet (often a copy that has only just started)
            except OSError as exc:
                on_error(relative, exc)
                continue
            files += 1
            yield (path, unicodedata.normalize("NFC", relative), f"{key_dir}/{_key(name)}" if key_dir else _key(name),
                   kind, info.st_size, info.st_mtime_ns)
        on_dir(rel_dir, mtime, files, time.perf_counter() - started, True)
        pending.extend(reversed(subdirs))


class Prefetcher:
    """Reads upcoming files ahead of the indexer with ``concurrency`` readers.

    Reading pulls the bytes over the network into the system's file cache while the previous file is
    being decoded; nothing is kept in this process (1 MiB chunks are discarded), so memory stays flat.
    A file that cannot be read within ``file_timeout`` is reported as failed and the scan moves on.
    """

    CHUNK = 1 << 20

    def __init__(self, paths: list[tuple[str, str]], *, concurrency: int = 4, window: int = 8, file_timeout: float = 120.0,
                 video_head: int = 8 << 20, opener: Callable = open):
        self.paths, self.window, self.file_timeout, self.video_head, self.opener = paths, max(1, window), file_timeout, video_head, opener
        self.pool = ThreadPoolExecutor(max_workers=max(1, min(16, concurrency)), thread_name_prefix="fh-read")
        self.futures: dict[int, Future] = {}
        self.bytes_read = 0
        self.seconds = 0.0
        self._lock = threading.Lock()
        self._next = 0

    def _read(self, path: str, kind: str) -> int:
        started, total = time.perf_counter(), 0
        limit = self.video_head if kind == "video" else None

        def attempt():
            nonlocal total
            total = 0
            began = time.perf_counter()  # the limit is per attempt; retries get a fresh one
            with self.opener(path, "rb") as handle:
                while limit is None or total < limit:
                    chunk = handle.read(self.CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if time.perf_counter() - began > self.file_timeout:
                        raise TimeoutError(f"reading took longer than {self.file_timeout:g}s")
            return total

        retry_io(attempt)
        with self._lock:
            self.bytes_read += total
            self.seconds += time.perf_counter() - started
        return total

    def _fill(self, index: int) -> None:
        while self._next < len(self.paths) and self._next <= index + self.window:
            path, kind = self.paths[self._next]
            self.futures[self._next] = self.pool.submit(self._read, path, kind)
            self._next += 1

    def wait(self, index: int) -> Optional[BaseException]:
        """Block until file ``index`` has been read (or failed/timed out). Returns the error, if any."""
        self._fill(index)
        future = self.futures.pop(index)
        try:
            future.result(timeout=self.file_timeout + 5)
            return None
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (TimeoutError,)) or type(exc).__name__ == "TimeoutError":
                return TimeoutError(f"The file could not be read within {self.file_timeout:g} seconds")
            return exc

    def rate(self) -> float:
        """Megabytes per second of reader time."""
        with self._lock:
            return (self.bytes_read / 1e6) / self.seconds if self.seconds > 0 else 0.0

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
