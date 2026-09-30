"""Per-media quality signals and the versioned best-shot formula.

Raw signals (all in [0, 1], higher is better) are stored in ``quality_signals``
and the composite in ``quality_scores`` keyed by formula version, so changing
the weights only re-runs cheap arithmetic, never image decoding.

Signals
  sharpness   log-scaled Laplacian variance (noise contribution removed) on a <= 1024 px copy
  exposure    penalises clipped shadows/highlights and a mean far from mid-grey
  noise       worse of (1 - Immerkaer noise sigma / 8) and JPEG blockiness
  face_quality mean detector/pose/blur quality of the faces (existing signal)
  eyes_open   eye-patch openness from the 5-point landmarks (dark iris blob height/width)
  smile       mouth-corner width relative to the eye distance
  aesthetic   linear head on the stored SigLIP 2 image embedding (zero-shot, from prompts)

Missing signals (no faces, no SigLIP vector yet) are left out and the
remaining weights are renormalised, so a landscape is not penalised for
having no eyes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

SIGNALS_VERSION = 1
FORMULA_VERSION = 1
# Documented best-shot weights (formula v1). Technical quality dominates, people
# signals decide between technically similar frames, aesthetic breaks remaining ties.
WEIGHTS = {
    "sharpness": 0.28,
    "exposure": 0.14,
    "noise": 0.10,
    "face_quality": 0.14,
    "eyes_open": 0.14,
    "smile": 0.06,
    "aesthetic": 0.14,
}
SIGNALS = tuple(WEIGHTS)
ANALYSIS_SIDE = 1024


def _gray(bgr: np.ndarray) -> np.ndarray:
    """Grey copy, downscaled (never upscaled: interpolation would smear noise into blobs)."""
    import cv2

    h, w = bgr.shape[:2]
    scale = ANALYSIS_SIDE / max(h, w)
    if scale < 0.98:
        bgr = cv2.resize(bgr, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr


def sharpness(gray: np.ndarray, sigma: Optional[float] = None) -> float:
    """Laplacian variance with the noise contribution removed.

    White noise of std sigma adds 20*sigma^2 to the variance of the 4-neighbour
    Laplacian (sum of squared kernel weights), so noisy frames no longer look
    "sharper" than clean ones.
    """
    import cv2

    var = float(cv2.Laplacian(gray.astype(np.float64), cv2.CV_64F).var())
    if sigma is None:
        sigma = noise_sigma(gray)
    var = max(0.0, var - 20.0 * sigma * sigma)
    # ~20 = very soft, ~1500+ = crisp at 1024 px
    return float(np.clip(math.log1p(var) / math.log1p(1500.0), 0.0, 1.0))


def blockiness(bgr_or_gray: np.ndarray) -> float:
    """JPEG 8x8 block artefacts: gradient energy on block borders / elsewhere (1.0 = none).

    Measured at native resolution (top-left 1024 px) so the block grid stays aligned.
    """
    import cv2

    g = bgr_or_gray if bgr_or_gray.ndim == 2 else cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2GRAY)
    g = g[:1024, :1024].astype(np.float32)
    if min(g.shape) < 32:
        return 1.0
    dx, dy = np.abs(np.diff(g, axis=1)), np.abs(np.diff(g, axis=0))
    on_x = np.arange(dx.shape[1]) % 8 == 7
    on_y = np.arange(dy.shape[0]) % 8 == 7
    bx = dx[:, on_x].mean() / (dx[:, ~on_x].mean() + 1e-6)
    by = dy[on_y, :].mean() / (dy[~on_y, :].mean() + 1e-6)
    return float((bx + by) / 2)


def exposure(gray: np.ndarray) -> float:
    """Clipping dominates; the mean only counts outside a generous dead zone around mid-grey,
    so deliberate high-key and low-key photos are not punished."""
    total = gray.size
    shadows = np.count_nonzero(gray <= 4) / total
    highlights = np.count_nonzero(gray >= 251) / total
    mean = float(gray.mean()) / 255.0
    deviation = max(0.0, abs(mean - 0.46) - 0.18)
    score = 1.0 - 2.0 * deviation - 2.5 * max(0.0, shadows - 0.02) - 3.0 * max(0.0, highlights - 0.01)
    return float(np.clip(score, 0.0, 1.0))


def noise_sigma(gray: np.ndarray) -> float:
    """Immerkaer (1996) fast noise estimation, in grey levels."""
    import cv2

    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float64)
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    response = cv2.filter2D(gray.astype(np.float64), -1, kernel, borderType=cv2.BORDER_REFLECT)[1:-1, 1:-1]
    return float(math.sqrt(math.pi / 2) * np.abs(response).sum() / (6 * (w - 2) * (h - 2)))


NOISE_SCALE = 8.0  # grey levels of Immerkaer sigma at which the noise signal reaches 0 (calibrated, see DECISIONS)


def noise(gray: np.ndarray) -> float:
    return float(np.clip(1.0 - noise_sigma(gray) / NOISE_SCALE, 0.0, 1.0))


def eye_openness(patch_gray: np.ndarray) -> Optional[float]:
    """Open eyes show a dark, roundish iris/pupil; closed eyes a thin dark lash line.

    Returns height/width of the dark blob through the patch centre, mapped to
    [0, 1] (about 0.15 closed, 0.45+ open), or None for unusable patches.
    """
    import cv2

    if patch_gray is None or patch_gray.size < 64 or min(patch_gray.shape) < 8:
        return None
    patch = cv2.GaussianBlur(patch_gray, (3, 3), 0).astype(np.float32)
    lo, hi = np.percentile(patch, 5), np.percentile(patch, 95)
    if hi - lo < 12:  # flat patch (occluded, over-exposed)
        return None
    mask = (patch < lo + 0.35 * (hi - lo)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return 0.0
    h, w = patch.shape
    cy, cx = h / 2, w / 2
    best, best_dist = None, 1e9
    for index in range(1, count):
        x, y, bw, bh, area = stats[index]
        if area < max(4, 0.004 * patch.size):
            continue
        dist = math.hypot(x + bw / 2 - cx, y + bh / 2 - cy)
        if dist < best_dist:
            best, best_dist = (bw, bh), dist
    if best is None:
        return 0.0
    ratio = best[1] / max(1, best[0])
    return float(np.clip((ratio - 0.12) / 0.4, 0.0, 1.0))


def eyes_open_from_kps(gray_full: np.ndarray, kps: np.ndarray) -> Optional[float]:
    """Openness of the less-open eye (a blink ruins the shot); ``kps`` in the same pixel space."""
    kps = np.asarray(kps, dtype=np.float64).reshape(5, 2)
    inter = float(np.linalg.norm(kps[1] - kps[0]))
    if inter < 8:
        return None
    half_w, half_h = inter * 0.22, inter * 0.14
    values = []
    for x, y in kps[:2]:
        x0, x1 = int(max(0, x - half_w)), int(min(gray_full.shape[1], x + half_w))
        y0, y1 = int(max(0, y - half_h)), int(min(gray_full.shape[0], y + half_h))
        value = eye_openness(gray_full[y0:y1, x0:x1])
        if value is not None:
            values.append(value)
    return float(min(values)) if values else None


def smile_from_kps(kps: np.ndarray) -> Optional[float]:
    """Mouth width / eye distance: ~0.80 neutral, ~1.0+ broad smile (frontal faces)."""
    kps = np.asarray(kps, dtype=np.float64).reshape(5, 2)
    inter = float(np.linalg.norm(kps[1] - kps[0]))
    if inter < 8:
        return None
    ratio = float(np.linalg.norm(kps[4] - kps[3])) / inter
    return float(np.clip((ratio - 0.80) / 0.25, 0.0, 1.0))


class AestheticHead:
    """score = sigmoid(scale * (w . v + bias)) on a unit SigLIP 2 image vector."""

    def __init__(self, weights: np.ndarray, bias: float, scale: float, model_key: str, version: str):
        self.w = np.asarray(weights, dtype=np.float32)
        self.bias, self.scale, self.model_key, self.version = float(bias), float(scale), model_key, version

    @classmethod
    def load(cls, path: Path) -> Optional["AestheticHead"]:
        try:
            data = json.loads(Path(path).read_text())
            return cls(np.asarray(data["weights"], dtype=np.float32), data["bias"], data["scale"],
                       data["model_key"], data.get("version", "1"))
        except (OSError, ValueError, KeyError):
            return None

    def __call__(self, vector: np.ndarray) -> float:
        z = self.scale * (float(np.dot(self.w, np.asarray(vector, dtype=np.float32))) + self.bias)
        return float(1.0 / (1.0 + math.exp(-z)))

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps({"weights": [round(float(x), 7) for x in self.w], "bias": self.bias,
                                          "scale": self.scale, "model_key": self.model_key, "version": self.version}))


def image_signals(bgr: np.ndarray) -> dict:
    gray = _gray(bgr)
    sigma = noise_sigma(gray)
    # "noise" covers sensor noise and compression artefacts (whichever is worse).
    compression = float(np.clip(1.0 - (blockiness(bgr) - 1.05) * 2.5, 0.0, 1.0))
    return {"sharpness": sharpness(gray, sigma), "exposure": exposure(gray),
            "noise": min(float(np.clip(1.0 - sigma / NOISE_SCALE, 0.0, 1.0)), compression)}


def face_signals(bgr: np.ndarray, faces: list[dict]) -> dict:
    """faces: dicts with 'quality' and optional 'landmarks' (5x2, in bgr pixel space)."""
    if not faces:
        return {"face_quality": None, "eyes_open": None, "smile": None, "faces": 0}
    import cv2

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    eyes, smiles = [], []
    for face in faces:
        kps = face.get("landmarks")
        if kps is None:
            continue
        e = eyes_open_from_kps(gray, kps)
        s = smile_from_kps(kps)
        if e is not None:
            eyes.append(e)
        if s is not None:
            smiles.append(s)
    return {
        "face_quality": float(np.mean([f.get("quality") or 0.5 for f in faces])),
        # The worst eyes in the frame matter most for a group shot.
        "eyes_open": float(min(eyes)) if eyes else None,
        "smile": float(np.mean(smiles)) if smiles else None,
        "faces": len(faces),
    }


def composite(signals: dict, weights: dict = WEIGHTS) -> tuple[float, dict]:
    """Weighted mean over the signals that exist; returns (score, per-signal contribution)."""
    present = {k: float(signals[k]) for k in weights if signals.get(k) is not None}
    total = sum(weights[k] for k in present)
    if not total:
        return 0.0, {}
    contributions = {k: weights[k] * v / total for k, v in present.items()}
    return float(sum(contributions.values())), {k: round(v, 5) for k, v in contributions.items()}
