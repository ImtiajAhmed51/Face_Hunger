"""Lazy, idle-unloading ONNX models: SigLIP 2 (text<->image) and DINOv2 (visual).

Models are read only from ``LFS_MODEL_DIR``; nothing is ever downloaded.
Each ONNX graph is a separate slot that loads on first use and is released
after ``idle_seconds`` without calls, so the text encoder (only needed for
queries) and the vision encoders (only needed while indexing) never sit in
RAM together for long.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..ops import diagnostics
from ..vectors.specs import DINOV2_BASE, DINOV2_SMALL, SIGLIP2_BASE, ModelSpec
from . import providers

logger = logging.getLogger(__name__)

PIL_RESAMPLE = {0: "NEAREST", 1: "LANCZOS", 2: "BILINEAR", 3: "BICUBIC"}


def _precision(folder: Path) -> str:
    try:
        return json.loads((folder / "MANIFEST.json").read_text()).get("precision", "fp32")
    except Exception:
        return "fp32"


class OnnxSlot:
    def __init__(self, path: Path, name: str):
        self.path, self.name = Path(path), name
        self.accelerate = _precision(self.path.parent) != "int8"
        self.failed_providers: list[str] = []
        self._lock = threading.Lock()
        self._session = None
        self.provider: Optional[str] = None
        self.error: Optional[str] = None
        self.last_used = 0.0
        self.loads = 0

    @property
    def installed(self) -> bool:
        return self.path.is_file() and self.path.stat().st_size > 0

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def session(self):
        with self._lock:
            self.last_used = time.monotonic()
            diagnostics.count("model_session", self._session is not None)
            if self._session is None:
                if not self.installed:
                    raise FileNotFoundError(f"{self.path} is missing; run scripts/fetch_models.py")
                started = time.monotonic()
                try:
                    self._session, self.provider = providers.create_session(
                        self.path, accelerate=self.accelerate, skip=self.failed_providers)
                    self.error = None
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    raise
                self.loads += 1
                diagnostics.record("model_load", self.name, (time.monotonic() - started) * 1000)
                logger.info("Loaded %s on %s in %.1fs", self.name, providers.label(self.provider),
                            time.monotonic() - started)
            return self._session

    def run(self, feeds: dict) -> list:
        """Run inference; if an accelerator fails at run time, retry on the next provider."""
        while True:
            session = self.session()
            try:
                return session.run(None, feeds)
            except Exception as exc:
                if self.provider in (None, "CPUExecutionProvider"):
                    raise
                logger.warning("%s failed on %s (%s); falling back", self.name, self.provider, exc)
                with self._lock:
                    self.failed_providers.append(self.provider)
                    self._session = None

    def unload_if_idle(self, idle_seconds: float) -> bool:
        with self._lock:
            if self._session is not None and time.monotonic() - self.last_used > idle_seconds:
                self._session = None
                logger.info("Unloaded idle model %s", self.name)
                return True
        return False

    def unload(self) -> None:
        with self._lock:
            self._session = None

    def status(self) -> dict:
        return {"name": self.name, "path": str(self.path), "installed": self.installed, "loaded": self.loaded,
                "failed_providers": list(self.failed_providers), "accelerate": self.accelerate,
                "provider": self.provider, "provider_label": providers.label(self.provider) if self.provider else None,
                "error": self.error}


def _pick_output(session, outputs, preferred: Sequence[str]) -> np.ndarray:
    names = [o.name for o in session.get_outputs()]
    for want in preferred:
        if want in names:
            return np.asarray(outputs[names.index(want)])
    for value in outputs:
        value = np.asarray(value)
        if value.ndim == 2:
            return value
    first = np.asarray(outputs[0])
    return first[:, 0, :] if first.ndim == 3 else first


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    return np.ascontiguousarray(matrix / norms)


def _feed_dtype(session, name: str):
    for item in session.get_inputs():
        if item.name == name:
            return np.float16 if "float16" in item.type else np.float32
    return np.float32


class ImagePreprocessor:
    """Implements the subset of HF image processors used by SigLIP 2 / DINOv2."""

    def __init__(self, config_path: Path, *, default_size=224, default_mean=(0.5, 0.5, 0.5), default_std=(0.5, 0.5, 0.5)):
        cfg = json.loads(config_path.read_text()) if config_path.is_file() else {}
        size = cfg.get("size") or {}
        self.shortest_edge = size.get("shortest_edge")
        self.size = (size.get("height", default_size), size.get("width", default_size))
        crop = cfg.get("crop_size") or {}
        self.crop = (crop.get("height"), crop.get("width")) if cfg.get("do_center_crop") and crop else None
        self.mean = np.asarray(cfg.get("image_mean", default_mean), dtype=np.float32).reshape(3, 1, 1)
        self.std = np.asarray(cfg.get("image_std", default_std), dtype=np.float32).reshape(3, 1, 1)
        self.scale = float(cfg.get("rescale_factor", 1 / 255))
        self.resample = PIL_RESAMPLE.get(int(cfg.get("resample", 2)), "BILINEAR")

    def __call__(self, bgr: np.ndarray) -> np.ndarray:
        from PIL import Image

        if bgr is None or getattr(bgr, "size", 0) == 0:
            raise ValueError("Empty image")
        rgb = np.stack([bgr] * 3, axis=-1) if bgr.ndim == 2 else bgr[:, :, 2::-1]
        image = Image.fromarray(np.ascontiguousarray(rgb.astype(np.uint8)))
        resample = getattr(Image.Resampling, self.resample)
        if self.shortest_edge:
            w, h = image.size
            s = self.shortest_edge / min(w, h)
            image = image.resize((max(1, round(w * s)), max(1, round(h * s))), resample)
        else:
            image = image.resize((self.size[1], self.size[0]), resample)
        if self.crop:
            ch, cw = self.crop
            w, h = image.size
            left, top = max(0, (w - cw) // 2), max(0, (h - ch) // 2)
            image = image.crop((left, top, left + cw, top + ch))
        array = np.asarray(image, dtype=np.float32).transpose(2, 0, 1) * self.scale
        return (array - self.mean) / self.std


class SigLIP2:
    spec = SIGLIP2_BASE

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.vision = OnnxSlot(self.folder / "vision.onnx", "siglip2-vision")
        self.text = OnnxSlot(self.folder / "text.onnx", "siglip2-text")
        self._tokenizer = None
        self._tok_lock = threading.Lock()
        self.preprocess = ImagePreprocessor(self.folder / "preprocessor_config.json")
        cfg_path = self.folder / "config.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
        self.max_length = int((cfg.get("text_config") or {}).get("max_position_embeddings", 64))

    @property
    def installed(self) -> bool:
        return self.vision.installed and self.text.installed and (self.folder / "tokenizer.json").is_file()

    def slots(self):
        return (self.vision, self.text)

    def tokenizer(self):
        with self._tok_lock:
            if self._tokenizer is None:
                from tokenizers import Tokenizer

                tok = Tokenizer.from_file(str(self.folder / "tokenizer.json"))
                pad_id = tok.token_to_id("<pad>")
                tok.enable_truncation(self.max_length)
                tok.enable_padding(length=self.max_length, pad_id=pad_id if pad_id is not None else 0,
                                   pad_token="<pad>")
                self._tokenizer = tok
            return self._tokenizer

    def embed_images(self, images: Sequence[np.ndarray], batch_size: int = 8) -> np.ndarray:
        session = self.vision.session()
        name = session.get_inputs()[0].name
        dtype = _feed_dtype(session, name)
        out = []
        for start in range(0, len(images), batch_size):
            batch = np.stack([self.preprocess(im) for im in images[start:start + batch_size]]).astype(dtype)
            result = self.vision.run({name: batch})
            out.append(_pick_output(self.vision.session(), result, ("image_embeds", "pooler_output")))
            self.vision.last_used = time.monotonic()
        return _unit_rows(np.concatenate(out)) if out else np.zeros((0, self.spec.dim), np.float32)

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        session = self.text.session()
        # SigLIP 2 was trained on lower-cased text, padded to max_length.
        encodings = self.tokenizer().encode_batch([t.strip().lower() for t in texts])
        ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        feeds = {}
        for item in session.get_inputs():
            if item.name == "input_ids":
                feeds[item.name] = ids
            elif item.name == "attention_mask":
                feeds[item.name] = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
            elif item.name == "position_ids":
                feeds[item.name] = np.broadcast_to(np.arange(ids.shape[1], dtype=np.int64), ids.shape).copy()
        result = self.text.run(feeds)
        return _unit_rows(_pick_output(self.text.session(), result, ("text_embeds", "pooler_output")))


class DINOv2:
    def __init__(self, folder: Path, spec: ModelSpec):
        self.folder, self.spec = Path(folder), spec
        self.model = OnnxSlot(self.folder / "model.onnx", spec.model_id)
        self.preprocess = ImagePreprocessor(self.folder / "preprocessor_config.json",
                                            default_mean=(0.485, 0.456, 0.406), default_std=(0.229, 0.224, 0.225))

    @property
    def installed(self) -> bool:
        return self.model.installed

    def slots(self):
        return (self.model,)

    def embed_images(self, images: Sequence[np.ndarray], batch_size: int = 8) -> np.ndarray:
        session = self.model.session()
        name = session.get_inputs()[0].name
        dtype = _feed_dtype(session, name)
        out = []
        for start in range(0, len(images), batch_size):
            batch = np.stack([self.preprocess(im) for im in images[start:start + batch_size]]).astype(dtype)
            result = self.model.run({name: batch})
            out.append(_pick_output(self.model.session(), result, ("pooler_output",)))
            self.model.last_used = time.monotonic()
        return _unit_rows(np.concatenate(out)) if out else np.zeros((0, self.spec.dim), np.float32)


def _media_frames(row: dict, frames: int = 4) -> list[np.ndarray]:
    from ..media_processing import frame_at, load_image

    path = Path(row["path"])
    if row["kind"] == "photo":
        return [load_image(path)]
    duration = row.get("duration")
    stamps = [0.0] if not duration or duration <= 0 else [float(f * duration) for f in np.linspace(0.08, 0.92, frames)]
    out = []
    for t in stamps:
        try:
            bgr = frame_at(path, max(0.0, t))
            if bgr is not None and getattr(bgr, "size", 0):
                out.append(bgr)
        except Exception:
            continue
    if not out:
        raise ValueError(f"Could not extract frames from {path.name}")
    return out


class MediaEmbedder:
    """Adapts an image model to the vector-space Embedder protocol (photos + videos)."""

    def __init__(self, spec: ModelSpec, embed_images):
        self.spec = spec
        self._embed_images = embed_images

    def embed_media(self, rows: Sequence[dict]) -> list:
        results: list = [None] * len(rows)
        images, owners = [], []
        for i, row in enumerate(rows):
            try:
                frames = _media_frames(row)
                images.extend(frames)
                owners.extend([i] * len(frames))
            except Exception as exc:
                results[i] = exc
        if images:
            try:
                vectors = self._embed_images(images)
            except Exception as exc:
                return [r if r is not None else exc for r in results]
            for i in set(owners):
                mean = vectors[[j for j, o in enumerate(owners) if o == i]].mean(axis=0)
                results[i] = mean / max(float(np.linalg.norm(mean)), 1e-12)
        return results


class ModelHub:
    """Discovers installed models and unloads idle ones in the background."""

    def __init__(self, model_dir: Path, *, idle_seconds: float = 300.0, dino_variant: str = "small"):
        self.model_dir = Path(model_dir)
        self.idle_seconds = float(idle_seconds)
        self.siglip = SigLIP2(self.model_dir / SIGLIP2_BASE.model_id)
        dino_spec = DINOV2_BASE if dino_variant == "base" else DINOV2_SMALL
        self.dino = DINOv2(self.model_dir / dino_spec.model_id, dino_spec)
        self._stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, name="model-reaper", daemon=True)
            self._reaper.start()

    def _reap(self) -> None:
        while not self._stop.wait(min(30.0, max(1.0, self.idle_seconds / 4))):
            self.unload_idle()

    def unload_idle(self) -> int:
        return sum(slot.unload_if_idle(self.idle_seconds) for model in (self.siglip, self.dino) for slot in model.slots())

    def embedder(self, role: str) -> Optional[MediaEmbedder]:
        if role == "text_image" and self.siglip.installed:
            return MediaEmbedder(self.siglip.spec, self.siglip.embed_images)
        if role == "visual" and self.dino.installed:
            return MediaEmbedder(self.dino.spec, self.dino.embed_images)
        return None

    def text_encoder(self):
        return self.siglip if self.siglip.installed else None

    def image_encoder(self, role: str):
        model = self.siglip if role == "text_image" else self.dino
        return model if model.installed else None

    def installed_specs(self) -> list[ModelSpec]:
        return [m.spec for m in (self.siglip, self.dino) if m.installed]

    def status(self) -> dict:
        try:
            available = providers.candidates()
        except Exception:
            available = []
        models = []
        for model, license_ in ((self.siglip, "Apache-2.0"), (self.dino, "Apache-2.0")):
            models.append({"key": model.spec.key, "role": model.spec.role, "installed": model.installed,
                           "license": license_, "slots": [s.status() for s in model.slots()]})
        return {"model_dir": str(self.model_dir), "idle_seconds": self.idle_seconds,
                "provider_order": available, "models": models}

    def close(self) -> None:
        self._stop.set()
        for model in (self.siglip, self.dino):
            for slot in model.slots():
                slot.unload()
