"""Discovery and restore helpers for soft-kept pre-conversion originals."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ..deps import db


def _resolve_soft_original(media_id: int, original_path: str | None = None) -> tuple[dict, Path]:
    """Return (media row, path to *.lfs_original on disk)."""
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "Media not found")
    path: Path | None = None
    if original_path:
        cand = Path(original_path)
        if cand.is_file() and cand.name.endswith(".lfs_original"):
            path = cand
    if path is None and row.get("original_path"):
        cand = Path(row["original_path"])
        if cand.is_file():
            path = cand
    if path is None or not path.is_file():
        cur = Path(row["path"]) if row.get("path") else None
        if cur is not None and cur.parent.is_dir():
            stem = cur.stem
            for cand in cur.parent.iterdir():
                if cand.name.endswith(".lfs_original") and (
                    cand.name.startswith(stem + ".") or cand.name.startswith(stem)
                ):
                    if cand.is_file():
                        path = cand
                        break
    if path is None or not path.is_file():
        raise HTTPException(404, "Soft-kept original not found on disk")
    return row, path


def _restored_path_from_soft(soft: Path) -> Path:
    """Map ``file.ext.lfs_original`` or ``file.ext.{id}.lfs_original`` → ``file.ext``."""
    name = soft.name
    if not name.endswith(".lfs_original"):
        raise HTTPException(400, "Not a soft-kept original")
    base = name[: -len(".lfs_original")]
    # Strip optional .{{media_id}} inserted by video_compat
    parts = base.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        # e.g. movie.wmv.42 → movie.wmv  OR  movie.mp4.42 → movie.mp4
        base = parts[0]
    return soft.with_name(base)


def _list_soft_originals() -> list[dict]:
    """All recoverable pre-conversion originals (*.lfs_original)."""
    items: list[dict] = []
    seen: set[str] = set()

    rows = db.all(
        """SELECT id, name, path, original_path, size, kind
           FROM media WHERE original_path IS NOT NULL AND deleted_at IS NULL"""
    )
    for row in rows:
        op = Path(row["original_path"]) if row.get("original_path") else None
        if op is None or not op.is_file():
            continue
        key = str(op.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            st = op.stat()
            size = int(st.st_size)
        except OSError:
            size = 0
        items.append({
            "media_id": row["id"],
            "media_name": row.get("name"),
            "converted_path": row.get("path"),
            "original_path": str(op),
            "original_name": op.name,
            "size": size,
            "kind": row.get("kind") or "video",
        })

    # Also discover disk orphans not linked in DB (heuristic from converted paths)
    for row in db.all(
        "SELECT id, name, path, kind FROM media WHERE kind='video' AND deleted_at IS NULL AND path IS NOT NULL"
    ):
        try:
            cur = Path(row["path"])
            if not cur.parent.is_dir():
                continue
            stem = cur.stem
            for cand in cur.parent.iterdir():
                if not cand.name.endswith(".lfs_original"):
                    continue
                if not (cand.name.startswith(stem + ".") or cand.name.startswith(stem)):
                    continue
                if not cand.is_file():
                    continue
                key = str(cand.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = int(cand.stat().st_size)
                except OSError:
                    size = 0
                items.append({
                    "media_id": row["id"],
                    "media_name": row.get("name"),
                    "converted_path": row.get("path"),
                    "original_path": str(cand),
                    "original_name": cand.name,
                    "size": size,
                    "kind": row.get("kind") or "video",
                })
        except OSError:
            continue

    items.sort(key=lambda x: -int(x.get("size") or 0))
    return items


def _list_converted_backups() -> list[dict]:
    """Discover *.lfs_converted backups left after restoring soft originals."""
    items: list[dict] = []
    seen: set[str] = set()
    # Walk parents of known media paths (videos + any path that might have backups)
    parents: set[str] = set()
    for row in db.all(
        "SELECT id, name, path, kind FROM media WHERE deleted_at IS NULL AND path IS NOT NULL"
    ):
        try:
            p = Path(row["path"])
            if p.parent.is_dir():
                parents.add(str(p.parent.resolve()))
        except OSError:
            continue
    for parent_s in parents:
        parent = Path(parent_s)
        try:
            for cand in parent.iterdir():
                if not cand.is_file():
                    continue
                if ".lfs_converted" not in cand.name:
                    continue
                key = str(cand.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = int(cand.stat().st_size)
                except OSError:
                    size = 0
                # Best-effort link to a media row in the same folder
                media_id = None
                media_name = None
                stem = cand.name.split(".lfs_converted")[0]
                for row in db.all(
                    "SELECT id, name, path FROM media WHERE deleted_at IS NULL AND path IS NOT NULL"
                ):
                    try:
                        rp = Path(row["path"])
                        if rp.parent.resolve() != parent.resolve():
                            continue
                        if rp.name == stem or rp.stem == Path(stem).stem or stem.startswith(rp.stem):
                            media_id = row["id"]
                            media_name = row.get("name")
                            break
                    except OSError:
                        continue
                items.append({
                    "path": str(cand),
                    "name": cand.name,
                    "size": size,
                    "media_id": media_id,
                    "media_name": media_name,
                })
        except OSError:
            continue
    items.sort(key=lambda x: -int(x.get("size") or 0))
    return items

