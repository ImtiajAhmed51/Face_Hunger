"""Optional local vision-language model (SmolVLM2-500M, Apache-2.0) on ONNX Runtime.

This module is imported only when the user has enabled the VLM in Settings; nothing at
startup touches it. Three ONNX graphs are used: a vision encoder (one 512 px image -> 64
tokens), the token embedder, and the decoder with a key/value cache. Decoding is greedy:
the tasks here (titles, one-line captions, small JSON) want the most likely answer, and
it keeps runs reproducible.

The model is loaded on first use, unloaded after ``idle_seconds`` without use, and its
memory is measured (process RSS before/after load) so the status card and the tests can
check the 4 GB cap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MODEL_ID = "smolvlm2-500m"
LICENSE = "Apache-2.0"
RAM_CAP_BYTES = 4 * 1024 ** 3
FILES = ("vision_encoder.onnx", "embed_tokens.onnx", "decoder.onnx", "tokenizer.json", "config.json")
IMAGE_SIZE = 512
IMAGE_TOKENS = 64


def _rss() -> int:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except Exception:
        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)


class VLMUnavailable(RuntimeError):
    pass


class LocalVLM:
    def __init__(self, model_dir: Path, *, idle_seconds: float = 300.0):
        self.dir = Path(model_dir) / MODEL_ID
        self.idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._sessions: Optional[dict] = None
        self._tokenizer = None
        self._config: dict = {}
        self._last_used = 0.0
        self._timer: Optional[threading.Timer] = None
        self.loaded_bytes = 0
        self.load_seconds = 0.0
        self.provider = None
        self.generations = 0

    # -- lifecycle ----------------------------------------------------------------
    @property
    def installed(self) -> bool:
        return all((self.dir / name).is_file() for name in FILES)

    @property
    def loaded(self) -> bool:
        return self._sessions is not None

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.dir.glob("*") if p.is_file()) if self.dir.is_dir() else 0

    def status(self) -> dict:
        return {"model": MODEL_ID, "license": LICENSE, "installed": self.installed, "loaded": self.loaded,
                "disk_bytes": self.disk_bytes(), "ram_bytes": self.loaded_bytes if self.loaded else 0,
                "ram_cap_bytes": RAM_CAP_BYTES, "device": self.provider, "load_seconds": round(self.load_seconds, 2),
                "idle_seconds": self.idle_seconds, "generations": self.generations}

    def load(self) -> dict:
        with self._lock:
            if self._sessions is not None:
                self._touch()
                return self.status()
            if not self.installed:
                raise VLMUnavailable("The local VLM is not installed. Run: python scripts/fetch_models.py --only smolvlm2-500m")
            import onnxruntime as ort
            from tokenizers import Tokenizer

            before, started = _rss(), time.perf_counter()
            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 4) - 1))
            options.enable_cpu_mem_arena = False  # return memory to the OS on unload
            providers = ["CPUExecutionProvider"]  # quantized graphs: CPU is the reliable provider
            self._sessions = {name: ort.InferenceSession(str(self.dir / f"{name}.onnx"), options, providers=providers)
                              for name in ("vision_encoder", "embed_tokens", "decoder")}
            self._tokenizer = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
            self._config = json.loads((self.dir / "config.json").read_text())
            self.provider = "CPU"
            self.load_seconds = time.perf_counter() - started
            self.loaded_bytes = max(0, _rss() - before)
            if self.loaded_bytes > RAM_CAP_BYTES:
                self.unload()
                raise VLMUnavailable("The local VLM needs more memory than the 4 GB cap allows")
            self._touch()
            logger.info("VLM loaded in %.1fs (%.0f MB)", self.load_seconds, self.loaded_bytes / 1e6)
            return self.status()

    def unload(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._sessions = None
            self._tokenizer = None
            import gc

            gc.collect()

    def _touch(self) -> None:
        self._last_used = time.monotonic()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.idle_seconds, self._idle_check)
        self._timer.daemon = True
        self._timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            if self._sessions is not None and time.monotonic() - self._last_used >= self.idle_seconds - 0.05:
                logger.info("VLM idle for %.0fs: unloading", self.idle_seconds)
                self.unload()

    # -- inference -----------------------------------------------------------------------
    @staticmethod
    def _pixels(image) -> tuple[np.ndarray, np.ndarray]:
        """Letterbox an RGB PIL image into 512x512; mask marks the real pixels."""
        from PIL import Image

        image = image.convert("RGB")
        scale = IMAGE_SIZE / max(image.size)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        pixels = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), np.float32)
        mask = np.zeros((IMAGE_SIZE, IMAGE_SIZE), bool)
        pixels[:size[1], :size[0]] = (np.asarray(image, np.float32) / 255.0 - 0.5) / 0.5
        mask[:size[1], :size[0]] = True
        return pixels.transpose(2, 0, 1)[None, None], mask[None, None]

    def generate(self, prompt: str, image=None, *, max_new_tokens: int = 64) -> str:
        """Greedy answer to ``prompt`` (about ``image`` when given)."""
        with self._lock:
            self.load()
            sessions, tok = self._sessions, self._tokenizer
            image_token = int(self._config.get("image_token_id", 49190))
            eos = tok.token_to_id("<end_of_utterance>")
            if image is not None:
                body = "<fake_token_around_image><global-img>" + "<image>" * IMAGE_TOKENS + "<fake_token_around_image>"
                text = f"<|im_start|>User:{body}{prompt}<end_of_utterance>\nAssistant:"
            else:
                text = f"<|im_start|>User: {prompt}<end_of_utterance>\nAssistant:"
            ids = np.array([tok.encode(text, add_special_tokens=False).ids], dtype=np.int64)
            embeds = sessions["embed_tokens"].run(None, {"input_ids": ids})[0]
            if image is not None:
                pixels, mask = self._pixels(image)
                features = sessions["vision_encoder"].run(None, {"pixel_values": pixels, "pixel_attention_mask": mask})[0]
                slots = np.nonzero(ids[0] == image_token)[0]
                embeds[0, slots] = features.reshape(-1, features.shape[-1])[:len(slots)]
            text_cfg = self._config.get("text_config", {})
            layers = int(text_cfg.get("num_hidden_layers", 32))
            heads = int(text_cfg.get("num_key_value_heads", 5))
            head_dim = int(text_cfg.get("head_dim", 64))
            past = {f"past_key_values.{i}.{kind}": np.zeros((1, heads, 0, head_dim), np.float32)
                    for i in range(layers) for kind in ("key", "value")}
            attention = np.ones((1, ids.shape[1]), np.int64)
            positions = np.arange(1, ids.shape[1] + 1, dtype=np.int64)[None]
            out: list[int] = []
            decoder = sessions["decoder"]
            for _ in range(max_new_tokens):
                results = decoder.run(None, {"inputs_embeds": embeds, "attention_mask": attention, "position_ids": positions, **past})
                token = int(results[0][0, -1].argmax())
                if token == eos:
                    break
                out.append(token)
                for i in range(layers):
                    past[f"past_key_values.{i}.key"] = results[1 + 2 * i]
                    past[f"past_key_values.{i}.value"] = results[2 + 2 * i]
                embeds = sessions["embed_tokens"].run(None, {"input_ids": np.array([[token]], np.int64)})[0]
                attention = np.ones((1, attention.shape[1] + 1), np.int64)
                positions = positions[:, -1:] + 1
            self.generations += 1
            self._touch()
            return tok.decode(out, skip_special_tokens=True).strip()
