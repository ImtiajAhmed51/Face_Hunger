"""Computes and stores per-media quality signals and best-shot scores (see backend/scoring.py)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .. import scoring
from ..engine import iou

logger = logging.getLogger(__name__)

PENDING_SQL = f"""
    SELECT m.id, m.path, m.kind, m.width, m.height, m.status FROM media m
    LEFT JOIN quality_signals q ON q.media_id = m.id
    WHERE m.deleted_at IS NULL AND m.missing = 0 AND m.status IN ('indexed','stale')
      AND (q.media_id IS NULL OR q.version < {scoring.SIGNALS_VERSION} OR q.computed_at < COALESCE(m.indexed_at, ''))
    ORDER BY m.id DESC LIMIT ?
"""


class QualityService:
    def __init__(self, services):
        self.s = services
        self._head = None
        self._head_mtime = None

    # -- aesthetic head ------------------------------------------------------
    def head_path(self) -> Path:
        return Path(self.s.config.model_dir) / "siglip2-base-patch16-224" / "aesthetic_head.json"

    def head(self) -> Optional[scoring.AestheticHead]:
        path = self.head_path()
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            self._head = None
            return None
        if mtime != self._head_mtime:
            self._head, self._head_mtime = scoring.AestheticHead.load(path), mtime
        return self._head

    def _aesthetic(self, media_id: int) -> Optional[float]:
        head = self.head()
        if head is None:
            return None
        try:
            vector = self.s.vectors.get(head.model_key).vector(media_id)
        except KeyError:
            return None
        return head(vector) if vector is not None else None

    # -- signals --------------------------------------------------------------
    def _faces(self, media_id: int) -> list[dict]:
        rows = self.s.db.all(
            "SELECT id, bbox, quality, landmarks, timestamp FROM faces WHERE media_id=? AND deleted_at IS NULL "
            "AND review_state != 'rejected'", (media_id,))
        for row in rows:
            row["bbox"] = json.loads(row["bbox"])
            row["landmarks"] = json.loads(row["landmarks"]) if row["landmarks"] else None
        return rows

    def _backfill_landmarks(self, bgr: np.ndarray, faces: list[dict], scale: float) -> None:
        """Faces indexed before landmarks were stored: re-detect once on the analysis image."""
        missing = [f for f in faces if f["landmarks"] is None]
        if not missing:
            return
        try:
            detections = self.s.engine.detect(bgr)
        except Exception as exc:
            logger.debug("landmark re-detection skipped: %s", exc)
            return
        updates = []
        for face in missing:
            box = [v * scale for v in face["bbox"]]
            best = max(detections, key=lambda d: iou(box, d["bbox"]), default=None)
            if best is None or iou(box, best["bbox"]) < 0.4 or not best.get("landmarks"):
                continue
            face["landmarks"] = [[x / scale, y / scale] for x, y in best["landmarks"]]
            updates.append((json.dumps([[round(x, 2), round(y, 2)] for x, y in face["landmarks"]]), face["id"]))
        if updates:
            with self.s.db.connect() as conn:
                conn.executemany("UPDATE faces SET landmarks=? WHERE id=?", updates)

    def compute(self, row: dict) -> dict:
        from .. import imaging
        from ..media_processing import load_image

        path = Path(row["path"])
        if row["kind"] == "photo":
            bgr = load_image(path, max_side=scoring.ANALYSIS_SIDE)
        else:  # videos: the representative frame the thumbnail was made from
            thumb = self.s.config.data_dir / "thumbnails" / f"media-{row['id']}.jpg"
            bgr = imaging.load_bgr(thumb)
        signals = scoring.image_signals(bgr)
        faces = self._faces(row["id"])
        if row["kind"] == "photo" and faces:
            # Landmarks/bboxes are in the indexed image's pixels; map them to the analysis image.
            scale = bgr.shape[1] / float(row["width"] or bgr.shape[1])
            self._backfill_landmarks(bgr, faces, scale)
            scaled = [{**f, "landmarks": None if f["landmarks"] is None else np.asarray(f["landmarks"]) * scale}
                      for f in faces]
            signals.update(scoring.face_signals(bgr, scaled))
        elif faces:  # video faces come from many frames: keep their detector quality only
            signals.update(face_quality=float(np.mean([f["quality"] or 0.5 for f in faces])), faces=len(faces),
                           eyes_open=None, smile=None)
        else:
            signals.update(face_quality=None, eyes_open=None, smile=None, faces=0)
        signals["aesthetic"] = self._aesthetic(row["id"])
        return signals

    def _store(self, media_id: int, signals: dict, error: Optional[str] = None) -> None:
        cols = [*scoring.SIGNALS, "faces"]
        with self.s.db.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO quality_signals(media_id, version, {', '.join(cols)}, error, computed_at) "
                f"VALUES (?, ?, {', '.join('?' * len(cols))}, ?, CURRENT_TIMESTAMP)",
                (media_id, scoring.SIGNALS_VERSION, *(signals.get(c) for c in cols), error))
            conn.execute("DELETE FROM quality_scores WHERE media_id=?", (media_id,))

    def rescore(self, *, formula_version: int = scoring.FORMULA_VERSION, weights: dict = scoring.WEIGHTS,
                limit: int = 5000) -> int:
        """Composite scores for signals that have none under this formula: arithmetic only."""
        rows = self.s.db.all(
            "SELECT q.* FROM quality_signals q LEFT JOIN quality_scores s ON s.media_id=q.media_id AND s.formula_version=? "
            "WHERE s.media_id IS NULL AND q.error IS NULL LIMIT ?", (formula_version, limit))
        values = []
        for row in rows:
            score, breakdown = scoring.composite(row, weights)
            values.append((row["media_id"], formula_version, score, json.dumps(breakdown)))
        if values:
            with self.s.db.connect() as conn:
                conn.executemany("INSERT OR REPLACE INTO quality_scores VALUES (?,?,?,?)", values)
        return len(values)

    def refresh_aesthetic(self, limit: int = 2000) -> int:
        head = self.head()
        if head is None:
            return 0
        rows = self.s.db.all(
            "SELECT q.media_id FROM quality_signals q JOIN media_vectors v ON v.media_id=q.media_id AND v.model_key=? "
            "WHERE q.aesthetic IS NULL LIMIT ?", (head.model_key, limit))
        updates = [(a, r["media_id"]) for r in rows if (a := self._aesthetic(r["media_id"])) is not None]
        if updates:
            with self.s.db.connect() as conn:
                conn.executemany("UPDATE quality_signals SET aesthetic=? WHERE media_id=?", updates)
                conn.executemany("DELETE FROM quality_scores WHERE media_id=?", [(m,) for _, m in updates])
        return len(updates)

    def pending_count(self) -> int:
        return len(self.s.db.all(PENDING_SQL, (1_000_000_000,)))

    def run(self, checkpoint: Callable[[], None], progress: Callable[..., None],
            should_yield: Callable[[], bool] = lambda: False, batch: int = 32) -> dict:
        done = failed = 0
        total = self.pending_count()
        while True:
            checkpoint()
            if should_yield():
                return {"yielded": True, "processed": done, "total": total}
            rows = self.s.db.all(PENDING_SQL, (batch,))
            if not rows:
                break
            for row in rows:
                checkpoint()
                try:
                    self._store(row["id"], self.compute(row))
                    done += 1
                except Exception as exc:
                    self._store(row["id"], {}, error=f"{type(exc).__name__}: {exc}"[:300])
                    failed += 1
                progress(processed=done + failed, total=total, scored=done, failed=failed)
            self.rescore()
        refreshed = self.refresh_aesthetic()
        while self.rescore():
            checkpoint()
        return {"processed": done + failed, "total": total, "scored": done, "failed": failed, "aesthetic_filled": refreshed}

    # -- read side ------------------------------------------------------------
    def for_media(self, media_id: int) -> dict:
        signals = self.s.db.one("SELECT * FROM quality_signals WHERE media_id=?", (media_id,))
        score = self.s.db.one("SELECT * FROM quality_scores WHERE media_id=? AND formula_version=?",
                              (media_id, scoring.FORMULA_VERSION))
        return {
            "formula_version": scoring.FORMULA_VERSION,
            "weights": scoring.WEIGHTS,
            "signals": {k: signals.get(k) for k in (*scoring.SIGNALS, "faces")} if signals else None,
            "error": signals.get("error") if signals else None,
            "score": score["score"] if score else None,
            "breakdown": json.loads(score["breakdown"]) if score else None,
            "aesthetic_model": self.head().model_key if self.head() else None,
        }
