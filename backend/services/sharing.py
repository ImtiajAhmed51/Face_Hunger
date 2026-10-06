"""Share safely: anonymized, metadata-free copies of photos, written as new files.

Who gets anonymized
  mode "all_except_kept" (default): every face is anonymized unless its person is in
  ``keep_people``. Faces without a person count as unknown and are anonymized.
  mode "only_selected": only faces of ``anonymize_people`` (and unknown faces when
  ``anonymize_unknown`` is true).

How faces are found: the stored boxes (with landmarks) plus, when the face engine is
available, a fresh detection on the image being exported, so faces that were skipped or
deleted at index time are still covered. After anonymizing, the output is checked again: any
face that still matches a targeted identity is covered with a solid mask. Originals are only
read. Videos are not anonymized (faces between sampled frames cannot be guaranteed); they are
skipped unless ``include_videos`` is set, in which case they are copied with metadata removed
only if they contain no targeted person.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .. import anonymize as anon
from .. import edits as edit_model
from ..engine import iou

logger = logging.getLogger(__name__)

DEFAULTS = {"mode": "all_except_kept", "keep_people": [], "anonymize_people": [], "anonymize_unknown": True,
            "method": "blur", "strength": 0.7, "strip_metadata": True, "strip_gps": True, "apply_edits": True,
            "include_videos": False, "verify": True, "destination": {"type": "zip"}}


class ShareError(ValueError):
    pass


def normalize_options(options: dict) -> dict:
    out = {**DEFAULTS, **{k: v for k, v in (options or {}).items() if k in DEFAULTS or k == "media_ids"}}
    if out["mode"] not in ("all_except_kept", "only_selected"):
        raise ShareError("mode must be all_except_kept or only_selected")
    if out["method"] not in anon.METHODS:
        raise ShareError(f"method must be one of {', '.join(anon.METHODS)}")
    out["strength"] = min(1.0, max(0.0, float(out["strength"])))
    out["keep_people"] = [int(p) for p in out["keep_people"]]
    out["anonymize_people"] = [int(p) for p in out["anonymize_people"]]
    dest = out["destination"] or {"type": "zip"}
    if dest.get("type") not in ("zip", "folder"):
        raise ShareError("destination.type must be zip or folder")
    if dest["type"] == "folder" and not str(dest.get("path") or "").strip():
        raise ShareError("destination.path is required for a folder export")
    out["destination"] = dest
    return out


class SharingService:
    def __init__(self, services):
        self.s = services

    @property
    def db(self):
        return self.s.db

    # -- who is in the selection --------------------------------------------------
    def people_in(self, media_ids: list[int]) -> dict:
        if not media_ids:
            return {"people": [], "unknown_faces": 0, "photos": 0, "videos": 0}
        ph = ",".join("?" * len(media_ids))
        people = self.db.all(
            f"""SELECT p.id, p.name, COUNT(DISTINCT f.media_id) AS media_count, MIN(f.id) AS face_id FROM faces f
                JOIN people p ON p.id = f.person_id
                WHERE f.media_id IN ({ph}) AND f.deleted_at IS NULL GROUP BY p.id ORDER BY media_count DESC""",
            tuple(media_ids))
        unknown = self.db.one(f"SELECT COUNT(*) c FROM faces WHERE media_id IN ({ph}) AND deleted_at IS NULL AND person_id IS NULL",
                              tuple(media_ids))["c"]
        kinds = {r["kind"]: r["c"] for r in self.db.all(
            f"SELECT kind, COUNT(*) c FROM media WHERE id IN ({ph}) GROUP BY kind", tuple(media_ids))}
        return {"people": [{"id": p["id"], "display_name": (p["name"] or "").strip() or f"Person {p['id']}",
                            "media_count": p["media_count"], "face_id": p["face_id"]} for p in people],
                "unknown_faces": unknown, "photos": kinds.get("photo", 0), "videos": kinds.get("video", 0)}

    # -- targeting ---------------------------------------------------------------------
    def _targeted(self, person_id: Optional[int], o: dict) -> bool:
        if o["mode"] == "all_except_kept":
            return person_id is None or person_id not in o["keep_people"]
        if person_id is None:
            return bool(o["anonymize_unknown"])
        return person_id in o["anonymize_people"]

    def _engine_ready(self) -> bool:
        try:
            self.s.engine.load()
            return True
        except Exception as exc:
            logger.info("face engine unavailable for share verification: %s", exc)
            return False

    def _stored_faces(self, media_id: int) -> list[dict]:
        rows = self.db.all("SELECT id, person_id, bbox, landmarks, embedding_offset, embedding_sha FROM faces "
                           "WHERE media_id=? AND deleted_at IS NULL", (media_id,))
        for r in rows:
            r["bbox"] = json.loads(r["bbox"])
            r["landmarks"] = json.loads(r["landmarks"]) if r["landmarks"] else None
            try:
                r["embedding"] = self.s.store.read(r["embedding_offset"], r["embedding_sha"])
            except Exception:
                r["embedding"] = None
        return rows

    def _kept_centroids(self, o: dict) -> list[np.ndarray]:
        if o["mode"] != "all_except_kept":
            return []
        return [c for c in (self.s.library._centroid(p) for p in o["keep_people"]) if c is not None]

    def render(self, media_id: int, options: dict, *, max_side: Optional[int] = None) -> tuple[bytes, dict]:
        """Anonymized JPEG bytes for one photo plus a small report."""
        from PIL import Image

        from .. import imaging

        o = normalize_options(options)
        row = self.db.one("SELECT * FROM media WHERE id=?", (media_id,))
        if row is None or row["kind"] != "photo":
            raise KeyError("Photo not found")
        path = Path(row["path"])
        image = imaging.open_image(path, max_side=max_side)
        bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
        scale = bgr.shape[1] / float(row["width"] or bgr.shape[1])
        threshold = float(self.db.settings().get("matching_threshold", 0.48))

        stored = self._stored_faces(media_id)
        regions = [{"bbox": [v * scale for v in f["bbox"]],
                    "landmarks": None if f["landmarks"] is None else [[x * scale, y * scale] for x, y in f["landmarks"]]}
                   for f in stored if self._targeted(f["person_id"], o)]
        target_vectors = [f["embedding"] for f in stored if self._targeted(f["person_id"], o) and f["embedding"] is not None]
        engine = o["verify"] and self._engine_ready()
        if engine:
            kept = self._kept_centroids(o)
            for det in self.s.engine.detect(bgr):
                match = max(stored, key=lambda f: iou([v * scale for v in f["bbox"]], det["bbox"]), default=None)
                if match is not None and iou([v * scale for v in match["bbox"]], det["bbox"]) >= 0.3:
                    targeted = self._targeted(match["person_id"], o)
                else:  # a face the index does not know: kept only if it clearly is a kept person
                    is_kept = any(float(det["embedding"] @ c) >= threshold for c in kept)
                    targeted = (not is_kept) if o["mode"] == "all_except_kept" else bool(o["anonymize_unknown"])
                if targeted:
                    regions.append({"bbox": det["bbox"], "landmarks": det.get("landmarks")})
                    target_vectors.append(det["embedding"])
        out = anon.anonymize(bgr, regions, method=o["method"], strength=o["strength"]) if regions else bgr
        escalated = 0
        if engine and target_vectors:
            for _ in range(2):  # anything still recognisable gets a solid mask
                leaks = [d for d in self.s.engine.detect(out)
                         if max(float(d["embedding"] @ v) for v in target_vectors) >= threshold]
                if not leaks:
                    break
                escalated += len(leaks)
                out = anon.anonymize(out, [{"bbox": d["bbox"], "landmarks": d.get("landmarks")} for d in leaks], method="mask")
        result = Image.fromarray(np.ascontiguousarray(out[:, :, ::-1]))
        if o["apply_edits"]:
            result = edit_model.apply(result, self.s.edits.get(media_id))
        buf = io.BytesIO()
        save = {"format": "JPEG", "quality": 92}
        if not o["strip_metadata"]:
            try:
                with imaging._pil().open(path) as src:
                    exif = src.getexif()
                exif[0x0112] = 1  # pixels are already upright
                if o["strip_gps"]:
                    exif.pop(0x8825, None)
                save["exif"] = exif.tobytes()
            except Exception:
                pass
        result.save(buf, **save)
        return buf.getvalue(), {"faces_anonymized": len(regions), "escalated": escalated, "verified": bool(engine)}

    # -- destinations ----------------------------------------------------------------------
    def _folder(self, raw: str) -> Path:
        folder = Path(raw).expanduser()
        if not folder.is_absolute():
            raise ShareError("The export folder must be an absolute path")
        folder = folder.resolve()
        forbidden = [Path(self.s.config.data_dir).resolve()] + [Path(r["path"]).resolve() for r in self.db.all("SELECT path FROM libraries")]
        for root in forbidden:
            if folder == root or folder.is_relative_to(root):
                raise ShareError("Choose a folder outside your libraries and the app's data folder")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _unique(folder: Path, name: str) -> Path:
        target = folder / name
        counter = 1
        while target.exists():  # never overwrite anything that is already there
            target = folder / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        return target

    def _copy_video(self, source: Path, target: Path) -> None:
        from ..media_processing import _ffmpeg_bin

        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            raise ShareError("ffmpeg is required to share videos")
        proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                               "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-map_metadata", "-1",
                               "-map_chapters", "-1", "-movflags", "+faststart", str(target)],
                              capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise ShareError(f"ffmpeg failed: {proc.stderr.strip()[-200:]}")

    def run(self, job_id: int, options: dict, state: dict, *, checkpoint: Callable[[], None],
            progress: Callable[..., None]) -> dict:
        """Export every item; ``state`` (the job's saved progress) makes it resumable."""
        o = normalize_options(options)
        ids = [int(m) for m in options.get("media_ids", [])]
        if not ids:
            raise ShareError("media_ids required")
        is_zip = o["destination"]["type"] == "zip"
        folder = (self.s.config.data_dir / "exports" / f"share-{job_id}") if is_zip else self._folder(o["destination"]["path"])
        folder.mkdir(parents=True, exist_ok=True)
        done: dict = dict(state.get("done") or {})
        skipped: dict = dict(state.get("skipped") or {})
        report = {"faces_anonymized": int(state.get("faces_anonymized") or 0), "escalated": int(state.get("escalated") or 0)}
        for index, media_id in enumerate(ids):
            checkpoint()
            key = str(media_id)
            if key in done and Path(done[key]).exists() or key in skipped:
                continue
            row = self.db.one("SELECT * FROM media WHERE id=? AND deleted_at IS NULL", (media_id,))
            if row is None or not Path(row["path"]).is_file():
                skipped[key] = "missing"
            elif row["kind"] == "video":
                people = {r["person_id"] for r in self.db.all("SELECT DISTINCT person_id FROM faces WHERE media_id=? AND deleted_at IS NULL", (media_id,))}
                if not o["include_videos"]:
                    skipped[key] = "videos are not anonymized"
                elif any(self._targeted(p, o) for p in people):
                    skipped[key] = "video shows someone to anonymize"
                else:
                    target = self._unique(folder, Path(row["name"]).stem + ".mp4")
                    tmp = target.with_name(f".{target.name}.part.mp4")
                    self._copy_video(Path(row["path"]), tmp)
                    os.replace(tmp, target)
                    done[key] = str(target)
            else:
                try:
                    data, item = self.render(media_id, o)
                    target = self._unique(folder, Path(row["name"]).stem + ".jpg")
                    tmp = target.with_name(f".{target.name}.part")
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
                    done[key] = str(target)
                    report["faces_anonymized"] += item["faces_anonymized"]
                    report["escalated"] += item["escalated"]
                except Exception as exc:
                    if type(exc).__name__ in ("Cancelled", "_Shutdown"):
                        raise
                    logger.warning("share export failed for media %s: %s", media_id, exc)
                    skipped[key] = f"{type(exc).__name__}: {exc}"[:160]
            progress(force=True, processed=index + 1, total=len(ids), done=done, skipped=skipped, **report)
        result = {"processed": len(ids), "total": len(ids), "exported": len(done), "skipped": skipped, "done": done, **report,
                  "destination": str(folder)}
        if is_zip:
            archive = self.s.config.data_dir / "exports" / f"share-{job_id}.zip"
            part = archive.with_suffix(".zip.part")
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED) as zf:
                for target in done.values():
                    checkpoint()
                    zf.write(target, Path(target).name)
            os.replace(part, archive)
            for target in done.values():
                Path(target).unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
            result.update(destination=str(archive), archive=archive.name)
        return result
