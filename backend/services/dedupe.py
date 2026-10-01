"""Duplicate resolution: keep-best suggestions, burst groups, audited resolve with byte-exact undo.

Resolving a group soft-deletes the items not kept (database only). With
``free_space`` the originals of those items are also *moved* (never copied,
re-encoded or deleted) into ``data_dir/duplicate-bin/<audit id>/`` with a
manifest of their SHA-256. That frees exactly their size from the library
folders; the bytes are released for good only when the user empties the bin
from Cleanup. Undo moves every file back to its original path, verifies the
hash, and clears ``deleted_at``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BURST_SECONDS = 2.0
BURST_SIMILARITY = 0.80   # visual cosine (DINOv2) within a burst
BURST_PHASH = 18          # fallback when no visual vectors: perceptual-hash Hamming distance


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DedupeService:
    def __init__(self, services):
        self.s = services

    @property
    def db(self):
        return self.s.db

    def bin_dir(self) -> Path:
        return Path(self.s.config.data_dir) / "duplicate-bin"

    # -- suggestions ---------------------------------------------------------
    def _scores(self, ids: list[int]) -> dict[int, float]:
        if not ids:
            return {}
        return {r["media_id"]: r["score"] for r in self.db.all(
            f"""SELECT media_id, score FROM quality_scores WHERE media_id IN ({','.join('?' * len(ids))})
                AND formula_version = (SELECT MAX(formula_version) FROM quality_scores)""", tuple(ids))}

    def suggest(self, items: list[dict]) -> dict:
        """Keep the best shot; ties broken by pixels, then size, then the earliest copy."""
        ids = [i["id"] for i in items]
        scores = self._scores(ids)

        def rank(item):
            pixels = (item.get("width") or 0) * (item.get("height") or 0)
            return (round(scores.get(item["id"], -1.0), 3), pixels, item.get("size") or 0, -item["id"])

        keep = max(items, key=rank)
        remove = [i["id"] for i in items if i["id"] != keep["id"]]
        savings = int(sum(i.get("size") or 0 for i in items if i["id"] in remove))
        return {"keep": [keep["id"]], "remove": remove, "savings_bytes": savings,
                "scores": {str(k): round(v, 4) for k, v in scores.items()},
                "reason": "best-shot score" if scores else "largest, sharpest copy"}

    def annotate(self, groups: list[dict]) -> list[dict]:
        for group in groups:
            group["suggestion"] = self.suggest(group["items"])
        return groups

    # -- bursts ----------------------------------------------------------------
    def burst_groups(self, *, limit: int = 2000) -> list[dict]:
        """Photos taken within 2 s of each other on the same camera that look alike."""
        from .presenters import _media_row

        rows = self.db.all(
            """SELECT * FROM media WHERE kind='photo' AND deleted_at IS NULL AND missing=0
                 AND captured_at IS NOT NULL AND COALESCE(date_source,'') IN ('exif','filename')
               ORDER BY camera_make, camera_model, captured_at, id""")
        space = self.s.visual_space()
        groups, current = [], []

        def stamp(r):
            return datetime.fromisoformat(r["captured_at"][:19]).timestamp()

        def similar(a, b) -> bool:
            if space is not None:
                va, vb = space.vector(a["id"]), space.vector(b["id"])
                if va is not None and vb is not None:
                    return float(np.dot(va, vb)) >= BURST_SIMILARITY
            from ..duplicates import hamming
            return bool(a["phash"] and b["phash"] and hamming(a["phash"], b["phash"]) <= BURST_PHASH)

        def flush():
            if len(current) >= 2:
                groups.append(current[:])

        prev = None
        for r in rows:
            same_cam = prev is not None and (prev["camera_make"], prev["camera_model"]) == (r["camera_make"], r["camera_model"])
            if prev is not None and same_cam and stamp(r) - stamp(prev) <= BURST_SECONDS and similar(prev, r):
                current.append(r)
            else:
                flush()
                current = [r]
            prev = r
            if len(groups) >= limit:
                break
        flush()
        ignored = {row["group_key"] for row in self.db.all("SELECT group_key FROM ignored_duplicate_groups")}
        out = []
        for members in groups:
            ids = sorted(m["id"] for m in members)
            key = ",".join(map(str, ids))
            if key in ignored:
                continue
            out.append({"type": "burst", "key": f"burst-{ids[0]}", "fingerprint": key, "similarity": None,
                        "items": [_media_row(m) for m in members]})
        return self.annotate(out)

    # -- resolve / undo ------------------------------------------------------------
    def resolve(self, groups: list[dict], *, free_space: bool = False) -> dict:
        """groups: [{"keep": [ids], "remove": [ids]}]. One audit entry for the whole batch."""
        remove = sorted({int(m) for g in groups for m in g.get("remove", [])} - {int(m) for g in groups for m in g.get("keep", [])})
        if not remove:
            raise ValueError("Nothing to remove")
        rows = self.db.all(f"SELECT id, path, size, deleted_at FROM media WHERE id IN ({','.join('?' * len(remove))})",
                           tuple(remove))
        rows = [r for r in rows if r["deleted_at"] is None]
        lib = self.s.library
        moved = []
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            entry = lib._log(conn, "duplicates.resolve", "", {}, len(rows))
            target_dir = self.bin_dir() / str(entry)
            try:
                if free_space:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for r in rows:
                        src = Path(r["path"])
                        if not src.is_file():
                            continue
                        dest = target_dir / f"{r['id']}{src.suffix}"
                        digest = _sha256(src)
                        shutil.move(str(src), str(dest))  # rename on the same volume; copy+remove otherwise
                        moved.append({"id": r["id"], "from": str(src), "to": str(dest), "sha256": digest,
                                      "bytes": dest.stat().st_size})
                    (target_dir / "manifest.json").write_text(json.dumps(moved, indent=1))
                stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                conn.executemany("UPDATE media SET deleted_at=? WHERE id=?", [(stamp, r["id"]) for r in rows])
                freed = sum(m["bytes"] for m in moved)
                summary = (f"Resolved {len(groups)} duplicate group(s): {len(rows)} item(s) to Deleted"
                           + (f", {freed / 1e6:.1f} MB moved to the duplicate bin" if moved else ""))
                conn.execute("UPDATE audit_log SET summary=?, undo=? WHERE id=?",
                             (summary, json.dumps({"rows": [{"id": r["id"], "deleted_at": None} for r in rows], "moved": moved}),
                              entry))
            except BaseException:
                for m in reversed(moved):  # put files back before the transaction rolls back
                    if Path(m["to"]).exists() and not Path(m["from"]).exists():
                        Path(m["from"]).parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(m["to"], m["from"])
                raise
        self.s.cluster.invalidate()
        predicted = int(sum(r["size"] or 0 for r in rows))
        return {"audit_id": entry, "removed": len(rows), "predicted_bytes": predicted,
                "moved_bytes": sum(m["bytes"] for m in moved), "moved": len(moved)}

    def undo_moves(self, moved: list[dict]) -> None:
        """Move binned originals back; refuses to overwrite anything now at the original path."""
        for m in moved:
            src, dest = Path(m["to"]), Path(m["from"])
            if dest.exists():
                if dest.is_file() and _sha256(dest) == m["sha256"]:
                    continue  # already back
                raise FileExistsError(f"{dest} exists; refusing to overwrite it")
            if not src.is_file():
                raise FileNotFoundError(f"{src} is no longer in the duplicate bin")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            if _sha256(dest) != m["sha256"]:
                raise ValueError(f"{dest} does not match its recorded hash")
        folders = {Path(m["to"]).parent for m in moved}
        for folder in folders:
            remaining = [p for p in folder.iterdir() if p.name != "manifest.json"] if folder.exists() else []
            if not remaining:
                shutil.rmtree(folder, ignore_errors=True)

    def bin_usage(self) -> dict:
        folder = self.bin_dir()
        size = files = 0
        if folder.exists():
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    if name != "manifest.json":
                        size += os.path.getsize(os.path.join(root, name))
                        files += 1
        return {"bytes": size, "files": files}

    def empty_bin(self) -> dict:
        """Permanently release the space (the only place duplicate resolution deletes files).
        Binned items are marked purged in the audit log so they can no longer be undone."""
        usage = self.bin_usage()
        folder = self.bin_dir()
        ids = []
        if folder.exists():
            for sub in folder.iterdir():
                if sub.is_dir() and sub.name.isdigit():
                    ids.append(int(sub.name))
            shutil.rmtree(folder)
        if ids:
            with self.db.connect() as conn:
                conn.executemany("UPDATE audit_log SET undone_at=COALESCE(undone_at, 'purged') WHERE id=? AND action='duplicates.resolve'",
                                 [(i,) for i in ids])
        return {**usage, "groups": len(ids)}


def undo_hook(service: DedupeService, conn, data: dict) -> None:
    service.undo_moves(data.get("moved", []))
    conn.executemany("UPDATE media SET deleted_at=NULL WHERE id=?", [(r["id"],) for r in data["rows"]])

