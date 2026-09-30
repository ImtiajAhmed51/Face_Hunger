"""Identity of an embedding space: (model_id, version, dim).

A new model or a new version of an existing model always gets a new key, so
its vectors live beside the old ones and never invalidate existing indexes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    version: str
    dim: int
    subject: str  # "media" (one vector per media item) or "face" (per face row)
    role: str  # "text_image" | "visual" | "face_identity"

    @property
    def key(self) -> str:
        return f"{self.model_id}@{self.version}:{self.dim}"

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", self.key)


# Existing stores adopted in place (migration 6).
LEGACY_DINO = ModelSpec("dinov2-vitb14-torchhub", "1", 768, "media", "visual")
FACE_ARCFACE = ModelSpec("buffalo_l-w600k_r50", "1", 512, "face", "face_identity")

# ONNX models loaded from LFS_MODEL_DIR (see scripts/fetch_models.py).
SIGLIP2_BASE = ModelSpec("siglip2-base-patch16-224", "1", 768, "media", "text_image")
DINOV2_SMALL = ModelSpec("dinov2-small", "1", 384, "media", "visual")
DINOV2_BASE = ModelSpec("dinov2-base", "1", 768, "media", "visual")

KNOWN = {s.key: s for s in (LEGACY_DINO, FACE_ARCFACE, SIGLIP2_BASE, DINOV2_SMALL, DINOV2_BASE)}
