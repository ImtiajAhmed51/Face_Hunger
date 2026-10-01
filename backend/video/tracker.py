"""ByteTrack-style face tracking over sampled video frames.

Association per frame:
  1. High-confidence detections (quality >= ``high``) are matched to live
     tracks with the Hungarian algorithm on cost = 1 - (w_iou * IoU + w_emb * cos),
     gated by minimum IoU *or* strong appearance similarity (frames are seconds
     apart, so faces can move a lot between samples; identity carries the match).
  2. Low-confidence detections get a second chance against the tracks left
     unmatched (ByteTrack's key idea: blurry/turned faces keep a track alive
     instead of spawning a new one), with a stricter appearance gate.
  3. Unmatched high-confidence detections start new tracks; tracks unseen for
     ``max_gap`` seconds end.

Each track keeps an EMA of its (unit) ArcFace embeddings. Tracks of the same
person separated by a cut or an exit/re-entry are joined later by the normal
person assignment (every track's best face is matched against people), so the
tracker never has to guess identity across long gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union > 0 else 0.0


@dataclass
class Track:
    id: int
    bbox: list
    emb: np.ndarray
    first_ts: float
    last_ts: float
    hits: int = 1
    best_quality: float = 0.0
    best_index: Optional[int] = None  # caller's detection id for the best face crop
    history: list = field(default_factory=list)  # timestamps seen


class FaceTracker:
    def __init__(self, *, max_gap: float = 6.0, high: float = 0.45, w_iou: float = 0.4, w_emb: float = 0.6,
                 min_iou: float = 0.1, strong_emb: float = 0.55, low_emb: float = 0.6, momentum: float = 0.8):
        self.max_gap, self.high = max_gap, high
        self.w_iou, self.w_emb = w_iou, w_emb
        self.min_iou, self.strong_emb, self.low_emb = min_iou, strong_emb, low_emb
        self.momentum = momentum
        self.tracks: list[Track] = []
        self.finished: list[Track] = []
        self._next = 1

    def _live(self, ts: float) -> list[Track]:
        alive = []
        for track in self.tracks:
            (alive if ts - track.last_ts <= self.max_gap else self.finished).append(track)
        self.tracks = alive
        return alive

    def _match(self, tracks: list[Track], dets: list[dict], *, emb_gate: float) -> tuple[list, list, list]:
        if not tracks or not dets:
            return [], list(range(len(tracks))), list(range(len(dets)))
        from scipy.optimize import linear_sum_assignment

        cost = np.full((len(tracks), len(dets)), 1e6)
        for i, track in enumerate(tracks):
            for j, det in enumerate(dets):
                overlap = iou(track.bbox, det["bbox"])
                sim = float(track.emb @ det["embedding"])
                if sim < emb_gate or (overlap < self.min_iou and sim < self.strong_emb):
                    continue
                cost[i, j] = 1.0 - (self.w_iou * overlap + self.w_emb * sim)
        rows, cols = linear_sum_assignment(cost)
        pairs = [(r, c) for r, c in zip(rows, cols) if cost[r, c] < 1e5]
        matched_t = {r for r, _ in pairs}
        matched_d = {c for _, c in pairs}
        return (pairs, [i for i in range(len(tracks)) if i not in matched_t],
                [j for j in range(len(dets)) if j not in matched_d])

    def _update(self, track: Track, det: dict, ts: float) -> None:
        emb = self.momentum * track.emb + (1 - self.momentum) * np.asarray(det["embedding"], dtype=np.float32)
        track.emb = emb / max(float(np.linalg.norm(emb)), 1e-12)
        track.bbox, track.last_ts, track.hits = det["bbox"], ts, track.hits + 1
        track.history.append(ts)
        quality = float(det.get("quality", det.get("detection", 0.0)))
        if quality > track.best_quality:
            track.best_quality, track.best_index = quality, det.get("index")

    def _new(self, det: dict, ts: float) -> Track:
        emb = np.asarray(det["embedding"], dtype=np.float32)
        track = Track(self._next, det["bbox"], emb / max(float(np.linalg.norm(emb)), 1e-12), ts, ts,
                      best_quality=float(det.get("quality", det.get("detection", 0.0))), best_index=det.get("index"),
                      history=[ts])
        self._next += 1
        self.tracks.append(track)
        return track

    def update(self, ts: float, detections: list[dict]) -> list[int]:
        """Associate one frame's detections; returns the track id for each detection, in order."""
        live = self._live(ts)
        assigned: list[Optional[int]] = [None] * len(detections)
        high = [j for j, d in enumerate(detections) if float(d.get("quality", d.get("detection", 0))) >= self.high]
        low = [j for j in range(len(detections)) if j not in high]

        pairs, rest_t, rest_d = self._match(live, [detections[j] for j in high], emb_gate=0.25)
        for ti, dj in pairs:
            self._update(live[ti], detections[high[dj]], ts)
            assigned[high[dj]] = live[ti].id
        remaining = [live[i] for i in rest_t]
        pairs2, _, rest_low = self._match(remaining, [detections[j] for j in low], emb_gate=self.low_emb)
        for ti, dj in pairs2:
            self._update(remaining[ti], detections[low[dj]], ts)
            assigned[low[dj]] = remaining[ti].id
        for dj in rest_d:
            assigned[high[dj]] = self._new(detections[high[dj]], ts).id
        for dj in rest_low:  # low-confidence faces still get a track (they are still faces)
            assigned[low[dj]] = self._new(detections[low[dj]], ts).id
        return [int(a) for a in assigned]

    def all_tracks(self) -> list[Track]:
        return sorted(self.finished + self.tracks, key=lambda t: t.id)
