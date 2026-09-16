"""Face quality scoring from detection score, pose (from 5-point landmarks), and blur.

Quality is used to:
- down-weight low-quality faces when forming person centroids / matching
- apply a heavier penalty in review_threshold decisions
- prefer high-quality faces as representatives
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

import numpy as np


def _landmark_pose(kps: np.ndarray) -> Tuple[float, float]:
    """Estimate rough yaw/pitch in [-1, 1] from InsightFace 5-point landmarks.

    Points: 0 left-eye, 1 right-eye, 2 nose, 3 left-mouth, 4 right-mouth.
    Returns (yaw, pitch) where |value| closer to 1 means more extreme pose.
    """
    if kps is None or np.asarray(kps).shape != (5, 2) or not np.isfinite(kps).all():
        return 0.0, 0.0
    pts = np.asarray(kps, dtype=np.float64)
    left_eye, right_eye, nose = pts[0], pts[1], pts[2]
    eye_mid = (left_eye + right_eye) * 0.5
    interocular = float(np.linalg.norm(right_eye - left_eye))
    if interocular < 1e-6:
        return 0.0, 0.0
    # Horizontal offset of nose relative to eye midpoint → yaw proxy
    yaw = float(np.clip((nose[0] - eye_mid[0]) / (interocular * 0.85), -1.5, 1.5))
    # Vertical: nose should sit below eyes; extreme up/down → pitch
    expected_y = eye_mid[1] + interocular * 0.55
    pitch = float(np.clip((nose[1] - expected_y) / (interocular * 0.9), -1.5, 1.5))
    return yaw, pitch


def _blur_score(bgr_crop: np.ndarray) -> float:
    """Laplacian variance based sharpness in [0, 1]. Higher = sharper."""
    if bgr_crop is None or bgr_crop.size == 0:
        return 0.5
    try:
        import cv2
        gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY) if bgr_crop.ndim == 3 else bgr_crop
        if gray.shape[0] < 8 or gray.shape[1] < 8:
            return 0.5
        var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        # Typical face crops: blurry < 50, sharp > 200
        return float(np.clip(math.log1p(var) / math.log1p(400.0), 0.0, 1.0))
    except Exception:
        return 0.5


def face_quality(
    detection: float,
    kps: Optional[np.ndarray] = None,
    bgr: Optional[np.ndarray] = None,
    bbox: Optional[Sequence[float]] = None,
) -> float:
    """Combined quality in [0, 1].

    Components:
    - detection confidence (InsightFace det_score)
    - pose penalty from landmarks (profile / extreme pitch hurt)
    - blur penalty from Laplacian on the face crop
    """
    det = float(np.clip(detection, 0.0, 1.0))
    yaw, pitch = _landmark_pose(kps) if kps is not None else (0.0, 0.0)
    pose_penalty = min(1.0, 0.55 * abs(yaw) + 0.35 * abs(pitch))
    pose = 1.0 - pose_penalty

    blur = 0.5
    if bgr is not None and bbox is not None:
        try:
            x, y, w, h = map(float, bbox)
            h_img, w_img = bgr.shape[:2]
            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(w_img, int(x + w))
            y2 = min(h_img, int(y + h))
            if x2 > x1 and y2 > y1:
                blur = _blur_score(bgr[y1:y2, x1:x2])
        except Exception:
            blur = 0.5

    # Weighted geometric-ish combination; detection is primary gate
    quality = (det ** 0.55) * (0.35 + 0.65 * pose) * (0.4 + 0.6 * blur)
    return float(np.clip(quality, 0.0, 1.0))


def quality_adjusted_similarity(similarity: float, quality: float, review_threshold: float) -> float:
    """Apply quality penalty for borderline review decisions.

    Low-quality faces near the review threshold are pushed further into
    the review queue (or treated more cautiously) rather than auto-accepted.
    """
    sim = float(similarity)
    q = float(np.clip(quality, 0.0, 1.0))
    if q >= 0.75:
        return sim
    # Max penalty ~0.08 for very low quality near the decision boundary
    penalty = (0.75 - q) * 0.12
    if sim >= review_threshold:
        return sim - penalty * 0.5
    return sim - penalty
