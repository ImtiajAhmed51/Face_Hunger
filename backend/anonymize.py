"""Face anonymization on pixel arrays: blur, pixelate or solid mask inside an elliptical region.

Everything here works on a copy; callers write the result as a NEW file. The region is an
ellipse around the face box, enlarged to cover hairline, chin and ears, and tilted along the
eye line when the 5-point landmarks are known. ``strength`` (0-1) scales the effect, but never
below a floor that keeps the face unrecognisable: sharing safely must not depend on the user
finding the right slider position.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

METHODS = ("blur", "pixelate", "mask")
PAD = 0.35           # the ellipse extends this fraction of the box beyond it on every side
MIN_STRENGTH = 0.35  # effective floor for blur/pixelate


def _ellipse(bbox: Sequence[float], landmarks: Optional[Sequence[Sequence[float]]], scale: float = 1.0):
    x, y, w, h = (float(v) * scale for v in bbox)
    cx, cy = x + w / 2, y + h / 2
    angle = 0.0
    if landmarks is not None and len(landmarks) >= 2:
        (lx, ly), (rx, ry) = landmarks[0], landmarks[1]
        angle = math.degrees(math.atan2((ry - ly) * scale, (rx - lx) * scale))
        if abs(angle) > 60:  # implausible for an upright photo: ignore
            angle = 0.0
    return (cx, cy), (w / 2 * (1 + 2 * PAD), h / 2 * (1 + 2 * PAD)), angle


def region_mask(shape: tuple[int, int], bbox, landmarks=None, scale: float = 1.0) -> np.ndarray:
    """uint8 mask (255 inside the padded ellipse) for an image of ``shape`` (h, w)."""
    import cv2

    mask = np.zeros(shape[:2], np.uint8)
    (cx, cy), (ax, ay), angle = _ellipse(bbox, landmarks, scale)
    cv2.ellipse(mask, (int(round(cx)), int(round(cy))), (max(1, int(round(ax))), max(1, int(round(ay)))), angle, 0, 360, 255, -1)
    return mask


def anonymize(bgr: np.ndarray, faces: list[dict], *, method: str = "blur", strength: float = 0.7,
              scale: float = 1.0) -> np.ndarray:
    """Return a copy of ``bgr`` with every face in ``faces`` ({"bbox", "landmarks"?}) anonymized.

    ``scale`` converts face coordinates (stored in indexed-image pixels) to this image's pixels.
    """
    import cv2

    if method not in METHODS:
        raise ValueError(f"method must be one of {', '.join(METHODS)}")
    out = bgr.copy()
    height, width = out.shape[:2]
    strength = min(1.0, max(MIN_STRENGTH, float(strength)))
    for face in faces:
        mask = region_mask((height, width), face["bbox"], face.get("landmarks"), scale)
        ys, xs = np.nonzero(mask)
        if not len(xs):
            continue
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
        patch = out[y0:y1, x0:x1]
        inside = mask[y0:y1, x0:x1] > 0
        size = max(x1 - x0, y1 - y0)
        if method == "mask":
            treated = np.zeros_like(patch)
        elif method == "pixelate":
            blocks = max(3, int(round(12 - 8 * strength)))  # 4 (strong) .. 9 (weak) cells across the face
            small = cv2.resize(patch, (blocks, max(1, round(blocks * patch.shape[0] / patch.shape[1]))),
                               interpolation=cv2.INTER_AREA)
            treated = cv2.resize(small, (patch.shape[1], patch.shape[0]), interpolation=cv2.INTER_NEAREST)
        else:
            sigma = max(3.0, size * (0.10 + 0.25 * strength))
            # Downscale before blurring: equivalent result, far cheaper on large faces.
            factor = max(1, int(sigma // 6))
            small = cv2.resize(patch, (max(1, patch.shape[1] // factor), max(1, patch.shape[0] // factor)),
                               interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), sigma / factor)
            treated = cv2.resize(small, (patch.shape[1], patch.shape[0]), interpolation=cv2.INTER_LINEAR)
        patch[inside] = treated[inside]
    return out


def covers(mask_faces: list[dict], bbox, scale: float = 1.0) -> bool:
    """True if a detected box's centre lies inside one of the anonymized ellipses."""
    x, y, w, h = bbox
    cx, cy = x + w / 2, y + h / 2
    for face in mask_faces:
        (ex, ey), (ax, ay), _angle = _ellipse(face["bbox"], face.get("landmarks"), scale)
        if ((cx - ex) / max(ax, 1e-6)) ** 2 + ((cy - ey) / max(ay, 1e-6)) ** 2 <= 1.0:
            return True
    return False
