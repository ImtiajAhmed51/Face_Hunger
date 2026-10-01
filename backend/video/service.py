"""Video intelligence: keyframes + SigLIP moments, face tracks per video, person moments, clips."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..vectors.file import VectorFile
from .keyframes import extract_keyframes

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = 1
MOMENT_PAD = 1.0  # seconds added around face sightings in moments/clips


def _display(name, pid) -> str:
    return (name or "").strip() or f"Person {pid}"


class VideoService:
    def __init__(self, services):
        self.s = services
        self._files: dict[str, VectorFile] = {}
        self._matrix: dict[str, tuple[int, np.ndarray, np.ndarray]] = {}
        self._lock = threading.Lock()

    # -- encoders / storage ----------------------------------------------------
    def image_encoder(self):
        """Anything with .spec, .embed_images(bgr list) and .embed_texts(list) (SigLIP 2)."""
        override = getattr(self.s, "keyframe_encoder", None)
        return override if override is not None else self.s.models.text_encoder()

    def _file(self, spec) -> VectorFile:
        with self._lock:
            if spec.key not in self._files:
                path = self.s.config.data_dir / "vectors" / f"keyframes-{spec.slug}.f32"
                path.parent.mkdir(parents=True, exist_ok=True)
                self._files[spec.key] = VectorFile(path, spec.dim)
            return self._files[spec.key]

    def keyframe_path(self, keyframe_id: int) -> Path:
        return self.s.config.data_dir / "keyframes" / f"{keyframe_id}.jpg"

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()

    # -- analysis job -------------------------------------------------------------
    def pending(self, limit: int = 1_000_000) -> list[dict]:
        return self.s.db.all(
            """SELECT m.id, m.path, m.duration FROM media m LEFT JOIN video_analysis a ON a.media_id = m.id
               WHERE m.kind='video' AND m.deleted_at IS NULL AND m.missing=0 AND m.status IN ('indexed','stale')
                 AND (a.media_id IS NULL OR a.version < ? OR (a.model_key IS NULL AND ? IS NOT NULL))
               ORDER BY m.id DESC LIMIT ?""",
            (ANALYSIS_VERSION, self._encoder_key(), limit))

    def _encoder_key(self) -> Optional[str]:
        encoder = self.image_encoder()
        return encoder.spec.key if encoder is not None else None

    def analyze(self, row: dict, *, checkpoint: Callable[[], None] = lambda: None,
                progress: Callable[[float], None] = lambda _t: None) -> dict:
        from ..media_processing import _ffmpeg_bin

        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for video keyframes")
        frames = extract_keyframes(Path(row["path"]), ffmpeg=ffmpeg, duration=row.get("duration"),
                                   checkpoint=checkpoint, progress=progress)
        checkpoint()
        encoder = self.image_encoder()
        vectors = None
        if encoder is not None and frames:
            import cv2

            images = [cv2.imdecode(np.frombuffer(k.jpeg, np.uint8), cv2.IMREAD_COLOR) for k in frames]
            vectors = encoder.embed_images(images)
            checkpoint()
        db = self.s.db
        folder = self.s.config.data_dir / "keyframes"
        folder.mkdir(parents=True, exist_ok=True)
        old = [r["id"] for r in db.all("SELECT id FROM video_keyframes WHERE media_id=?", (row["id"],))]
        stored = self._file(encoder.spec).append_many(vectors) if vectors is not None else []
        with db.connect() as conn:
            conn.execute("DELETE FROM video_keyframes WHERE media_id=?", (row["id"],))
            ids = []
            for i, frame in enumerate(frames):
                kid = conn.execute("INSERT INTO video_keyframes(media_id, t) VALUES (?, ?)", (row["id"], frame.t)).lastrowid
                ids.append(kid)
                if stored:
                    offset, sha = stored[i]
                    conn.execute("INSERT INTO keyframe_vectors(keyframe_id, model_key, offset, sha) VALUES (?,?,?,?)",
                                 (kid, encoder.spec.key, offset, sha))
            conn.execute("INSERT OR REPLACE INTO video_analysis(media_id, version, keyframes, model_key, error, analyzed_at) "
                         "VALUES (?,?,?,?,NULL,CURRENT_TIMESTAMP)",
                         (row["id"], ANALYSIS_VERSION, len(frames), encoder.spec.key if stored else None))
        for kid, frame in zip(ids, frames):
            tmp = folder / f".{kid}.tmp"
            tmp.write_bytes(frame.jpeg)
            os.replace(tmp, folder / f"{kid}.jpg")
        for kid in old:
            (folder / f"{kid}.jpg").unlink(missing_ok=True)
        with self._lock:
            self._matrix.clear()
        return {"keyframes": len(frames), "embedded": len(stored)}

    def record_failure(self, media_id: int, error: str) -> None:
        with self.s.db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO video_analysis(media_id, version, keyframes, model_key, error, analyzed_at) "
                         "VALUES (?,?,0,NULL,?,CURRENT_TIMESTAMP)", (media_id, ANALYSIS_VERSION, error[:300]))

    def run(self, checkpoint, progress, should_yield=lambda: False) -> dict:
        rows = self.pending()
        done = failed = 0
        for index, row in enumerate(rows):
            checkpoint()
            if should_yield():
                return {"yielded": True, "processed": done + failed, "total": len(rows)}
            duration = float(row.get("duration") or 0)

            def report(t, row=row, index=index, duration=duration):
                progress(processed=index, total=len(rows), current=row["path"],
                         video_seconds=round(t, 1), video_duration=round(duration, 1))

            try:
                self.analyze(row, checkpoint=checkpoint, progress=report)
                done += 1
            except Exception as exc:
                if type(exc).__name__ in ("Cancelled", "_Shutdown"):
                    raise
                logger.warning("video analysis failed for %s: %s", row["path"], exc)
                self.record_failure(row["id"], f"{type(exc).__name__}: {exc}")
                failed += 1
            progress(processed=index + 1, total=len(rows), analyzed=done, failed=failed)
        return {"processed": done + failed, "total": len(rows), "analyzed": done, "failed": failed, "current": None}

    # -- moments search -------------------------------------------------------------
    def _keyframe_matrix(self, model_key: str):
        db = self.s.db
        count = db.one("SELECT COUNT(*) c FROM keyframe_vectors WHERE model_key=?", (model_key,))["c"]
        with self._lock:
            cached = self._matrix.get(model_key)
            if cached and cached[0] == count:
                return cached[1], cached[2]
        rows = db.all("SELECT keyframe_id, offset, sha FROM keyframe_vectors WHERE model_key=? ORDER BY keyframe_id",
                      (model_key,))
        encoder = self.image_encoder()
        vf = self._file(encoder.spec)
        ids = np.array([r["keyframe_id"] for r in rows], dtype=np.int64)
        mat = np.stack([vf.read(r["offset"], r["sha"], verify=False) for r in rows]) if rows else np.zeros((0, encoder.spec.dim), np.float32)
        with self._lock:
            self._matrix[model_key] = (count, ids, mat)
        return ids, mat

    def search_moments(self, text: str, *, limit: int = 40, per_video: int = 3, people: Optional[list[int]] = None) -> dict:
        encoder = self.image_encoder()
        if encoder is None:
            return {"items": [], "warning": "Video moment search needs SigLIP 2; run scripts/fetch_models.py"}
        ids, mat = self._keyframe_matrix(encoder.spec.key)
        if not len(ids):
            return {"items": [], "warning": "No video keyframes analyzed yet"}
        q = encoder.embed_texts([text])[0]
        sims = mat @ q
        order = np.argsort(-sims)
        rows = {r["id"]: r for r in self.s.db.all(
            f"SELECT k.id, k.media_id, k.t, m.name, m.duration FROM video_keyframes k JOIN media m ON m.id=k.media_id "
            f"WHERE m.deleted_at IS NULL AND k.id IN ({','.join('?' * min(len(order), 2000))})",
            tuple(int(ids[i]) for i in order[:2000]))}
        allowed = None
        if people:
            allowed = {r["media_id"] for r in self.s.db.all(
                f"SELECT DISTINCT media_id FROM faces WHERE deleted_at IS NULL AND person_id IN ({','.join('?' * len(people))})",
                tuple(people))}
        out, per = [], {}
        for i in order[:2000]:
            row = rows.get(int(ids[i]))
            if row is None or (allowed is not None and row["media_id"] not in allowed):
                continue
            if per.get(row["media_id"], 0) >= per_video:
                continue
            per[row["media_id"]] = per.get(row["media_id"], 0) + 1
            out.append({"keyframe_id": row["id"], "media_id": row["media_id"], "name": row["name"], "t": round(row["t"], 2),
                        "duration": row["duration"], "similarity": round(float(sims[i]), 4)})
            if len(out) >= limit:
                break
        return {"items": out, "warning": None}

    # -- faces in videos -------------------------------------------------------------
    def _segments(self, times: list[float], gap: float) -> list[tuple[float, float]]:
        segments: list[list[float]] = []
        for t in sorted(times):
            if segments and t - segments[-1][1] <= gap:
                segments[-1][1] = t
            else:
                segments.append([t, t])
        return [(max(0.0, a - MOMENT_PAD), b + MOMENT_PAD) for a, b in segments]

    def _gap(self) -> float:
        return max(6.0, 2.5 * float(self.s.db.settings().get("video_interval", 3.0)))

    def detail(self, media_id: int) -> dict:
        db = self.s.db
        faces = db.all(
            """SELECT f.id, f.track_id, f.timestamp, f.quality, f.person_id, p.name FROM faces f
               LEFT JOIN people p ON p.id = f.person_id
               WHERE f.media_id=? AND f.deleted_at IS NULL AND f.review_state != 'rejected' AND f.timestamp IS NOT NULL
               ORDER BY f.timestamp""", (media_id,))
        tracks: dict = {}
        for f in faces:
            key = f["track_id"] if f["track_id"] is not None else -f["id"]
            t = tracks.setdefault(key, {"track_id": f["track_id"], "person_id": f["person_id"],
                                        "display_name": _display(f["name"], f["person_id"]) if f["person_id"] else None,
                                        "times": [], "best_face_id": f["id"], "_q": -1.0})
            t["times"].append(round(f["timestamp"], 2))
            if (f["quality"] or 0) > t["_q"]:
                t["_q"], t["best_face_id"] = f["quality"] or 0, f["id"]
        track_list = []
        for t in tracks.values():
            t.pop("_q")
            t["start"], t["end"] = t["times"][0], t["times"][-1]
            track_list.append(t)
        track_list.sort(key=lambda t: t["start"])
        people: dict = {}
        for t in track_list:
            if t["person_id"] is None:
                continue
            p = people.setdefault(t["person_id"], {"person_id": t["person_id"], "display_name": t["display_name"],
                                                    "times": [], "best_face_id": t["best_face_id"]})
            p["times"].extend(t["times"])
        gap = self._gap()
        for p in people.values():
            p["segments"] = [{"start": round(a, 2), "end": round(b, 2)} for a, b in self._segments(p["times"], gap)]
            del p["times"]
        keyframes = [{"id": k["id"], "t": round(k["t"], 2)} for k in
                     db.all("SELECT id, t FROM video_keyframes WHERE media_id=? ORDER BY t", (media_id,))]
        analysis = db.one("SELECT * FROM video_analysis WHERE media_id=?", (media_id,))
        return {"media_id": media_id, "tracks": track_list, "people": sorted(people.values(), key=lambda p: p["segments"][0]["start"]),
                "keyframes": keyframes, "analyzed": bool(analysis and not analysis["error"]),
                "analysis_error": analysis["error"] if analysis else None}

    def person_moments(self, person_id: int) -> list[dict]:
        db = self.s.db
        rows = db.all(
            """SELECT f.media_id, f.timestamp, f.id, f.quality, m.name, m.duration FROM faces f JOIN media m ON m.id = f.media_id
               WHERE f.person_id=? AND f.deleted_at IS NULL AND f.review_state != 'rejected' AND f.timestamp IS NOT NULL
                 AND m.deleted_at IS NULL AND m.kind='video'
                 AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.media_id=f.media_id AND e.person_id=f.person_id)
               ORDER BY m.id DESC, f.timestamp""", (person_id,))
        by_media: dict = {}
        for r in rows:
            entry = by_media.setdefault(r["media_id"], {"media_id": r["media_id"], "name": r["name"], "duration": r["duration"],
                                                        "times": [], "best_face_id": r["id"], "_q": -1.0})
            entry["times"].append(r["timestamp"])
            if (r["quality"] or 0) > entry["_q"]:
                entry["_q"], entry["best_face_id"] = r["quality"] or 0, r["id"]
        gap = self._gap()
        out = []
        for entry in by_media.values():
            entry.pop("_q")
            entry["segments"] = [{"start": round(a, 2), "end": round(b, 2)} for a, b in self._segments(entry.pop("times"), gap)]
            out.append(entry)
        return out

    # -- clips --------------------------------------------------------------------------
    def clip(self, source: Path, start: float, end: float, target: Path, *, precise: bool = False) -> Path:
        """Cut [start, end] without touching the original. Stream copy by default (fast, lossless,
        starts at the keyframe before ``start``); ``precise`` re-encodes for frame-exact cuts."""
        from ..media_processing import _ffmpeg_bin

        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for clip export")
        if end <= start:
            raise ValueError("end must be after start")
        target.parent.mkdir(parents=True, exist_ok=True)
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"] if precise else ["-c", "copy"]
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{start:.3f}", "-i", str(source),
               "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a?", *codec, "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 and not precise:
            # Some containers/codecs cannot be stream-copied into MP4: fall back to re-encoding.
            return self.clip(source, start, end, target, precise=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg clip failed: {proc.stderr.strip()[-300:]}")
        return target

    def person_clips_zip(self, media_id: int, person_id: int, *, precise: bool = False) -> Path:
        row = self.s.db.one("SELECT * FROM media WHERE id=? AND kind='video'", (media_id,))
        if not row:
            raise KeyError("Video not found")
        moments = next((m for m in self.person_moments(person_id) if m["media_id"] == media_id), None)
        if not moments:
            raise KeyError("This person does not appear in this video")
        person = self.s.db.one("SELECT name FROM people WHERE id=?", (person_id,))
        name = _display(person["name"] if person else None, person_id).replace("/", "_")
        out_dir = self.s.config.data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"clips-{media_id}-{person_id}.zip"
        stem = Path(row["name"]).stem
        with tempfile.TemporaryDirectory(dir=out_dir) as tmp, zipfile.ZipFile(target.with_suffix(".part"), "w") as zf:
            manifest = []
            for i, seg in enumerate(moments["segments"], start=1):
                clip = self.clip(Path(row["path"]), seg["start"], seg["end"], Path(tmp) / f"{i}.mp4", precise=precise)
                arc = f"{stem} - {name} - {i:02d} ({seg['start']:.0f}s-{seg['end']:.0f}s).mp4"
                zf.write(clip, arc)
                manifest.append({"file": arc, **seg})
            zf.writestr("manifest.json", json.dumps({"source": row["name"], "person": name, "clips": manifest}, indent=1))
        os.replace(target.with_suffix(".part"), target)
        return target
