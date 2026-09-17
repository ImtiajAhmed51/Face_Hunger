"""DINOv2 visual embeddings for media-level duplicate / near-duplicate detection.

Lazy-loads facebookresearch DINOv2 (ViT-B/14 → 768-D) via torch hub.
Embeddings are L2-normalized for cosine similarity via inner product.

On macOS, forces CPU by default to avoid MPS / torch-hub segfaults.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Safer defaults before torch is imported (macOS MPS / OpenMP / conda conflicts)
os.environ.setdefault("XFORMERS_DISABLED", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("KMP_INIT_AT_FORK", "FALSE")
os.environ.setdefault("LFS_DINO_DEVICE", "cpu")

DINO_DIM = 768
_MODEL = None
_TRANSFORM = None
_LOCK = threading.Lock()
_DEVICE = "cpu"
_ERROR: Optional[str] = None


def _pick_device(torch) -> str:
    """Prefer explicit env, then CUDA, never MPS by default (segfault risk)."""
    forced = (os.environ.get("LFS_DINO_DEVICE") or "").strip().lower()
    if forced in ("cpu", "cuda", "mps"):
        if forced == "cuda" and not torch.cuda.is_available():
            return "cpu"
        if forced == "mps":
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
            return "cpu"
        return forced
    if torch.cuda.is_available():
        return "cuda"
    # macOS MPS can segfault with dinov2 hub weights — stay on CPU unless forced
    return "cpu"


def _ensure_model():
    global _MODEL, _TRANSFORM, _DEVICE, _ERROR
    if _MODEL is not None:
        return
    if _ERROR is not None:
        raise RuntimeError(_ERROR)
    with _LOCK:
        if _MODEL is not None:
            return
        if _ERROR is not None:
            raise RuntimeError(_ERROR)
        try:
            import torch
            import torchvision.transforms as T

            # Limit intra-op threads (helps avoid OpenMP segfaults on macOS)
            try:
                torch.set_num_threads(1)
                torch.set_num_interop_threads(1)
            except Exception:
                pass

            _DEVICE = _pick_device(torch)

            # Prefer local hub cache; avoid re-download
            torch.hub.set_dir(
                os.environ.get(
                    "TORCH_HOME",
                    os.path.join(os.path.expanduser("~"), ".cache", "torch"),
                )
            )

            model = torch.hub.load(
                "facebookresearch/dinov2",
                "dinov2_vitb14",
                trust_repo=True,
                verbose=False,
            )
            model.eval()
            for p in model.parameters():
                p.requires_grad_(False)
            model = model.to(_DEVICE)
            # Warm up once on CPU/CUDA to catch load issues early
            with torch.inference_mode():
                dummy = torch.zeros(1, 3, 224, 224, device=_DEVICE)
                _ = model(dummy)

            _MODEL = model
            _TRANSFORM = T.Compose(
                [
                    T.Resize(256, interpolation=T.InterpolationMode.BICUBIC),
                    T.CenterCrop(224),
                    T.ToTensor(),
                    T.Normalize(
                        mean=(0.485, 0.456, 0.406),
                        std=(0.229, 0.224, 0.225),
                    ),
                ]
            )
            logger.info("DINOv2 vitb14 ready on %s (python %s)", _DEVICE, sys.version.split()[0])
        except BaseException as exc:
            # BaseException: catch SystemExit-like failures during hub load too
            _ERROR = f"DINOv2 unavailable: {type(exc).__name__}: {exc}"
            logger.exception(_ERROR)
            _MODEL = None
            raise RuntimeError(_ERROR) from exc


def available() -> bool:
    try:
        _ensure_model()
        return True
    except Exception:
        return False


def status() -> dict:
    return {
        "available": _MODEL is not None and _ERROR is None,
        "device": _DEVICE if _MODEL is not None else None,
        "dim": DINO_DIM,
        "error": _ERROR,
    }


def embed_bgr(bgr: np.ndarray) -> np.ndarray:
    """Embed a single BGR uint8 image → unit 768-D float32 vector."""
    _ensure_model()
    import torch
    from PIL import Image

    if bgr is None or getattr(bgr, "size", 0) == 0:
        raise ValueError("Empty image")
    if bgr.ndim == 2:
        rgb = np.stack([bgr, bgr, bgr], axis=-1)
    else:
        rgb = bgr[:, :, ::-1]  # BGR → RGB
    pil = Image.fromarray(np.ascontiguousarray(rgb.astype(np.uint8)))
    tensor = _TRANSFORM(pil).unsqueeze(0).to(_DEVICE)
    with torch.inference_mode():
        feat = _MODEL(tensor)
        if isinstance(feat, (tuple, list)):
            feat = feat[0]
        vec = feat.squeeze(0).detach().float().cpu().numpy()
    n = float(np.linalg.norm(vec))
    if n < 1e-12:
        raise ValueError("Zero-norm DINOv2 embedding")
    return np.ascontiguousarray((vec / n).astype(np.float32))


def embed_image_path(path: Path) -> np.ndarray:
    from .media_processing import load_image

    return embed_bgr(load_image(path))


def embed_video_path(
    path: Path,
    duration: Optional[float],
    frame_at_fn,
    *,
    n_frames: int = 4,
) -> np.ndarray:
    """Average DINOv2 embeddings of representative frames (unit vector)."""
    if duration is None or duration <= 0:
        times = [0.0]
    else:
        fracs = np.linspace(0.08, 0.92, n_frames)
        times = [float(f * duration) for f in fracs]
    vectors = []
    for t in times:
        try:
            bgr = frame_at_fn(path, max(0.0, t))
            if bgr is not None and getattr(bgr, "size", 0) > 0:
                vectors.append(embed_bgr(bgr))
        except Exception:
            continue
    if not vectors:
        raise ValueError(f"Could not extract frames from {path}")
    mean = np.mean(np.stack(vectors, axis=0), axis=0)
    n = float(np.linalg.norm(mean))
    if n < 1e-12:
        raise ValueError("Zero-norm video embedding")
    return np.ascontiguousarray((mean / n).astype(np.float32))