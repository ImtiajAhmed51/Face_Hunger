#!/usr/bin/env python3
"""Build the zero-shot aesthetic head for SigLIP 2 (offline, one-time; run after fetch_models).

    python scripts/build_aesthetic_head.py [--model-dir models]

The head is a single direction in SigLIP's image-text space: the mean text
embedding of "good photo" prompts minus that of "bad photo" prompts. Scoring a
photo is one dot product with its stored image embedding, so no extra model is
loaded at scoring time. Writes models/siglip2-base-patch16-224/aesthetic_head.json.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.scoring import AestheticHead  # noqa: E402
from backend.vectors.specs import SIGLIP2_BASE  # noqa: E402

POSITIVE = [
    "a beautiful photo", "a stunning, well composed photograph", "a professional high quality photo",
    "a sharp, well exposed photo with pleasing light", "an award winning photograph", "a lovely candid moment",
]
NEGATIVE = [
    "a blurry photo", "a badly composed snapshot", "an out of focus, noisy photo", "an overexposed washed out photo",
    "a dark underexposed photo", "a boring accidental picture of nothing", "a photo with motion blur",
]


def build_head(encoder, positive=POSITIVE, negative=NEGATIVE, *, scale: float = 40.0) -> AestheticHead:
    pos = encoder.embed_texts(positive).mean(axis=0)
    neg = encoder.embed_texts(negative).mean(axis=0)
    direction = pos - neg
    direction = direction / max(float(np.linalg.norm(direction)), 1e-12)
    return AestheticHead(direction.astype(np.float32), bias=0.0, scale=scale, model_key=encoder.spec.key, version="prompts-1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default=os.environ.get("LFS_MODEL_DIR", "models"))
    args = parser.parse_args()
    from backend.ml.models import SigLIP2

    folder = Path(args.model_dir) / SIGLIP2_BASE.model_id
    model = SigLIP2(folder)
    if not model.installed:
        print(f"SigLIP 2 is not installed in {folder}; run scripts/fetch_models.py first")
        return 1
    head = build_head(model)
    head.save(folder / "aesthetic_head.json")
    print(f"Wrote {folder / 'aesthetic_head.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
