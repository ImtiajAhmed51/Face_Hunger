"""Canonical-path library traversal without escaping the registered folder."""

import fnmatch
import json
import os
from pathlib import Path

IMAGE = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"})
VIDEO = frozenset({
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts",
    ".wmv", ".mpg", ".mpeg", ".flv", ".asf", ".rm", ".rmvb", ".vob",
    ".3gp", ".ts",
})


def resolve_inside(path, root):
    root = Path(root).expanduser().resolve(strict=True)
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path escapes the registered library: {path}")
    return candidate


def authorized_root(path, roots):
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Library is not a directory")
    if not any(root.is_relative_to(Path(base).expanduser().resolve()) for base in roots):
        raise ValueError("Library is outside the configured authorized roots")
    return root


def scan(library, ignored=None):
    """Yield (canonical Path, 'photo'|'video'); traversal errors abort the scan."""
    if isinstance(library, (str, Path)):
        root = Path(library).expanduser().resolve(strict=True)
    else:
        root = Path(library["path"]).expanduser().resolve(strict=True)
        if ignored is None:
            ignored = library["ignored"]
    if not root.is_dir():
        raise ValueError("Library is not a directory")
    if isinstance(ignored, str):
        ignored = json.loads(ignored)
    patterns = [str(p).replace("\\", "/").strip("/") for p in (ignored or []) if str(p).strip("/")]

    def excluded(path):
        relative = path.relative_to(root).as_posix()
        return any(relative == p or relative.startswith(p.rstrip("/") + "/")
                   or fnmatch.fnmatchcase(relative, p)
                   or ("/" not in p and any(fnmatch.fnmatchcase(part, p) for part in path.relative_to(root).parts))
                   for p in patterns)

    def fail(error):
        raise error

    seen = set()
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink() and not excluded(base / d))
        for name in sorted(files):
            path = base / name
            if excluded(path):
                continue
            # Soft-kept originals / converted backups — do not re-index
            if path.name.endswith(".lfs_original") or ".lfs_converted" in path.name:
                continue
            suffix = path.suffix.lower()
            kind = "photo" if suffix in IMAGE else "video" if suffix in VIDEO else None
            if kind is None:
                continue
            try:
                canonical = resolve_inside(path, root)
            except ValueError:
                continue
            if canonical not in seen and not excluded(canonical):
                seen.add(canonical)
                yield canonical, kind
