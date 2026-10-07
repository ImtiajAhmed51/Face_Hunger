"""Colour-palette embedding: a 4x4x4 RGB histogram of the whole image plus a 2x2 grid of mean colours.

Replace ``Embedder.embed`` with your own model. The contract:

    embed(images: list[bytes]) -> list[list[float]]

``images`` are JPEG previews (longest side <= 384 px). Return one vector of length
``dim`` (see plugin.toml) per image; the app normalises and indexes them.
"""

import io

import numpy as np
from PIL import Image


class Embedder:
    def embed(self, images):
        return [self._one(data).tolist() for data in images]

    @staticmethod
    def _one(data: bytes) -> np.ndarray:
        pixels = np.asarray(Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64)), dtype=np.float32)
        bins = (pixels // 64).astype(np.int64).reshape(-1, 3)
        hist = np.bincount(bins[:, 0] * 16 + bins[:, 1] * 4 + bins[:, 2], minlength=64).astype(np.float32)
        hist = np.sqrt(hist / hist.sum())
        grid = pixels.reshape(2, 32, 2, 32, 3).mean(axis=(1, 3)).reshape(-1) / 255.0      # 12 values
        tone = np.array([pixels.mean() / 255.0, pixels.std() / 128.0, *(pixels.mean(axis=(0, 1)) / 255.0)[:2]], dtype=np.float32)
        vector = np.concatenate([hist, 0.5 * grid, 0.5 * tone]).astype(np.float32)        # 64 + 12 + 4 = 80
        return vector / max(float(np.linalg.norm(vector)), 1e-12)
