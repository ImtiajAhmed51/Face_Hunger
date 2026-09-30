#!/usr/bin/env python3
"""One-time, manual download of the optional ONNX models used by Face Hunger.

Face Hunger never downloads anything at runtime. Run this script yourself once;
it writes into LFS_MODEL_DIR (default ./models) and records SHA-256 checksums in
MANIFEST.json next to each model so the app can verify files on load.

    python scripts/fetch_models.py                 # SigLIP 2 + DINOv2-small
    python scripts/fetch_models.py --only dinov2-small
    python scripts/fetch_models.py --precision fp16  # GPU/CoreML-friendly weights
    python scripts/fetch_models.py --list

Models (all Apache-2.0):
  siglip2-base-patch16-224  text<->image embeddings (768-D)   google/siglip2-base-patch16-224
  dinov2-small              visual similarity (384-D)          facebook/dinov2-small
  dinov2-base               visual similarity (768-D)          facebook/dinov2-base

buffalo_l (face detection/recognition) is NOT fetched here: its weights are
licensed for non-commercial research only; install them yourself into
LFS_MODEL_DIR/buffalo_l if that licence fits your use.

Standard library only (urllib); no Hugging Face client is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

HF = "https://huggingface.co/{repo}/resolve/{revision}/{path}"

# Per precision: which exported file becomes the local canonical name.
MODELS = {
    "siglip2-base-patch16-224": {
        "repo": "onnx-community/siglip2-base-patch16-224-ONNX",
        "license": "Apache-2.0",
        "files": {
            "vision.onnx": {"int8": "onnx/vision_model_quantized.onnx", "fp16": "onnx/vision_model_fp16.onnx",
                            "fp32": "onnx/vision_model.onnx"},
            "text.onnx": {"int8": "onnx/text_model_quantized.onnx", "fp16": "onnx/text_model_fp16.onnx",
                          "fp32": "onnx/text_model.onnx"},
            "tokenizer.json": "tokenizer.json",
            "preprocessor_config.json": "preprocessor_config.json",
            "config.json": "config.json",
        },
    },
    "dinov2-small": {
        "repo": "onnx-community/dinov2-small-ONNX",
        "license": "Apache-2.0",
        "files": {
            # DINOv2-small is ~88 MB in fp32; quantizing it costs duplicate-detection accuracy.
            "model.onnx": {"int8": "onnx/model.onnx", "fp16": "onnx/model_fp16.onnx", "fp32": "onnx/model.onnx"},
            "preprocessor_config.json": "preprocessor_config.json",
            "config.json": "config.json",
        },
    },
    "dinov2-base": {
        "repo": "onnx-community/dinov2-base-ONNX",
        "license": "Apache-2.0",
        "files": {
            "model.onnx": {"int8": "onnx/model_quantized.onnx", "fp16": "onnx/model_fp16.onnx", "fp32": "onnx/model.onnx"},
            "preprocessor_config.json": "preprocessor_config.json",
            "config.json": "config.json",
        },
    },
}
DEFAULT = ("siglip2-base-patch16-224", "dinov2-small")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "face-hunger-fetch-models/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        fd, tmp = tempfile.mkstemp(prefix=".part-", dir=target.parent)
        done = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        sys.stdout.write(f"\r    {target.name}: {done * 100 // total:3d}% of {total / 1e6:.0f} MB")
                        sys.stdout.flush()
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    sys.stdout.write("\n")


def fetch(name: str, model_dir: Path, precision: str, revision: str, force: bool) -> None:
    spec = MODELS[name]
    dest = model_dir / name
    print(f"==> {name} ({spec['license']}) from {spec['repo']} [{precision}] -> {dest}")
    manifest = {"name": name, "repo": spec["repo"], "revision": revision, "precision": precision,
                "license": spec["license"], "files": {}}
    for local, remote in spec["files"].items():
        remote_path = remote[precision] if isinstance(remote, dict) else remote
        target = dest / local
        if target.is_file() and not force:
            print(f"    {local}: present, skipping (use --force to re-download)")
        else:
            download(HF.format(repo=spec["repo"], revision=revision, path=remote_path), target)
        manifest["files"][local] = {"source": remote_path, "sha256": sha256(target), "bytes": target.stat().st_size}
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"    wrote {dest / 'MANIFEST.json'}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", default=os.environ.get("LFS_MODEL_DIR", "models"))
    parser.add_argument("--only", action="append", choices=sorted(MODELS), help="fetch only this model (repeatable)")
    parser.add_argument("--precision", choices=("int8", "fp16", "fp32"), default="int8",
                        help="int8 (default, smallest, CPU-friendly), fp16 (CUDA/CoreML), fp32")
    parser.add_argument("--revision", default="main", help="Hugging Face revision / commit to pin")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for name, spec in MODELS.items():
            print(f"{name:28s} {spec['license']:11s} {spec['repo']}")
        return 0
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(model_dir).free
    if free < 2_000_000_000:
        print(f"warning: only {free / 1e9:.1f} GB free in {model_dir}")
    for name in args.only or DEFAULT:
        fetch(name, model_dir, args.precision, args.revision, args.force)
    if "siglip2-base-patch16-224" in (args.only or DEFAULT):
        # Offline post-step: derive the zero-shot aesthetic head from the text encoder.
        import subprocess
        subprocess.run([sys.executable, str(Path(__file__).with_name("build_aesthetic_head.py")),
                        "--model-dir", str(model_dir)], check=False)
    print("Done. Restart Face Hunger; Settings > Models shows the provider in use.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
