"""Single-process background indexing with per-media atomic replacement."""

import json
import os
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path

from . import media_processing as media_io
from . import duplicates as dup_mod
from .engine import deduplicate, iou
from .scanner import authorized_root, resolve_inside, scan

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
        self.data_dir = Path(config.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "thumbnails").mkdir(exist_ok=True)

    @property
    def busy(self):
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    def latest(self):
        return self.db.one("SELECT * FROM jobs ORDER BY id DESC LIMIT 1")

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

    def start(self, library_id, force=False, retry_failed=False):
        library = self.db.one("SELECT * FROM libraries WHERE id=?", (library_id,))
        if library is None:
            raise ValueError("Library does not exist")
        authorized_root(library["path"], self.config.roots)
        return self._launch(library_id, lambda: self._index(library, bool(force), bool(retry_failed)))

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

    def _update(self, **fields):
        allowed = {"status", "phase", "total", "processed", "faces", "people", "skipped", "failed", "current_file", "error"}
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

    def _index(self, library, force, retry_failed):
        root = authorized_root(library["path"], self.config.roots)
        settings = self.db.settings()
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
        try:
            from . import dino as dino_mod
            if dino_mod.available():
                st = dino_mod.status()
                dlabel = st.get("device_label") or st.get("device") or "unknown"
                _log.info("DINOv2 Device: %s", dlabel)
                _log.info("Embedding Device: %s", dlabel)
            else:
                _log.info("DINOv2 Device: unavailable")
        except Exception as exc:
            _log.info("DINOv2 Device: unavailable (%s)", exc)
        # FAISS / vector index is CPU-only (faiss-cpu); no CUDA on Apple Silicon
        try:
            import faiss  # noqa: F401
            _log.info("Vector Index Device: CPU (faiss-cpu; FAISS GPU requires CUDA)")
        except ImportError:
            _log.info("Vector Index Device: CPU (numpy fallback; faiss not installed)")
        self._checkpoint()
        self._update(phase="scanning")
        with tempfile.TemporaryDirectory(prefix=".scan-", dir=self.data_dir) as temporary:
            manifest = sqlite3.connect(Path(temporary) / "manifest.sqlite")
            try:
                manifest.execute("CREATE TABLE files(path TEXT PRIMARY KEY, kind TEXT NOT NULL)")
                total = 0
                for path, kind in scan(library, library["ignored"]):
                    self._checkpoint()
                    manifest.execute("INSERT OR IGNORE INTO files VALUES (?,?)", (str(path), kind))
                    total += 1
                    if total % 100 == 0:
                        manifest.commit()
                        self._update(total=total)
                manifest.commit()
                self._update(phase="indexing", total=total)
                progress = {"processed": 0, "faces": 0, "skipped": 0, "failed": 0}
                for path, kind in manifest.execute("SELECT path,kind FROM files ORDER BY path"):
                    self._checkpoint()
                    self._update(current_file=path)
                    try:
                        outcome, faces = self._index_file(library, root, Path(path), kind, settings, force, retry_failed)
                        progress["faces"] += faces
                        if outcome == "skipped":
                            progress["skipped"] += 1
                    except Cancelled:
                        raise
                    except Exception as exc:
                        self.cluster.invalidate()
                        progress["failed"] += 1
                        message = f"{path}: {type(exc).__name__}: {exc}"
                        self._record_failure(library["id"], path, kind, message)
                        if len(self._errors) < 20:
                            self._errors.append(message)
                        suffix = f"\n{progress['failed'] - 20} additional failures; see individual media errors." if progress["failed"] > 20 else ""
                        self._update(error="\n".join(self._errors) + suffix)
                    progress["processed"] += 1
                    self._update(**progress, people=self.db.one("SELECT COUNT(*) n FROM people WHERE face_count>0")["n"])
                self._checkpoint()
                self._update(phase="checking_missing", current_file=None)
                self._mark_missing(library["id"], root)
                self._update(people=self.db.one("SELECT COUNT(*) n FROM people WHERE face_count>0")["n"])
            finally:
                manifest.close()

    def _record_failure(self, library_id, path, kind, message):
        with self.db.connect() as conn:
            conn.execute("INSERT INTO media(library_id,path,name,kind,size,mtime_ns,status,error) "
                "VALUES (?,?,?,?,0,0,'failed',?) ON CONFLICT(path) DO UPDATE SET status='failed',error=excluded.error",
                (library_id, str(path), Path(path).name, kind, message))

    def _index_file(self, library, root, path, kind, settings, force, retry_failed):
        if authorized_root(library["path"], self.config.roots) != root:
            raise ValueError("Registered library target changed during scanning")
        path = resolve_inside(path, root)
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
        with tempfile.TemporaryDirectory(prefix=f".media-{media_id}-", dir=self.data_dir) as directory:
            staged = sqlite3.connect(Path(directory) / "faces.sqlite")
            staged.row_factory = sqlite3.Row
            try:
                staged.execute("PRAGMA journal_mode=OFF")
                staged.execute(
                    "CREATE TABLE detections(id INTEGER PRIMARY KEY,bbox TEXT,timestamp REAL,detection REAL,"
                    "embedding_offset INTEGER,embedding_sha TEXT,jpeg BLOB,face_id INTEGER,"
                    "quality REAL DEFAULT 0.5,track_id INTEGER)"
                )
                metadata, jpeg, duplicates = self._stage(path, kind, settings, staged)
                self._checkpoint()
                if authorized_root(library["path"], self.config.roots) != root:
                    raise ValueError("Registered library target changed during indexing")
                current_path = resolve_inside(path, root)
                after = current_path.stat()
                if current_path != path or (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
                        after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev):
                    raise ValueError("Media changed during indexing; previous faces were retained. Scan again.")
                return self._publish(media_id, staged, metadata, jpeg, duplicates, settings)
            finally:
                staged.close()

    def _stage(self, path, kind, settings, staged):
        min_quality = float(settings.get("min_face_quality", 0.25))
        adaptive = bool(settings.get("adaptive_video", True))
        if kind == "photo":
            metadata = media_io.image_metadata(path)
            frames = iter([(None, media_io.load_image(path))])
            metadata["duration"] = None
        else:
            metadata = media_io.video_metadata(path)
            frames = media_io.video_frames(
                path, float(settings["video_interval"]), self._checkpoint, adaptive=adaptive
            )
        media_jpeg, duplicates = None, 0
        active_tracks = []
        next_track = 1
        try:
            for timestamp, image in frames:
                self._checkpoint()
                if media_jpeg is None:
                    metadata["height"], metadata["width"] = image.shape[:2]
                    media_jpeg = media_io.thumbnail_bytes(image)
                detections = deduplicate(self.engine.detect(image))
                duplicates += detections.duplicate_count
                frame_faces = []
                for face in detections:
                    self._checkpoint()
                    quality = float(face.get("quality", face["detection"]))
                    if quality < min_quality:
                        duplicates += 1
                        continue
                    offset, sha = self.store.append(face["embedding"])
                    jpeg = media_io.thumbnail_bytes(image, face["bbox"], 256)
                    track_id = None
                    if timestamp is not None:
                        best_iou, best_idx = 0.0, -1
                        for i, tr in enumerate(active_tracks):
                            if timestamp - tr["last_ts"] > float(settings["video_interval"]) * 3:
                                continue
                            overlap = iou(face["bbox"], tr["bbox"])
                            if overlap < 0.3:
                                continue
                            sim = float(face["embedding"] @ tr["emb"])
                            if sim < 0.35:
                                continue
                            score = overlap + sim
                            if score > best_iou:
                                best_iou, best_idx = score, i
                        if best_idx >= 0:
                            track_id = active_tracks[best_idx]["track_id"]
                            active_tracks[best_idx] = {
                                "bbox": face["bbox"],
                                "emb": face["embedding"],
                                "track_id": track_id,
                                "last_ts": timestamp,
                            }
                        else:
                            track_id = next_track
                            next_track += 1
                            active_tracks.append({
                                "bbox": face["bbox"],
                                "emb": face["embedding"],
                                "track_id": track_id,
                                "last_ts": timestamp,
                            })
                    staged.execute(
                        "INSERT INTO detections(bbox,timestamp,detection,embedding_offset,embedding_sha,jpeg,quality,track_id) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (json.dumps(face["bbox"]), timestamp, float(face["detection"]),
                         offset, sha, jpeg, quality, track_id),
                    )
                    frame_faces.append(face)
                if timestamp is not None:
                    active_tracks = [
                        t for t in active_tracks
                        if timestamp - t["last_ts"] <= float(settings["video_interval"]) * 3
                    ]
                staged.commit()
        finally:
            if hasattr(frames, "close"):
                frames.close()
        if media_jpeg is None:
            raise ValueError("No decodable image or video frames")

        # --- exact + perceptual hashes ---
        try:
            metadata["content_hash"] = dup_mod.content_hash(path)
        except Exception:
            metadata["content_hash"] = None

        try:
            if kind == "photo":
                bgr = media_io.load_image(path)
                metadata["phash"] = dup_mod.image_phash(bgr)
            else:
                metadata["phash"] = dup_mod.video_phash(
                    path,
                    metadata.get("duration"),
                    frame_at_fn=lambda p, t: media_io.frame_at(p, t),
                )
        except Exception:
            metadata["phash"] = None

        # --- DINOv2 media embedding (best-effort; never fail indexing) ---
        metadata["dino_offset"] = None
        metadata["dino_sha"] = None
        try:
            from . import dino as dino_mod
            from .media_embeddings import MediaEmbeddingStore

            if dino_mod.available():
                mstore = MediaEmbeddingStore(self.data_dir / "media_embeddings.bin")
                try:
                    from .dino_duplicates import embed_media

                    offset, sha = embed_media(
                        path,
                        kind,
                        metadata.get("duration"),
                        lambda p, t: media_io.frame_at(p, t),
                        mstore,
                    )
                    metadata["dino_offset"] = offset
                    metadata["dino_sha"] = sha
                finally:
                    mstore.close()
        except Exception:
            pass

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
                          detection["embedding_offset"], detection["embedding_sha"], quality, track_id)
                if match is not None:
                    face_id = match["id"]
                    conn.execute(
                        "UPDATE faces SET bbox=?,timestamp=?,detection=?,embedding_offset=?,embedding_sha=?,"
                        "quality=?,track_id=?,deleted_at=? WHERE id=?",
                        (*values, match["deleted_at"], face_id),
                    )
                    conn.execute("UPDATE prior SET reused=1 WHERE id=?", (face_id,))
                else:
                    face_id = conn.execute(
                        "INSERT INTO faces(bbox,timestamp,detection,embedding_offset,embedding_sha,"
                        "quality,track_id,media_id) VALUES (?,?,?,?,?,?,?,?)",
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
            for detection in staged.execute("SELECT * FROM detections ORDER BY id"):
                self._cancel_only()
                face = conn.execute("SELECT * FROM faces WHERE id=?", (detection["face_id"],)).fetchone()
                if face["person_id"] is not None or face["deleted_at"] is not None or face["review_state"] == "rejected":
                    continue
                tid = face["track_id"]
                quality = face["quality"] if face["quality"] is not None else 0.5
                if tid is not None and tid in assigned_tracks:
                    pid, similarity = assigned_tracks[tid]
                    state = _review_state(similarity, quality)
                    conn.execute(
                        "UPDATE faces SET person_id=?,similarity=?,review_state=? WHERE id=?",
                        (pid, similarity, state, face["id"]),
                    )
                    if pid is not None:
                        affected.add(pid)
                    continue
                leader_id = track_leaders.get(tid, face["id"]) if tid is not None else face["id"]
                leader = conn.execute("SELECT * FROM faces WHERE id=?", (leader_id,)).fetchone() or face
                vector = self.store.read(leader["embedding_offset"], leader["embedding_sha"])
                pid, similarity = self.cluster.assign(
                    conn, vector, media_id=media_id, timestamp=face["timestamp"],
                    face_id=face["id"], threshold=settings["matching_threshold"],
                )
                state = _review_state(similarity, quality)
                conn.execute(
                    "UPDATE faces SET person_id=?,similarity=?,review_state=? WHERE id=?",
                    (pid, similarity, state, face["id"]),
                )
                if tid is not None:
                    assigned_tracks[tid] = (pid, similarity)
                if pid is not None:
                    affected.add(pid)
            conn.execute(
                """
                UPDATE faces SET review_state='confirmed'
                WHERE media_id=? AND deleted_at IS NULL AND review_state='unreviewed'
                  AND person_id IS NOT NULL AND manual=0
                  AND id NOT IN (
                    SELECT id FROM (
                      SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY person_id
                        ORDER BY COALESCE(quality, 0) DESC, detection DESC, id
                      ) AS rn
                      FROM faces
                      WHERE media_id=? AND deleted_at IS NULL
                        AND review_state='unreviewed' AND person_id IS NOT NULL AND manual=0
                    ) ranked WHERE rn = 1
                  )
                """,
                (media_id, media_id),
            )
            conn.execute(
                """
                UPDATE faces SET review_state='confirmed'
                WHERE media_id=? AND deleted_at IS NULL AND review_state='unreviewed'
                  AND track_id IS NOT NULL AND person_id IS NULL AND manual=0
                  AND id NOT IN (
                    SELECT id FROM (
                      SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY track_id
                        ORDER BY COALESCE(quality, 0) DESC, detection DESC, id
                      ) AS rn
                      FROM faces
                      WHERE media_id=? AND deleted_at IS NULL
                        AND review_state='unreviewed' AND track_id IS NOT NULL
                        AND person_id IS NULL AND manual=0
                    ) ranked WHERE rn = 1
                  )
                """,
                (media_id, media_id),
            )
            self.cluster.refresh(conn, affected)
            conn.execute(
                "UPDATE media SET width=?,height=?,duration=?,captured_at=?,thumbnail=?,duplicate_count=?,"
                "content_hash=?,phash=?,dino_offset=?,dino_sha=?,"
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
                    media_id,
                ),
            )
            count = conn.execute("SELECT COUNT(*) FROM faces WHERE media_id=? AND deleted_at IS NULL", (media_id,)).fetchone()[0]
            self._cancel_only()
        staged.commit()
        self._checkpoint()
        media_io.write_thumbnail(thumbnail_dir / f"media-{media_id}.jpg", jpeg)
        for detection in staged.execute("SELECT face_id,jpeg FROM detections ORDER BY id"):
            self._checkpoint()
            media_io.write_thumbnail(thumbnail_dir / f"face-{detection['face_id']}.jpg", detection["jpeg"])
        with self.db.connect() as conn:
            conn.execute("UPDATE media SET status='indexed',error=NULL WHERE id=?", (media_id,))
        return "indexed", count

    def _mark_missing(self, library_id, root):
        if authorized_root(root, self.config.roots) != root:
            raise ValueError("Registered library target changed before missing-file verification")
        with tempfile.TemporaryDirectory(prefix=".missing-", dir=self.data_dir) as directory:
            proposal = sqlite3.connect(Path(directory) / "missing.sqlite")
            try:
                proposal.execute("CREATE TABLE changes(id INTEGER PRIMARY KEY, missing INTEGER)")
                last = 0
                while True:
                    self._checkpoint()
                    rows = self.db.all("SELECT id,path,missing FROM media WHERE library_id=? AND id>? ORDER BY id LIMIT 256",
                                       (library_id, last))
                    if not rows:
                        break
                    for row in rows:
                        self._checkpoint()
                        try:
                            missing = int(not resolve_inside(row["path"], root).is_file())
                        except ValueError:
                            missing = 1
                        if missing != row["missing"]:
                            proposal.execute("INSERT INTO changes VALUES (?,?)", (row["id"], missing))
                    last = rows[-1]["id"]
                proposal.commit()
                self._checkpoint()
                with self.db.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    affected = set()
                    for media_id, missing in proposal.execute("SELECT id,missing FROM changes"):
                        self._cancel_only()
                        affected.update(r[0] for r in conn.execute("SELECT DISTINCT person_id FROM faces WHERE media_id=? "
                                                                 "AND person_id IS NOT NULL", (media_id,)))
                        conn.execute("UPDATE media SET missing=? WHERE id=? AND library_id=?", (missing, media_id, library_id))
                    self.cluster.refresh(conn, affected)
                    self._cancel_only()
            finally:
                proposal.close()