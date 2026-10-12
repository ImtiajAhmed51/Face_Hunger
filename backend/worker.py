"""Single-process background indexing with per-media atomic replacement."""

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from . import duplicates as dup_mod
from . import media_processing as media_io
from . import metadata as metadata_mod
from .engine import deduplicate, iou
from .imaging import NON_WEB_SUFFIXES
from .netlib import LibraryOffline, probe, relative_identity, retry_io
from .netscan import Prefetcher, walk
from .scanner import authorized_root, resolve_inside
from .video.tracker import FaceTracker

Job = dict
_process_guard = threading.Lock()


class Cancelled(Exception):
    pass


class Worker:
    def __init__(self, db, config, engine, store, cluster):
        self.db, self.config, self.engine = db, config, engine
        self.store, self.cluster = store, cluster
        self._condition = threading.Condition()
        self._thread = None
        self._job_id = None
        self._paused = self._cancelled = self._closing = False
        self._guard_fd = None
        self._errors = []
        # Called as on_indexed(media_id, content_changed) after a media item is published.
        self.on_indexed = None
        # Called as on_video_progress(path, seconds, duration) for each sampled video frame.
        self.on_video_progress = self._video_progress
        # Called as on_finished(status) when an index/reconcile job ends.
        self.on_finished = None
        self.data_dir = Path(config.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "thumbnails").mkdir(exist_ok=True)

    @property
    def busy(self):
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    def latest(self):
        return self.db.one("SELECT * FROM jobs WHERE kind='index' ORDER BY id DESC LIMIT 1")

    def _acquire_guard(self):
        if not _process_guard.acquire(blocking=False):
            raise RuntimeError("An indexing or reconciliation worker is already running")
        try:
            self._guard_fd = os.open(self.data_dir / ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "nt":
                import msvcrt
                if os.fstat(self._guard_fd).st_size == 0:
                    os.write(self._guard_fd, b"0")
                os.lseek(self._guard_fd, 0, os.SEEK_SET)
                msvcrt.locking(self._guard_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._release_guard()
            raise RuntimeError("Another process owns this data directory's worker lock")

    def _release_guard(self):
        if self._guard_fd is not None:
            os.close(self._guard_fd)
            self._guard_fd = None
        _process_guard.release()

    def start(self, library_id, force=False, retry_failed=False, quick=False):
        library = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        if library is None:
            raise ValueError("Library does not exist")
        online, reason = probe(library["path"])
        if not online:
            self._library_offline(library, reason)
            raise LibraryOffline(f"\"{library['name']}\" is offline ({reason}). Nothing was changed; it will be scanned when it is back.")
        authorized_root(library["path"], self.config.roots)
        return self._launch(library_id, lambda: self._index(library, bool(force), bool(retry_failed), bool(quick)))

    def _library_offline(self, library, reason):
        with self.db.connect() as conn:
            conn.execute("UPDATE libraries SET status='offline', status_reason=?, status_changed_at=CURRENT_TIMESTAMP "
                         "WHERE id=? AND status!='offline'", (reason, library["id"]))

    def reconcile(self):
        return self._launch(None, lambda: self.cluster.reconcile(
            callback=self._reconcile_progress, checkpoint=self._checkpoint))

    def _launch(self, library_id, action):
        with self._condition:
            if self._closing:
                raise RuntimeError("Worker is shutting down")
            if self.busy:
                raise RuntimeError("An indexing or reconciliation job is already running")
            self._acquire_guard()
            try:
                with self.db.connect() as conn:
                    self._job_id = conn.execute("INSERT INTO jobs(library_id) VALUES (?)", (library_id,)).lastrowid
                self._paused = self._cancelled = False
                self._errors = []
                job = self.db.one("SELECT * FROM jobs WHERE id=?", (self._job_id,))
                self._thread = threading.Thread(target=self._run, args=(action,), name=f"lfs-job-{self._job_id}", daemon=False)
                self._thread.start()
                return job
            except BaseException:
                self._release_guard()
                raise

    def _refine_track_edges(self, path, tracker, staged, min_quality: float, interval: float, duration: float) -> None:
        """Sampling every ``interval`` s places track edges up to that far from the truth. Probe
        1 s steps just outside each track's first/last sighting and keep faces that match the
        track's identity, so moments are accurate to ~1 s without decoding every frame."""
        if interval <= 1.0:
            return
        probes: dict[float, list] = {}
        for track in tracker.all_tracks():
            for k in range(1, int(interval)):
                for ts in (track.first_ts - k, track.history[-1] + k):
                    if 0 <= ts <= (duration or ts):
                        probes.setdefault(round(ts, 2), []).append(track)
        # At most 2 * (interval - 1) extra detections per track.
        from contextlib import closing
        with closing(media_io.frames_at(path, sorted(probes), self._checkpoint)) as frames:
            for ts, image in frames:
                candidates = probes[ts]
                if image is None:
                    continue
                faces = [f for f in self.engine.detect(image) if float(f.get("quality", f["detection"])) >= min_quality]
                for track in candidates:
                    best = max(faces, key=lambda f: float(f["embedding"] @ track.emb), default=None)
                    if best is None or float(best["embedding"] @ track.emb) < 0.5:
                        continue
                    faces.remove(best)
                    offset, sha = self.store.append(best["embedding"])
                    staged.execute(
                        "INSERT INTO detections(bbox,timestamp,detection,embedding_offset,embedding_sha,jpeg,quality,track_id,"
                        "landmarks) VALUES (?,?,?,?,?,?,?,?,?)",
                        (json.dumps(best["bbox"]), ts, float(best["detection"]), offset, sha,
                         media_io.thumbnail_bytes(image, best["bbox"], 256),
                         float(best.get("quality", best["detection"])), track.id,
                         json.dumps(best["landmarks"]) if best.get("landmarks") is not None else None))
        # Caller commits probe rows and refinement completion together.

    def _video_progress(self, path, seconds: float, duration: float) -> None:
        """Default progress for full scans: per-video position in the job's progress JSON (<= 2/s)."""
        now = time.monotonic()
        if now - getattr(self, "_last_video_write", 0.0) < 0.5 or self._job_id is None:
            return
        self._last_video_write = now
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET progress=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (json.dumps({"current": str(path), "video_seconds": round(seconds, 1),
                                      "video_duration": round(duration, 1)}), self._job_id))

    def _update(self, **fields):
        allowed = {"status", "phase", "total", "processed", "faces", "people", "skipped", "failed", "current_file", "error", "extra"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid job progress fields")
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                         (*fields.values(), self._job_id))

    def _checkpoint(self):
        with self._condition:
            while self._paused and not self._cancelled and not self._closing:
                self._condition.wait(timeout=0.5)
            if self._cancelled or self._closing:
                raise Cancelled("Job cancelled at a safe checkpoint")

    def control(self, job_id, action):
        if action not in ("pause", "resume", "cancel"):
            raise ValueError("Action must be pause, resume, or cancel")
        with self._condition:
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if job is None:
                raise ValueError("Job does not exist")
            if job_id != self._job_id or not self.busy or job["status"] in ("completed", "cancelled", "failed", "interrupted"):
                return job
            if action == "pause":
                self._paused = True
                self._update(status="paused")
            elif action == "resume":
                self._paused = False
                self._update(status="running")
            else:
                self._cancelled, self._paused = True, False
                self._update(phase="cancelling")
            self._condition.notify_all()
            return self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def _run(self, action):
        status, error = "completed", None
        try:
            with self._condition:
                self._update(status="paused" if self._paused else "running")
            self._checkpoint()
            action()
            self._checkpoint()
        except Cancelled:
            status = "cancelled"
        except LibraryOffline as exc:
            # Not a failure and not a reason to forget anything: the scan resumes when the folder is back.
            status, error = "offline", str(exc)
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.cluster.invalidate()
            try:
                with self._condition:
                    if status == "completed" and (self._cancelled or self._closing):
                        status = "cancelled"
                    with self.db.connect() as conn:
                        conn.execute("UPDATE jobs SET status=?,phase=?,current_file=NULL,"
                                     "error=COALESCE(?,error),finished_at=CURRENT_TIMESTAMP WHERE id=?",
                                     (status, status, error, self._job_id))
            finally:
                self._release_guard()
            if self.on_finished is not None:
                try:
                    self.on_finished(status)
                except Exception:
                    pass

    def _reconcile_progress(self, progress):
        self._update(phase=progress["phase"], processed=progress["processed"], total=progress["total"],
                     people=self.db.one("SELECT COUNT(*) n FROM people WHERE face_count>0")["n"])

    def shutdown(self, timeout=None):
        with self._condition:
            self._closing = self._cancelled = True
            self._paused = False
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _index(self, library, force, retry_failed, quick=False):
        root = authorized_root(library["path"], self.config.roots)
        settings = self.db.settings()
        options = json.loads(library.get("settings") or "{}")
        network = library.get("root_kind") == "network"
        started = time.perf_counter()
        self.engine.configure(
            detection_size=settings.get("detection_size", 640),
            multi_scale=bool(settings.get("multi_scale", False)),
        )
        self._update(phase="loading_model")
        status = self.engine.load()
        face_label = status.get("provider_label") or status.get("provider") or "unknown"
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Indexing Device: %s", face_label)
        _log.info("Face Embedding Device: %s", face_label)
        _log.info("Face Detection Device: %s", face_label)
        _log.info("Vector Index Device: CPU (usearch HNSW)")
        self._checkpoint()
        self._update(phase="scanning")
        library_id = library["id"]
        try:
            with tempfile.TemporaryDirectory(prefix=".scan-", dir=self.data_dir) as temporary:
                manifest = sqlite3.connect(Path(temporary) / "manifest.sqlite")
                try:
                    # Everything is compared in a scratch database, so memory does not grow with the library.
                    manifest.execute("CREATE TABLE files(rel_key TEXT PRIMARY KEY, path TEXT, rel_path TEXT, kind TEXT, size INTEGER, mtime_ns INTEGER)")
                    manifest.execute("CREATE TABLE known(rel_key TEXT PRIMARY KEY, id INTEGER, path TEXT, size INTEGER, mtime_ns INTEGER,"
                                     " kind TEXT, ok INTEGER, missing INTEGER, checks INTEGER, rel_dir TEXT)")
                    manifest.execute("CREATE TABLE dirs(rel_dir TEXT PRIMARY KEY, mtime_ns INTEGER, files INTEGER, seconds REAL, listed INTEGER)")
                    known_dirs = None
                    if quick and not force and not retry_failed:
                        known_dirs = {r["rel_dir"]: r["mtime_ns"] for r in self.db.all(
                            "SELECT rel_dir, mtime_ns FROM library_dirs WHERE library_id=?", (library_id,))} or None
                    extra = {"discovered": 0, "current_folder": "", "mode": "quick" if known_dirs else "full", "errors": []}

                    def on_dir(rel_dir, mtime, files, seconds, listed):
                        manifest.execute("INSERT OR REPLACE INTO dirs VALUES (?,?,?,?,?)", (rel_dir, mtime, files, seconds, int(listed)))
                        extra["current_folder"] = rel_dir

                    def on_error(relative, exc):
                        if len(extra["errors"]) < 50:
                            extra["errors"].append({"path": relative, "reason": f"{type(exc).__name__}: {exc.strerror or exc}" if isinstance(exc, OSError) else str(exc)})

                    total = 0
                    for path, rel_path, rel_key, kind, size, mtime_ns in walk(
                            root, library["ignored"], scan_videos=options.get("scan_videos", True) is not False, known_dirs=known_dirs,
                            checkpoint=self._checkpoint, on_dir=on_dir, on_error=on_error, network=network):
                        manifest.execute("INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?)", (rel_key, path, rel_path, kind, size, mtime_ns))
                        total += 1
                        if total % 500 == 0:
                            manifest.commit()
                            extra["discovered"] = total
                            self._update(total=total, extra=json.dumps(extra))
                    last = 0
                    while True:
                        rows = self.db.all("SELECT id,path,rel_path,rel_key,size,mtime_ns,kind,status,missing,missing_checks,deleted_at FROM media "
                                           "WHERE library_id=? AND id>? ORDER BY id LIMIT 5000", (library_id, last))
                        if not rows:
                            break
                        batch = []
                        for r in rows:
                            rel_path, rel_key = r["rel_path"], r["rel_key"]
                            if rel_key is None:
                                try:
                                    rel_path, rel_key = relative_identity(str(root), r["path"])
                                except ValueError:
                                    continue
                            batch.append((rel_key, r["id"], r["path"], r["size"], r["mtime_ns"], r["kind"],
                                          int(r["status"] == "indexed" or r["deleted_at"] is not None), r["missing"], r["missing_checks"],
                                          rel_path.rpartition("/")[0]))
                        manifest.executemany("INSERT OR IGNORE INTO known VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                        last = rows[-1]["id"]
                    manifest.commit()
                    listed_all = known_dirs is None
                    # Unlisted folders (quick mode) are untouched: their files count as present and unchanged.
                    assumed = 0 if listed_all else manifest.execute(
                        "SELECT COUNT(*) FROM known k WHERE k.rel_dir IN (SELECT rel_dir FROM dirs WHERE listed=0)").fetchone()[0]
                    # Files that are back (were marked missing, or had a missing strike) and did not change: clear the mark, no re-index.
                    back = [r[0] for r in manifest.execute(
                        "SELECT k.id FROM files f JOIN known k ON k.rel_key=f.rel_key WHERE (k.missing=1 OR k.checks>0) AND k.ok=1 "
                        "AND k.size=f.size AND k.mtime_ns=f.mtime_ns AND k.kind=f.kind AND k.path=f.path")]
                    select = ("SELECT f.path, f.kind FROM files f LEFT JOIN known k ON k.rel_key=f.rel_key WHERE k.rel_key IS NULL OR k.ok=0 "
                              "OR k.size!=f.size OR k.mtime_ns!=f.mtime_ns OR k.kind!=f.kind OR k.path!=f.path ORDER BY f.path")
                    if force:
                        select = "SELECT path, kind FROM files ORDER BY path"
                    elif retry_failed:
                        select = "SELECT f.path, f.kind FROM files f JOIN known k ON k.rel_key=f.rel_key WHERE k.ok=0 ORDER BY f.path"
                    work = manifest.execute(select).fetchall()
                    grand_total = total + assumed
                    progress = {"processed": grand_total - len(work), "faces": 0, "skipped": grand_total - len(work), "failed": 0}
                    extra["discovered"] = grand_total
                    self._update(phase="indexing", total=grand_total, extra=json.dumps(extra), **progress)
                    if back:
                        with self.db.connect() as conn:
                            conn.executemany("UPDATE media SET missing=0, missing_checks=0 WHERE id=?", [(i,) for i in back])
                    prefetch = Prefetcher(work, concurrency=int(options.get("concurrency", 4) or 4)) if network and work else None
                    loop_started = time.perf_counter()
                    try:
                        for index, (path, kind) in enumerate(work):
                            self._checkpoint()
                            extra["current_folder"] = os.path.dirname(path)
                            if prefetch is not None:
                                done = index + 1
                                elapsed = time.perf_counter() - loop_started
                                extra["mb_per_s"] = round(prefetch.rate(), 2)
                                extra["eta_seconds"] = round(elapsed / done * (len(work) - done)) if done else None
                            self._update(current_file=path, extra=json.dumps(extra))
                            try:
                                failure = prefetch.wait(index) if prefetch is not None else None
                                if failure is not None:
                                    raise failure
                                call = lambda path=path, kind=kind: self._index_file(library, root, Path(path), kind, settings, force, retry_failed)  # noqa: E731
                                outcome, faces = retry_io(call) if network else call()
                                progress["faces"] += faces
                                if outcome == "skipped":
                                    progress["skipped"] += 1
                            except Cancelled:
                                raise
                            except Exception as exc:
                                if isinstance(exc, OSError) and not probe(str(root))[0]:
                                    raise LibraryOffline("The library folder stopped answering during the scan") from exc
                                self.cluster.invalidate()
                                progress["failed"] += 1
                                message = f"{path}: {type(exc).__name__}: {exc}"
                                self._record_failure(library_id, path, kind, message, root)
                                if len(self._errors) < 20:
                                    self._errors.append(message)
                                suffix = f"\n{progress['failed'] - 20} additional failures; see individual media errors." if progress["failed"] > 20 else ""
                                self._update(error="\n".join(self._errors) + suffix)
                            progress["processed"] += 1
                            self._update(**progress, people=self.db.one("SELECT COUNT(*) n FROM people WHERE face_count>0")["n"])
                    finally:
                        if prefetch is not None:
                            extra["bytes_read"] = prefetch.bytes_read
                            extra["mb_per_s"] = round(prefetch.rate(), 2)
                            prefetch.close()
                    self._checkpoint()
                    self._update(phase="checking_missing", current_file=None)
                    self._mark_missing(library, root, manifest, listed_all)
                    with self.db.connect() as conn:
                        rows = manifest.execute("SELECT rel_dir, mtime_ns, files, seconds, listed FROM dirs").fetchall()
                        if listed_all:
                            conn.execute("DELETE FROM library_dirs WHERE library_id=?", (library_id,))
                        conn.executemany("INSERT INTO library_dirs(library_id, rel_dir, mtime_ns, files, seconds) VALUES (?,?,?,?,?) "
                                         "ON CONFLICT(library_id, rel_dir) DO UPDATE SET mtime_ns=excluded.mtime_ns, files=excluded.files, seconds=excluded.seconds",
                                         [(library_id, d, m, f, s) for d, m, f, s, listed in rows if listed])
                        stats = {"mode": extra["mode"], "files": grand_total, "changed": len(work), "failed": progress["failed"],
                                 "seconds": round(time.perf_counter() - started, 2), "bytes_read": extra.get("bytes_read", 0),
                                 "mb_per_s": extra.get("mb_per_s"), "errors": extra["errors"][:50], "full": listed_all,
                                 "full_at": datetime.now().isoformat(timespec="seconds") if listed_all
                                 else (json.loads(library.get("last_scan_stats") or "null") or {}).get("full_at")}
                        conn.execute("UPDATE libraries SET last_scan_at=CURRENT_TIMESTAMP, last_scan_stats=?, status='online', status_reason=NULL "
                                     "WHERE id=?", (json.dumps(stats), library_id))
                    self._update(people=self.db.one("SELECT COUNT(*) n FROM people WHERE face_count>0")["n"], extra=json.dumps(extra))
                finally:
                    manifest.close()
        except LibraryOffline as exc:
            self._library_offline(library, str(exc))
            raise

    def _record_failure(self, library_id, path, kind, message, root=None):
        rel = (None, None)
        if root is not None:
            try:
                rel = relative_identity(str(root), str(path))
            except ValueError:
                pass
        with self.db.connect() as conn:
            conn.execute("INSERT INTO media(library_id,path,name,kind,size,mtime_ns,status,error,rel_path,rel_key) "
                "VALUES (?,?,?,?,0,0,'failed',?,?,?) ON CONFLICT(path) DO UPDATE SET status='failed',error=excluded.error",
                (library_id, str(path), Path(path).name, kind, message, *rel))

    def _index_file(self, library, root, path, kind, settings, force, retry_failed):
        if authorized_root(library["path"], self.config.roots) != root:
            raise ValueError("Registered library target changed during scanning")
        path = resolve_inside(path, root)
        previous = self.db.one("SELECT * FROM media WHERE path=?", (str(path),))
        try:
            rel_path, rel_key = relative_identity(str(root), str(path))
        except ValueError:
            rel_path = rel_key = None
        if previous is None and rel_key is not None:
            # Same file under another spelling (NFC/NFD, letter case, a moved mount point): keep one row.
            twin = self.db.one("SELECT * FROM media WHERE library_id=? AND rel_key=? ORDER BY id LIMIT 1", (library["id"], rel_key))
            if twin is not None:
                with self.db.connect() as conn:
                    conn.execute("UPDATE OR IGNORE media SET path=?, name=? WHERE id=?", (str(path), path.name, twin["id"]))
                previous = self.db.one("SELECT * FROM media WHERE path=?", (str(path),))
        if previous and (previous["deleted_at"] is not None or previous["library_id"] != library["id"]):
            return "skipped", 0
        if retry_failed and not force and (not previous or previous["status"] != "failed"):
            return "skipped", 0
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Media is not a regular file")
        unchanged = previous and (previous["size"], previous["mtime_ns"], previous["kind"]) == (before.st_size, before.st_mtime_ns, kind)
        if unchanged and not force and previous["status"] == "indexed" and not previous["missing"]:
            return "skipped", 0
        with self.db.connect() as conn:
            state = "stale" if previous and previous["indexed_at"] else "pending"
            conn.execute("INSERT INTO media(library_id,path,name,kind,size,mtime_ns,status) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,"
                "kind=excluded.kind,status=excluded.status,error=NULL",
                (library["id"], str(path), path.name, kind, before.st_size, before.st_mtime_ns, state))
            media_id = conn.execute("SELECT id FROM media WHERE path=?", (str(path),)).fetchone()[0]
            conn.execute("UPDATE media SET rel_path=?, rel_key=?, missing_checks=0 WHERE id=?", (rel_path, rel_key, media_id))
        from contextlib import ExitStack
        from .video.checkpoint import VideoCheckpoint, source_fingerprint, InvalidCheckpoint, put
        with ExitStack() as stack:
            cache = None
            if kind == 'video':
                cache = VideoCheckpoint(self.data_dir,media_id,source_fingerprint(path,before,settings,self.engine,self.store))
                staged = stack.enter_context(cache)
            else:
                directory = stack.enter_context(tempfile.TemporaryDirectory(prefix=f'.media-{media_id}-',dir=self.data_dir))
                staged = sqlite3.connect(Path(directory)/'faces.sqlite')
                stack.callback(staged.close)
                staged.row_factory=sqlite3.Row
                staged.execute('CREATE TABLE detections(id INTEGER PRIMARY KEY,bbox TEXT,timestamp REAL,detection REAL,'
                               'embedding_offset INTEGER,embedding_sha TEXT,jpeg BLOB,face_id INTEGER,'
                               'quality REAL DEFAULT .5,track_id INTEGER,landmarks TEXT)')
            try:
                metadata,jpeg,duplicates=self._stage(path,kind,settings,staged)
            except InvalidCheckpoint:
                if cache is None:
                    raise
                staged.rollback()
                staged.execute('DELETE FROM detections');staged.execute('DELETE FROM checkpoint')
                put(staged,'fingerprint',cache.fingerprint);staged.commit()
                metadata,jpeg,duplicates=self._stage(path,kind,settings,staged)
            self._checkpoint()
            if authorized_root(library['path'],self.config.roots)!=root:
                raise ValueError('Registered library target changed during indexing')
            current_path=resolve_inside(path,root);after=current_path.stat()
            if current_path!=path or (before.st_size,before.st_mtime_ns,before.st_ino,before.st_dev)!=(after.st_size,after.st_mtime_ns,after.st_ino,after.st_dev):
                raise ValueError('Media changed during indexing; previous faces were retained. Scan again.')
            started=time.perf_counter()
            outcome=self._publish(media_id,staged,metadata,jpeg,duplicates,settings)
            timings=metadata.get('_profile',{})
            timings['publication_seconds']=round(time.perf_counter()-started,4)
            import logging
            logging.getLogger(__name__).info('Index pipeline profile',extra={'media_id':media_id,'kind':kind,**timings})
            if cache is not None and outcome[0]=='indexed':
                # Close SQLite before unlinking, keeping failures resumable.
                stack.close();cache.completed()
            if outcome[0]=='indexed' and self.on_indexed is not None:
                self.on_indexed(media_id,(previous['content_hash'] if previous else None)!=metadata.get('content_hash'))
            return outcome

    def _stage(self, path, kind, settings, staged):
        from .video.checkpoint import get, put, InvalidCheckpoint
        if kind == 'video':
            staged.execute('CREATE TABLE IF NOT EXISTS checkpoint(key TEXT PRIMARY KEY,value BLOB)')
            ready=get(staged,'ready')
            if ready:
                try:
                    for row in staged.execute('SELECT embedding_offset,embedding_sha FROM detections'):
                        self.store.read(row[0],row[1])
                except ValueError as exc:
                    raise InvalidCheckpoint('Cached embeddings no longer available') from exc
            if ready:
                row=staged.execute("SELECT value FROM checkpoint WHERE key='thumbnail'").fetchone()
                if not isinstance(ready,dict) or not isinstance(ready.get('metadata'),dict) or 'duplicates' not in ready or row is None:
                    raise InvalidCheckpoint('Incomplete video checkpoint')
                return ready['metadata'],bytes(row[0]),ready['duplicates']
        profile = (get(staged,'profile',{}) if kind=='video' else {})
        for key in ('metadata_seconds','decode_seconds','inference_seconds','embedding_write_seconds','thumbnail_seconds','hash_seconds'):
            profile.setdefault(key,0.0)
        profile.setdefault('sampled_frames',0);profile.setdefault('inference_calls',0)
        started=time.perf_counter()
        min_quality = float(settings.get("min_face_quality", 0.25))
        adaptive = bool(settings.get("adaptive_video", True))
        photo = None
        capture = metadata_mod.extract(path, kind)
        if kind == "photo":
            metadata = {}
            photo = media_io.load_image(path)  # decoded once (RAW: embedded preview), reused below
            frames = iter([(None, photo)])
            metadata["duration"] = None
            # Formats a browser cannot display get their viewer preview cached at index time.
            metadata["_preview"] = (media_io.thumbnail_bytes(photo, max_size=2560)
                                    if Path(path).suffix.lower() in NON_WEB_SUFFIXES else None)
        else:
            metadata = media_io.video_metadata(path)
            metadata.pop("captured_at", None)
            sampler=get(staged,'sampler',{})
            try:
                import base64
                import math
                if not isinstance(sampler,dict) or not math.isfinite(float(sampler.get('next_sample',0))) or float(sampler.get('next_sample',0))<0:
                    raise ValueError('Invalid sample position')
                if sampler.get('gray') and len(base64.b64decode(sampler['gray'],validate=True)) != 160*90:
                    raise ValueError('Invalid sampler image')
            except (ValueError,TypeError) as exc:
                raise InvalidCheckpoint('Invalid video sampler checkpoint') from exc
            frames = iter(()) if get(staged,'frames_complete',False) else media_io.video_frames(
                path, float(settings["video_interval"]), self._checkpoint, adaptive=adaptive,resume_state=sampler
            )
        profile['metadata_seconds']+=time.perf_counter()-started
        media_jpeg, duplicates = None, get(staged,'duplicates',0) if kind=='video' else 0
        if kind=='video':
            thumbnail=staged.execute("SELECT value FROM checkpoint WHERE key='thumbnail'").fetchone()
            media_jpeg=bytes(thumbnail[0]) if thumbnail else None
        tracker = FaceTracker(max_gap=max(6.0, float(settings["video_interval"]) * 3))
        duration = float(metadata.get("duration") or 0)
        previous_frame, cached_detections, cached_records, cached_crops = None, None, None, None
        if kind=='video':
            # Reconstruct tracking from existing embeddings, without inference or decoding.
            from itertools import groupby
            cursor=staged.execute('SELECT * FROM detections WHERE id<=? ORDER BY timestamp,id',(get(staged,'prefix_detection_id',0),))
            columns=[d[0] for d in cursor.description]
            restored=0
            for stamp,group in groupby(cursor,key=lambda row:row['timestamp'] if isinstance(row,sqlite3.Row) else row[columns.index('timestamp')]):
                self._checkpoint()
                group=[dict(row) if isinstance(row,sqlite3.Row) else dict(zip(columns,row)) for row in group]
                try:
                    detections=[{'bbox':json.loads(row['bbox']),'embedding':self.store.read(row['embedding_offset'],row['embedding_sha']),
                                 'quality':row['quality'],'detection':row['detection']} for row in group]
                except ValueError as exc:
                    raise InvalidCheckpoint('Cached face records no longer compatible') from exc
                ids=tracker.update(float(stamp),detections)
                if ids != [row['track_id'] for row in group]:
                    raise InvalidCheckpoint('Video checkpoint tracking is incompatible')
                restored+=len(group)
            profile['checkpoint_faces_reused']=restored
        def measured_frames(iterator):
            iterator=iter(iterator)
            while True:
                start=time.perf_counter()
                try: frame=next(iterator)
                except StopIteration: return
                profile['decode_seconds']+=time.perf_counter()-start
                yield frame
        try:
            for timestamp, image in measured_frames(frames):
                self._checkpoint()
                if timestamp is not None and self.on_video_progress is not None:
                    self.on_video_progress(path, float(timestamp), duration)
                if media_jpeg is None:
                    metadata["height"], metadata["width"] = image.shape[:2]
                    media_jpeg = media_io.thumbnail_bytes(image)
                # Only exact decoded-pixel equality permits reuse. Keep every
                # timestamp for tracking; never infer equality from a small preview.
                signature = (image.shape, hashlib.sha256(memoryview(image)).digest()) if kind == "video" else None
                repeated = signature is not None and signature == previous_frame
                started=time.perf_counter()
                detections = cached_detections if repeated else deduplicate(self.engine.detect(image))
                profile['inference_seconds']+=time.perf_counter()-started
                profile['inference_calls']+=int(not repeated)
                profile['sampled_frames']+=1
                duplicates += detections.duplicate_count
                kept = []
                for face in detections:
                    self._checkpoint()
                    quality = float(face.get("quality", face["detection"]))
                    if quality < min_quality:
                        duplicates += 1
                        continue
                    kept.append({**face, "quality": quality})
                # ByteTrack-style association across sampled frames (see backend/video/tracker.py).
                track_ids = tracker.update(float(timestamp), kept) if timestamp is not None else [None] * len(kept)
                started=time.perf_counter()
                records = cached_records if repeated else self.store.append_many([face["embedding"] for face in kept])
                profile['embedding_write_seconds']+=time.perf_counter()-started
                started=time.perf_counter()
                crops = cached_crops if repeated else [media_io.thumbnail_bytes(image, face["bbox"], 256) for face in kept]
                profile['thumbnail_seconds']+=time.perf_counter()-started
                previous_frame, cached_detections, cached_records, cached_crops = signature, detections, records, crops
                for face, track_id, (offset, sha), jpeg in zip(kept, track_ids, records, crops):
                    staged.execute(
                        "INSERT INTO detections(bbox,timestamp,detection,embedding_offset,embedding_sha,jpeg,quality,track_id,"
                        "landmarks) VALUES (?,?,?,?,?,?,?,?,?)",
                        (json.dumps(face["bbox"]), timestamp, float(face["detection"]),
                         offset, sha, jpeg, face["quality"], track_id,
                         json.dumps(face["landmarks"]) if face.get("landmarks") is not None else None),
                    )
                if kind=='video':
                    put(staged,'sampler',sampler);put(staged,'duplicates',duplicates);put(staged,'profile',profile)
                    put(staged,'prefix_detection_id',staged.execute('SELECT COALESCE(MAX(id),0) FROM detections').fetchone()[0])
                    staged.execute("INSERT OR IGNORE INTO checkpoint(key,value) VALUES ('thumbnail',?)",(media_jpeg,))
                staged.commit()
            if kind=='video':
                put(staged,'frames_complete',True);staged.commit()
        finally:
            if hasattr(frames, "close"):
                frames.close()
        if kind == "video" and tracker.all_tracks() and not get(staged,'refinement_complete',False):
            started=time.perf_counter()
            self._refine_track_edges(path, tracker, staged, min_quality, float(settings["video_interval"]), duration)
            profile['refinement_seconds']=time.perf_counter()-started
            put(staged,'refinement_complete',True);staged.commit()
        if media_jpeg is None:
            raise ValueError("No decodable image or video frames")

        # --- exact + perceptual hashes ---
        started=time.perf_counter()
        try:
            metadata["content_hash"] = dup_mod.content_hash(path)
        except Exception:
            metadata["content_hash"] = None

        try:
            if kind == "photo":
                metadata["phash"] = dup_mod.image_phash(photo)
            else:
                metadata["phash"] = dup_mod.video_phash(
                    path,
                    metadata.get("duration"),
                    frame_at_fn=lambda p, t: media_io.frame_at(p, t),
                )
        except Exception:
            metadata["phash"] = None

        metadata.update(capture)
        # Visual/text embeddings are computed by the embedding backfill job, not inline.
        metadata["dino_offset"] = None
        metadata["dino_sha"] = None

        profile['hash_seconds']+=time.perf_counter()-started
        metadata['_profile']={key:round(value,4) if isinstance(value,float) else value for key,value in profile.items()}
        if kind=='video':
            put(staged,'ready',{'metadata':metadata,'duplicates':duplicates});staged.commit()
        return metadata, media_jpeg, duplicates

    def _cancel_only(self):
        if self._cancelled or self._closing:
            raise Cancelled("Job cancelled before media commit")

    def _publish(self, media_id, staged, metadata, jpeg, duplicates, settings):
        thumbnail_dir = self.data_dir / "thumbnails"
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            media = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
            if media is None or media["deleted_at"] is not None:
                return "skipped", 0
            conn.execute("CREATE TEMP TABLE prior AS SELECT f.*,p.name AS person_name,"
                "EXISTS(SELECT 1 FROM rejections r WHERE r.face_id=f.id) AS has_rejections,0 AS reused "
                "FROM faces f LEFT JOIN people p ON p.id=f.person_id WHERE f.media_id=?", (media_id,))
            conn.execute("CREATE INDEX prior_timestamp ON prior(timestamp,reused)")
            conn.execute("CREATE INDEX prior_id ON prior(id)")
            affected = {r[0] for r in conn.execute("SELECT DISTINCT person_id FROM prior WHERE person_id IS NOT NULL")}
            conn.execute("UPDATE faces SET deleted_at=COALESCE(deleted_at,CURRENT_TIMESTAMP) WHERE media_id=?", (media_id,))
            conn.execute("UPDATE media SET missing=0 WHERE id=?", (media_id,))
            for detection in staged.execute("SELECT * FROM detections ORDER BY id"):
                self._cancel_only()
                timestamp = detection["timestamp"]
                if timestamp is None:
                    candidates = conn.execute("SELECT * FROM prior WHERE reused=0 AND timestamp IS NULL")
                else:
                    candidates = conn.execute("SELECT * FROM prior WHERE reused=0 AND timestamp BETWEEN ? AND ?",
                                              (timestamp - 0.001, timestamp + 0.001))
                bbox = json.loads(detection["bbox"])
                vector = self.store.read(detection["embedding_offset"], detection["embedding_sha"])
                match, best = None, 0.0
                for old in candidates:
                    overlap = iou(bbox, json.loads(old["bbox"]))
                    if overlap < 0.55:
                        continue
                    try:
                        similarity = float(vector @ self.store.read(old["embedding_offset"], old["embedding_sha"]))
                    except ValueError:
                        continue
                    if similarity >= settings["matching_threshold"] and overlap + similarity > best:
                        match, best = dict(old), overlap + similarity
                quality = float(detection["quality"] if detection["quality"] is not None else 0.5)
                track_id = detection["track_id"]
                values = (detection["bbox"], timestamp, detection["detection"],
                          detection["embedding_offset"], detection["embedding_sha"], quality, track_id,
                          detection["landmarks"])
                if match is not None:
                    face_id = match["id"]
                    conn.execute(
                        "UPDATE faces SET bbox=?,timestamp=?,detection=?,embedding_offset=?,embedding_sha=?,"
                        "quality=?,track_id=?,landmarks=?,deleted_at=? WHERE id=?",
                        (*values, match["deleted_at"], face_id),
                    )
                    conn.execute("UPDATE prior SET reused=1 WHERE id=?", (face_id,))
                else:
                    face_id = conn.execute(
                        "INSERT INTO faces(bbox,timestamp,detection,embedding_offset,embedding_sha,"
                        "quality,track_id,landmarks,media_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (*values, media_id),
                    ).lastrowid
                staged.execute("UPDATE detections SET face_id=? WHERE id=?", (face_id, detection["id"]))
                conn.execute("UPDATE faces SET thumbnail=? WHERE id=?", (str(thumbnail_dir / f"face-{face_id}.jpg"), face_id))
            conn.execute("DELETE FROM faces WHERE id IN (SELECT id FROM prior WHERE reused=0 AND manual=0 "
                         "AND review_state='unreviewed' AND deleted_at IS NULL AND has_rejections=0 "
                         "AND TRIM(COALESCE(person_name,''))='')")
            self.cluster.refresh(conn, affected)
            auto_confirm = bool(settings.get("auto_confirm", True))
            auto_confirm_threshold = float(
                settings.get("auto_confirm_threshold")
                or settings.get("review_threshold")
                or 0.52
            )
            min_quality_confirm = float(settings.get("min_face_quality", 0.35))

            def _review_state(similarity, quality):
                if not auto_confirm or similarity is None:
                    return "unreviewed"
                q = 0.5 if quality is None else float(quality)
                if q < max(0.25, min_quality_confirm * 0.85):
                    return "unreviewed"
                if float(similarity) >= auto_confirm_threshold:
                    return "confirmed"
                return "unreviewed"

            track_leaders = {}
            for detection in staged.execute(
                "SELECT * FROM detections WHERE track_id IS NOT NULL ORDER BY quality DESC, id"
            ):
                tid = detection["track_id"]
                if tid not in track_leaders:
                    track_leaders[tid] = detection["face_id"]
            assigned_tracks = {}
            video_identities = {}

            def remember_identity(pid, vector, quality):
                if pid is None or float(quality or 0) < .5:
                    return
                if pid not in video_identities and len(video_identities) >= 256:
                    return
                references = video_identities.setdefault(pid, [])
                if not any(float(vector @ old) >= .99 for old, _ in references):
                    references.append((vector, float(quality)))
                    references.sort(key=lambda item: -item[1])
                    del references[3:]

            # Publish a complete track, best face first, before matching the next
            # segment. Later segments can then use all its accepted evidence.
            for detection in staged.execute(
                "SELECT * FROM detections ORDER BY COALESCE(track_id,id), "
                "CASE WHEN track_id IS NOT NULL THEN quality ELSE 0 END DESC,id"
            ):
                self._cancel_only()
                face = conn.execute("SELECT * FROM faces WHERE id=?", (detection["face_id"],)).fetchone()
                if face["person_id"] is not None or face["deleted_at"] is not None or face["review_state"] == "rejected":
                    if (face['person_id'] is not None and face['deleted_at'] is None
                            and face['review_state'] != 'rejected'
                            and track_leaders.get(face['track_id']) == face['id']):
                        assigned_tracks[face['track_id']] = (face['person_id'], face['similarity'])
                    if (face['person_id'] is not None and face['deleted_at'] is None
                            and face['review_state'] != 'rejected' and face['track_id'] is not None
                            and (face['manual'] or face['review_state'] == 'confirmed')):
                        remember_identity(face['person_id'], self.store.read(face['embedding_offset'], face['embedding_sha']),
                                          face['quality'])
                    continue
                tid = face["track_id"]
                quality = face["quality"] if face["quality"] is not None else 0.5
                if tid is not None and tid in assigned_tracks:
                    pid, similarity = assigned_tracks[tid]
                    member_vector = self.store.read(face["embedding_offset"], face["embedding_sha"])
                    leader_id = track_leaders[tid]
                    leader_face = conn.execute("SELECT * FROM faces WHERE id=?", (leader_id,)).fetchone()
                    leader_vector = self.store.read(leader_face["embedding_offset"], leader_face["embedding_sha"])
                    track_similarity = float(member_vector @ leader_vector)
                    if track_similarity < float(settings["matching_threshold"]):
                        # Tracking can switch identities; retain this member for review.
                        pid, similarity = None, None
                    elif similarity is not None:
                        similarity = min(similarity, track_similarity)
                    if pid is not None:
                        conflict = conn.execute(
                            "SELECT 1 FROM exclusions WHERE media_id=? AND person_id=? "
                            "UNION ALL SELECT 1 FROM rejections WHERE face_id=? AND person_id=? "
                            "UNION ALL SELECT 1 FROM faces WHERE media_id=? AND person_id=? "
                            "AND deleted_at IS NULL AND id!=? "
                            "AND (timestamp IS NULL OR ABS(timestamp-?)<0.001) LIMIT 1",
                            (media_id, pid, face['id'], pid, media_id, pid, face['id'], face['timestamp']),
                        ).fetchone()
                        if conflict:
                            pid, similarity = None, None
                    if pid is not None and similarity is not None and self.cluster._match_model is not None:
                        from .match_learning import probability, PROBABILITY_FLOOR
                        if probability(self.cluster._match_model, similarity, quality, face['detection']) < PROBABILITY_FLOOR:
                            pid = None
                    state = _review_state(similarity, quality) if pid is not None else "unreviewed"
                    conn.execute(
                        "UPDATE faces SET person_id=?,similarity=?,review_state=? WHERE id=?",
                        (pid, similarity, state, face["id"]),
                    )
                    if pid is not None:
                        affected.add(pid)
                        self.cluster.observe_track_member(pid, member_vector, quality, float(face['detection']))
                        remember_identity(pid, member_vector, quality)
                    continue
                leader_id = track_leaders.get(tid, face["id"]) if tid is not None else face["id"]
                leader = conn.execute("SELECT * FROM faces WHERE id=?", (leader_id,)).fetchone() or face
                vector = self.store.read(leader["embedding_offset"], leader["embedding_sha"])
                pid, similarity = self.cluster.assign(
                    conn, vector, media_id=media_id, timestamp=leader["timestamp"],
                    face_id=leader["id"], threshold=settings["matching_threshold"],
                    video_identities=video_identities if tid is not None else None,
                )
                state = _review_state(similarity, quality) if pid is not None else "unreviewed"
                conn.execute(
                    "UPDATE faces SET person_id=?,similarity=?,review_state=? WHERE id=?",
                    (pid, similarity, state, face["id"]),
                )
                if tid is not None:
                    assigned_tracks[tid] = (pid, similarity)
                    remember_identity(pid, vector, leader['quality'])
                if pid is not None:
                    affected.add(pid)
            self.cluster.refresh(conn, affected)
            conn.execute(
                "UPDATE media SET width=?,height=?,duration=?,captured_at=?,thumbnail=?,duplicate_count=?,"
                "content_hash=?,phash=?,dino_offset=?,dino_sha=?,"
                "date_source=?,gps_lat=?,gps_lon=?,gps_alt=?,camera_make=?,camera_model=?,lens=?,meta_version=?,"
                "status='stale',error=NULL,missing=0,indexed_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    metadata["width"],
                    metadata["height"],
                    metadata.get("duration"),
                    metadata.get("captured_at"),
                    str(thumbnail_dir / f"media-{media_id}.jpg"),
                    duplicates,
                    metadata.get("content_hash"),
                    metadata.get("phash"),
                    metadata.get("dino_offset"),
                    metadata.get("dino_sha"),
                    *(metadata.get(k) for k in ("date_source", "gps_lat", "gps_lon", "gps_alt",
                                                 "camera_make", "camera_model", "lens", "meta_version")),
                    media_id,
                ),
            )
            count = conn.execute("SELECT COUNT(*) FROM faces WHERE media_id=? AND deleted_at IS NULL", (media_id,)).fetchone()[0]
            self._cancel_only()
        staged.commit()
        self._checkpoint()
        media_io.write_thumbnail(thumbnail_dir / f"media-{media_id}.jpg", jpeg)
        if metadata.get("_preview"):
            media_io.write_thumbnail(self.data_dir / "previews" / f"media-{media_id}.jpg", metadata["_preview"])
        for detection in staged.execute("SELECT face_id,jpeg FROM detections ORDER BY id"):
            self._checkpoint()
            media_io.write_thumbnail(thumbnail_dir / f"face-{detection['face_id']}.jpg", detection["jpeg"])
        with self.db.connect() as conn:
            conn.execute("UPDATE media SET status='indexed',error=NULL WHERE id=?", (media_id,))
        return "indexed", count

    def _mark_missing(self, library, root, manifest, listed_all):
        """Mark files that are gone, and only those.

        A file is a candidate only if its folder was listed in this scan and the listing did not
        contain it. Before anything is written the root is probed again: if it does not answer the
        library is offline and nothing changes. On network libraries a file must be absent in two
        scans before it is marked missing.
        """
        library_id = library["id"]
        if authorized_root(root, self.config.roots) != root:
            raise ValueError("Registered library target changed before missing-file verification")
        scope = "" if listed_all else " AND k.rel_dir IN (SELECT rel_dir FROM dirs WHERE listed=1)"
        gone = manifest.execute(
            "SELECT k.id, k.missing, k.checks FROM known k LEFT JOIN files f ON f.rel_key=k.rel_key "
            f"WHERE f.rel_key IS NULL{scope} AND (k.missing=0 OR k.checks<2)").fetchall()
        if not gone:
            return
        online, reason = probe(str(root))
        if not online:
            raise LibraryOffline(f"The library folder stopped answering before the missing-file check ({reason})")
        network = library.get("root_kind") == "network"
        self._checkpoint()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            affected = set()
            for media_id, missing, checks in gone:
                self._cancel_only()
                checks = int(checks or 0) + 1
                now_missing = 1 if (not network or checks >= 2) else 0
                if now_missing and not missing:
                    affected.update(r[0] for r in conn.execute("SELECT DISTINCT person_id FROM faces WHERE media_id=? "
                                                             "AND person_id IS NOT NULL", (media_id,)))
                conn.execute("UPDATE media SET missing=?, missing_checks=? WHERE id=? AND library_id=?",
                             (max(now_missing, int(missing or 0)), min(checks, 2), media_id, library_id))
            self.cluster.refresh(conn, affected)
            self._cancel_only()
