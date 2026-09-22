"""DINOv2 visual embeddings for media-level duplicate / near-duplicate detection.

Lazy-loads facebookresearch DINOv2 (ViT-B/14 → 768-D) via torch hub.
Embeddings are L2-normalized for cosine similarity via inner product.

On Apple Silicon, prefers MPS (M2/M-series GPU) when available.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# Safer defaults before torch is imported (macOS / OpenMP / conda conflicts)
os.environ.setdefault("XFORMERS_DISABLED", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("KMP_INIT_AT_FORK", "FALSE")
# Do not force CPU; allow automatic MPS selection unless user overrides

DINO_DIM = 768
_MODEL = None
_TRANSFORM = None
_LOCK = threading.Lock()
_DEVICE = "cpu"
_ERROR: Optional[str] = None
_DEVICE_LABEL = "CPU"


def _device_label(device: str) -> str:
    if device == "mps":
        return "Apple MPS (M2 GPU)"
    if device == "cuda":
        return "CUDA"
    return "CPU"


def _pick_device(torch) -> str:
    """Automatic device selection: MPS (Apple Silicon) → CUDA → CPU.

    Respects LFS_DINO_DEVICE override when set to cpu|cuda|mps.
    """
    forced = (os.environ.get("LFS_DINO_DEVICE") or "").strip().lower()
    if forced in ("cpu", "cuda", "mps"):
        if forced == "cuda" and not torch.cuda.is_available():
            logger.warning("LFS_DINO_DEVICE=cuda but CUDA unavailable; falling back to CPU")
            return "cpu"
        if forced == "mps":
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
            logger.warning("LFS_DINO_DEVICE=mps but MPS unavailable; falling back to CPU")
            return "cpu"
        return forced
    # Prefer Apple Silicon GPU (MPS) when available
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _ensure_model():
    global _MODEL, _TRANSFORM, _DEVICE, _ERROR, _DEVICE_LABEL
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

            # Limit intra-op threads (helps avoid OpenMP issues on macOS)
            try:
                torch.set_num_threads(1)
                torch.set_num_interop_threads(1)
            except Exception:
                pass

            preferred = _pick_device(torch)
            _DEVICE = preferred
            _DEVICE_LABEL = _device_label(_DEVICE)

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

            # Try preferred device; fall back to CPU on MPS failure
            try:
                model = model.to(_DEVICE)
                with torch.inference_mode():
                    dummy = torch.zeros(1, 3, 224, 224, device=_DEVICE)
                    _ = model(dummy)
            except Exception as mps_exc:
                if _DEVICE == "mps":
                    logger.warning(
                        "DINOv2 MPS init failed (%s); falling back to CPU", mps_exc
                    )
                    _DEVICE = "cpu"
                    _DEVICE_LABEL = "CPU"
                    model = model.to("cpu")
                    with torch.inference_mode():
                        dummy = torch.zeros(1, 3, 224, 224, device="cpu")
                        _ = model(dummy)
                else:
                    raise

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
            logger.info(
                "DINOv2 Device: %s | DINOv2 vitb14 ready on %s (python %s)",
                _DEVICE_LABEL,
                _DEVICE,
                sys.version.split()[0],
            )
        except BaseException as exc:
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
        "device_label": _DEVICE_LABEL if _MODEL is not None else None,
        "dim": DINO_DIM,
        "error": _ERROR,
    }


def _bgr_to_tensor(bgr: np.ndarray):
    """Convert BGR uint8 → normalized CHW float tensor (on CPU; moved later)."""
    from PIL import Image

    if bgr is None or getattr(bgr, "size", 0) == 0:
        raise ValueError("Empty image")
    if bgr.ndim == 2:
        rgb = np.stack([bgr, bgr, bgr], axis=-1)
    else:
        rgb = bgr[:, :, ::-1]  # BGR → RGB
    pil = Image.fromarray(np.ascontiguousarray(rgb.astype(np.uint8)))
    return _TRANSFORM(pil)


def embed_bgr(bgr: np.ndarray) -> np.ndarray:
    """Embed a single BGR uint8 image → unit 768-D float32 vector (on MPS when available)."""
    _ensure_model()
    import torch

    tensor = _bgr_to_tensor(bgr).unsqueeze(0).to(_DEVICE, non_blocking=True)
    with torch.inference_mode():
        feat = _MODEL(tensor)
        if isinstance(feat, (tuple, list)):
            feat = feat[0]
        # Keep on device for norm, transfer once
        vec = feat.squeeze(0).float()
        n = torch.linalg.vector_norm(vec)
        if float(n) < 1e-12:
            raise ValueError("Zero-norm DINOv2 embedding")
        vec = (vec / n).cpu().numpy()
    return np.ascontiguousarray(vec.astype(np.float32))


def embed_bgr_batch(images: Sequence[np.ndarray], batch_size: int = 8) -> List[np.ndarray]:
    """Batch-embed BGR images on the selected device (MPS preferred).

    Returns list of unit 768-D float32 vectors in the same order.
    Processes in chunks of `batch_size` to limit GPU memory.
    """
    if not images:
        return []
    _ensure_model()
    import torch

    results: List[np.ndarray] = []
    for start in range(0, len(images), max(1, batch_size)):
        chunk = images[start : start + batch_size]
        tensors = []
        for bgr in chunk:
            tensors.append(_bgr_to_tensor(bgr))
        batch = torch.stack(tensors, dim=0).to(_DEVICE, non_blocking=True)
        with torch.inference_mode():
            feat = _MODEL(batch)
            if isinstance(feat, (tuple, list)):
                feat = feat[0]
            feat = feat.float()
            norms = torch.linalg.vector_norm(feat, dim=1, keepdim=True).clamp_min(1e-12)
            feat = (feat / norms).cpu().numpy()
        for i in range(feat.shape[0]):
            results.append(np.ascontiguousarray(feat[i].astype(np.float32)))
    return results


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
    """Average DINOv2 embeddings of representative frames (unit vector).

    Frame embeddings are computed in one GPU batch when possible.
    """
    if duration is None or duration <= 0:
        times = [0.0]
    else:
        fracs = np.linspace(0.08, 0.92, n_frames)
        times = [float(f * duration) for f in fracs]

    frames: List[np.ndarray] = []
    for t in times:
        try:
            bgr = frame_at_fn(path, max(0.0, t))
            if bgr is not None and getattr(bgr, "size", 0) > 0:
                frames.append(bgr)
        except Exception:
            continue
    if not frames:
        raise ValueError(f"Could not extract frames from {path}")

    vectors = embed_bgr_batch(frames, batch_size=max(4, len(frames)))
    mean = np.mean(np.stack(vectors, axis=0), axis=0)
    n = float(np.linalg.norm(mean))
    if n < 1e-12:
        raise ValueError("Zero-norm video embedding")
    return np.ascontiguousarray((mean / n).astype(np.float32))
