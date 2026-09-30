"""DINOv2 visual embeddings (ONNX, loaded only from LFS_MODEL_DIR).

Replaces the former torch.hub implementation, which could download weights
at runtime. The model itself lives in :mod:`backend.ml.models`; this module
keeps the small status API the duplicates endpoints expose.
"""

from __future__ import annotations

from .ml import providers


def _hub():
    from .services.container import current

    return current().models


def status() -> dict:
    model = _hub().dino
    slot = model.model
    error = slot.error
    if not model.installed:
        error = f"DINOv2 ONNX model not installed at {model.folder}; run scripts/fetch_models.py"
    return {
        "available": model.installed and slot.error is None,
        "device": slot.provider,
        "device_label": providers.label(slot.provider) if slot.provider else None,
        "dim": model.spec.dim,
        "model": model.spec.key,
        "loaded": slot.loaded,
        "error": error,
    }


def available() -> bool:
    return bool(status()["available"])
