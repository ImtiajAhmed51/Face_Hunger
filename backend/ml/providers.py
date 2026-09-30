"""ONNX Runtime execution-provider selection with clean fallback.

Order: CUDA -> CoreML (Apple GPU/ANE; ONNX Runtime has no MPS provider) ->
DirectML -> CPU. ``LFS_ONNX_PROVIDER`` pins one provider (e.g. ``cpu``).
A session that fails to build on an accelerator is rebuilt on the next
provider, so a model always loads if CPU can run it.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ORDER = ("CUDAExecutionProvider", "CoreMLExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider")
LABELS = {
    "CUDAExecutionProvider": "CUDA",
    "CoreMLExecutionProvider": "Apple CoreML (GPU/ANE)",
    "DmlExecutionProvider": "DirectML",
    "CPUExecutionProvider": "CPU",
}
_ALIASES = {"cuda": "CUDAExecutionProvider", "coreml": "CoreMLExecutionProvider", "mps": "CoreMLExecutionProvider",
            "dml": "DmlExecutionProvider", "directml": "DmlExecutionProvider", "cpu": "CPUExecutionProvider"}


def label(provider: str | None) -> str:
    return LABELS.get(provider or "", provider or "unloaded")


def candidates(*, accelerate: bool = True) -> list[str]:
    import onnxruntime as ort

    available = set(ort.get_available_providers())
    forced = _ALIASES.get((os.environ.get("LFS_ONNX_PROVIDER") or "").strip().lower())
    if forced:
        order = [forced, "CPUExecutionProvider"]
    else:
        order = list(ORDER) if accelerate else ["CPUExecutionProvider"]
    seen, out = set(), []
    for name in order:
        if name and name in available and name not in seen:
            seen.add(name)
            out.append(name)
    return out or ["CPUExecutionProvider"]


def create_session(path, *, threads: int = 0, accelerate: bool = True, skip=()):
    """Return (InferenceSession, provider_name) using the first provider that works.

    ``accelerate=False`` goes straight to CPU (int8-quantized graphs are
    fastest there and CoreML rejects some of their ops at run time).
    ``skip`` lists providers that already failed for this model.
    """
    import onnxruntime as ort

    last_error = None
    for provider in [p for p in candidates(accelerate=accelerate) if p not in skip] or ["CPUExecutionProvider"]:
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            options.intra_op_num_threads = threads
        # Keep memory arenas from holding RAM after large batches.
        options.enable_cpu_mem_arena = False
        chain = [provider] if provider == "CPUExecutionProvider" else [provider, "CPUExecutionProvider"]
        try:
            session = ort.InferenceSession(str(path), sess_options=options, providers=chain)
            used = session.get_providers()[0]
            return session, used
        except Exception as exc:  # accelerator refused the graph: fall through
            last_error = exc
            logger.warning("ONNX provider %s failed for %s: %s", provider, path, exc)
    raise RuntimeError(f"No ONNX Runtime provider could load {path}: {last_error}")
