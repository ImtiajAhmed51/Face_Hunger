"""Exact + multi perceptual-hash duplicate detection for photos and videos."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


def content_hash(path: Path, *, max_bytes: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if max_bytes is None:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        else:
            remaining = max_bytes
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
    return h.hexdigest()


def _to_gray(bgr: np.ndarray) -> np.ndarray:
    if bgr is None or bgr.size == 0:
        return np.zeros((8, 8), dtype=np.uint8)
    if bgr.ndim == 3:
        return (0.114 * bgr[:, :, 0] + 0.587 * bgr[:, :, 1] + 0.299 * bgr[:, :, 2]).astype(np.uint8)
    return bgr.astype(np.uint8)


def _bits_to_hex(bits: np.ndarray) -> str:
    value = 0
    for bit in bits.flatten().astype(np.uint8):
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _ahash(gray: np.ndarray, size: int = 8) -> str:
    img = Image.fromarray(gray).resize((size, size), Image.Resampling.LANCZOS)
    pixels = np.asarray(img, dtype=np.float32)
    return _bits_to_hex(pixels > pixels.mean())


def _dhash(gray: np.ndarray, size: int = 8) -> str:
    img = Image.fromarray(gray).resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = np.asarray(img, dtype=np.float32)
    return _bits_to_hex(pixels[:, 1:] > pixels[:, :-1])


def _phash(gray: np.ndarray, hash_size: int = 8, highfreq: int = 32) -> str:
    """DCT perceptual hash — best for resized / recompressed photos."""
    img = Image.fromarray(gray).resize((highfreq, highfreq), Image.Resampling.LANCZOS)
    pixels = np.asarray(img, dtype=np.float32)
    # 2D DCT via successive 1D DCTs
    dct_rows = np.apply_along_axis(
        lambda row: np.fft.fft(np.concatenate([row, row[::-1]])).real[:highfreq],
        1,
        pixels,
    )
    dct = np.apply_along_axis(
        lambda col: np.fft.fft(np.concatenate([col, col[::-1]])).real[:highfreq],
        0,
        dct_rows,
    )
    low = dct[:hash_size, :hash_size].copy()
    low[0, 0] = 0  # ignore DC
    med = np.median(low)
    return _bits_to_hex(low > med)


def image_phash(bgr: np.ndarray, hash_size: int = 8) -> str:
    """Return combined hash: aHash(16) + dHash(16) + pHash(16) = 48 hex chars."""
    gray = _to_gray(bgr)
    if gray.size == 0:
        return ""
    return _ahash(gray, hash_size) + _dhash(gray, hash_size) + _phash(gray, hash_size)


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    try:
        x = int(a, 16) ^ int(b, 16)
        return bin(x).count("1")
    except ValueError:
        return 64


def video_phash(
    path: Path,
    duration: Optional[float],
    frame_at_fn,
    hash_size: int = 8,
) -> str:
    if duration is None or duration <= 0:
        times = [0.0]
    else:
        times = [0.05 * duration, 0.35 * duration, 0.65 * duration, 0.9 * duration]
    parts = []
    for t in times:
        try:
            bgr = frame_at_fn(path, max(0.0, t))
            if bgr is not None and getattr(bgr, "size", 0) > 0:
                parts.append(image_phash(bgr, hash_size))
        except Exception:
            continue
    if not parts:
        return ""
    while len(parts) < 4:
        parts.append(parts[-1])
    return "".join(parts[:4])  # 4 * 48 = 192 hex


def near_duplicate(phash_a: str, phash_b: str, *, max_distance: int = 10) -> bool:
    """True if two combined hashes are close enough.

    Image hash length: 48 hex (3×16).
    Video hash length: 192 hex (4 frames × 48).
    """
    if not phash_a or not phash_b:
        return False
    if len(phash_a) != len(phash_b):
        return False

    # Image: 48 hex → 3 hashes of 16
    if len(phash_a) == 48:
        da = hamming(phash_a[0:16], phash_b[0:16])   # aHash
        dd = hamming(phash_a[16:32], phash_b[16:32])  # dHash
        dp = hamming(phash_a[32:48], phash_b[32:48])  # pHash
        # Any two hashes close, or average low, or pHash very close
        close = sum(1 for d in (da, dd, dp) if d <= max_distance)
        avg = (da + dd + dp) / 3
        return close >= 2 or avg <= max_distance or dp <= max_distance - 2

    # Video: average over frame blocks of 48
    if len(phash_a) == 192:
        scores = []
        for i in range(0, 192, 48):
            if near_duplicate(phash_a[i : i + 48], phash_b[i : i + 48], max_distance=max_distance):
                scores.append(1)
            else:
                scores.append(0)
        return sum(scores) >= 2  # at least 2 of 4 frames match

    # Legacy 16-char dHash
    if len(phash_a) == 16:
        return hamming(phash_a, phash_b) <= max_distance

    return False