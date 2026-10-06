"""Edits in the database + sidecars, with history, revert and external-change reconciliation.

Where things live
  media_edits        current edit per media item (the source the UI and exports read)
  edit_history       every change with before/after, so any step can be reverted
  <original>.xmp     XMP sidecar next to the original (or data_dir/sidecars/<id>.xmp when the
                     folder is read-only); an existing ``<stem>.xmp`` from another tool is reused
  data_dir/sidecars/<id>.json   Face Hunger-specific copy: edit, flag, history, original hash

The original file is only ever read. Re-indexing never touches these tables. When a sidecar
changes on disk and its hash differs from what we last wrote, the external values are imported
(external wins: it is the newer statement of intent) and recorded in the history.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from .. import edits as model

FIELDS = ("rotation", "flip_h", "flip_v", "crop", "rating", "label", "flag")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fh-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class EditService:
    def __init__(self, services):
        self.s = services

    @property
    def db(self):
        return self.s.db

    # -- reading -----------------------------------------------------------------
    @staticmethod
    def _row_to_edit(row: Optional[dict]) -> dict:
        if not row:
            return dict(model.EMPTY)
        return {"rotation": row["rotation"], "flip_h": bool(row["flip_h"]), "flip_v": bool(row["flip_v"]),
                "crop": json.loads(row["crop"]) if row["crop"] else None, "rating": row["rating"],
                "label": row["label"], "flag": row["flag"]}

    def get(self, media_id: int) -> dict:
        return self._row_to_edit(self.db.one("SELECT * FROM media_edits WHERE media_id=?", (media_id,)))

    def version(self, media_id: int) -> int:
        row = self.db.one("SELECT version FROM media_edits WHERE media_id=?", (media_id,))
        return int(row["version"]) if row else 0

    def history(self, media_id: int, limit: int = 100) -> list[dict]:
        rows = self.db.all("SELECT id, at, source, summary, before, after FROM edit_history WHERE media_id=? "
                           "ORDER BY id DESC LIMIT ?", (media_id, limit))
        return [{**r, "before": json.loads(r["before"]), "after": json.loads(r["after"])} for r in rows]

    def detail(self, media_id: int) -> dict:
        row = self.db.one("SELECT * FROM media_edits WHERE media_id=?", (media_id,))
        edit = self._row_to_edit(row)
        return {"edit": edit, "version": int(row["version"]) if row else 0, "edited": edit != model.EMPTY,
                "sidecar": row["sidecar_path"] if row else None, "history": self.history(media_id),
                "aspects": list(model.ASPECTS)}

    # -- sidecars ------------------------------------------------------------------
    def sidecar_candidates(self, original: Path) -> list[Path]:
        return [original.with_name(original.name + ".xmp"), original.with_suffix(".xmp")]

    def _sidecar_target(self, media_id: int, original: Path, current: Optional[str]) -> Path:
        if current and Path(current).exists():
            return Path(current)
        for candidate in self.sidecar_candidates(original):
            if candidate.is_file():
                return candidate  # written by another application: update it in place
        beside = original.with_name(original.name + ".xmp")
        if original.parent.is_dir() and os.access(original.parent, os.W_OK):
            return beside
        return self.s.config.data_dir / "sidecars" / f"{media_id}.xmp"

    def _write_sidecars(self, conn, media_id: int, edit: dict) -> None:
        media = conn.execute("SELECT path, content_hash FROM media WHERE id=?", (media_id,)).fetchone()
        row = conn.execute("SELECT sidecar_path FROM media_edits WHERE media_id=?", (media_id,)).fetchone()
        target = self._sidecar_target(media_id, Path(media["path"]), row["sidecar_path"] if row else None)
        if edit == model.EMPTY and not target.exists():
            sha, path = None, None  # nothing to say and nothing on disk: do not create an empty sidecar
        else:
            existing = target.read_bytes() if target.is_file() else None
            data = model.write_xmp(edit, existing)
            try:
                _atomic_write(target, data)
            except OSError:
                target = self.s.config.data_dir / "sidecars" / f"{media_id}.xmp"
                _atomic_write(target, data)
            sha, path = model.sha256(data), str(target)
        conn.execute("UPDATE media_edits SET sidecar_path=?, sidecar_sha=? WHERE media_id=?", (path, sha, media_id))
        history = [dict(r) for r in conn.execute(
            "SELECT at, source, summary FROM edit_history WHERE media_id=? ORDER BY id DESC LIMIT 50", (media_id,))]
        _atomic_write(self.s.config.data_dir / "sidecars" / f"{media_id}.json", json.dumps(
            {"format": "face-hunger-sidecar", "version": 1, "media_id": media_id, "original": media["path"],
             "original_content_hash": media["content_hash"], "edit": edit, "history": history}, indent=1).encode())

    # -- writing -----------------------------------------------------------------------
    @staticmethod
    def _summary(before: dict, after: dict) -> str:
        parts = []
        if before["rotation"] != after["rotation"]:
            parts.append(f"rotate to {after['rotation']}°")
        if (before["flip_h"], before["flip_v"]) != (after["flip_h"], after["flip_v"]):
            parts.append("flip")
        if before["crop"] != after["crop"]:
            parts.append("crop" if after["crop"] else "remove crop")
        if before["rating"] != after["rating"]:
            parts.append(f"rating {after['rating']}")
        if before["label"] != after["label"]:
            parts.append(f"label {after['label'] or 'none'}")
        if before["flag"] != after["flag"]:
            parts.append(f"flag {after['flag'] or 'none'}")
        return ", ".join(parts) or "no change"

    def _store(self, conn, media_id: int, after: dict, source: str, summary: Optional[str] = None,
               write_sidecar: bool = True) -> Optional[int]:
        before = self._row_to_edit(conn.execute("SELECT * FROM media_edits WHERE media_id=?", (media_id,)).fetchone())
        if before == after:
            return None
        conn.execute(
            """INSERT INTO media_edits(media_id, rotation, flip_h, flip_v, crop, rating, label, flag, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?,1,CURRENT_TIMESTAMP)
               ON CONFLICT(media_id) DO UPDATE SET rotation=excluded.rotation, flip_h=excluded.flip_h, flip_v=excluded.flip_v,
                 crop=excluded.crop, rating=excluded.rating, label=excluded.label, flag=excluded.flag,
                 version=media_edits.version + 1, updated_at=CURRENT_TIMESTAMP""",
            (media_id, after["rotation"], int(after["flip_h"]), int(after["flip_v"]),
             json.dumps(after["crop"]) if after["crop"] else None, after["rating"], after["label"], after["flag"]))
        entry = conn.execute("INSERT INTO edit_history(media_id, source, summary, before, after) VALUES (?,?,?,?,?)",
                             (media_id, source, summary or self._summary(before, after), json.dumps(before),
                              json.dumps(after))).lastrowid
        if write_sidecar:
            self._write_sidecars(conn, media_id, after)
        return entry

    def update(self, media_id: int, patch: dict, *, source: str = "user") -> dict:
        """Apply a partial change (only the keys present in ``patch``)."""
        unknown = set(patch) - set(FIELDS)
        if unknown:
            raise model.EditError(f"Unknown edit fields: {', '.join(sorted(unknown))}")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone():
                raise KeyError("Media not found")
            current = self._row_to_edit(conn.execute("SELECT * FROM media_edits WHERE media_id=?", (media_id,)).fetchone())
            after = model.normalize({**current, **patch})
            self._store(conn, media_id, after, source)
        return self.detail(media_id)

    def revert(self, media_id: int, history_id: Optional[int] = None) -> dict:
        """Back to the original view, or to the state *before* a given history entry."""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if history_id is None:
                target, summary = dict(model.EMPTY), "revert to original"
            else:
                row = conn.execute("SELECT before, summary FROM edit_history WHERE id=? AND media_id=?",
                                   (history_id, media_id)).fetchone()
                if row is None:
                    raise KeyError("History entry not found")
                target, summary = model.normalize(json.loads(row["before"])), f"undo: {row['summary']}"
            self._store(conn, media_id, target, "revert", summary)
        return self.detail(media_id)

    def batch(self, media_ids: list[int], patch: dict) -> dict:
        """Rating / label / flag for many items; one audit entry that restores every previous value."""
        allowed = {k: v for k, v in patch.items() if k in ("rating", "label", "flag")}
        if not allowed:
            raise model.EditError("Batch edits support rating, label and flag")
        ids = list(dict.fromkeys(int(m) for m in media_ids))
        previous = []
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for media_id in ids:
                if not conn.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone():
                    continue
                current = self._row_to_edit(conn.execute("SELECT * FROM media_edits WHERE media_id=?", (media_id,)).fetchone())
                after = model.normalize({**current, **allowed})
                if self._store(conn, media_id, after, "user") is not None:
                    previous.append({"media_id": media_id, **{k: current[k] for k in allowed}})
            label = ", ".join(f"{k} {v if v not in (None, 0) else 'cleared'}" for k, v in allowed.items())
            entry = self.s.library._log(conn, "edits.batch", f"Set {label} on {len(previous)} item(s)",
                                        {"previous": previous}, len(previous))
        return {"changed": len(previous), "audit_id": entry}

    def undo_batch(self, conn, data: dict) -> None:
        for item in data["previous"]:
            media_id = item["media_id"]
            current = self._row_to_edit(conn.execute("SELECT * FROM media_edits WHERE media_id=?", (media_id,)).fetchone())
            self._store(conn, media_id, model.normalize({**current, **{k: v for k, v in item.items() if k != "media_id"}}),
                        "revert", "undo batch edit")

    # -- reconciliation -----------------------------------------------------------------
    def reconcile_media(self, media_id: int) -> Optional[dict]:
        """Import a sidecar that changed outside the app. Returns the new edit, or None if unchanged."""
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            media = conn.execute("SELECT path FROM media WHERE id=?", (media_id,)).fetchone()
            if media is None:
                return None
            row = conn.execute("SELECT * FROM media_edits WHERE media_id=?", (media_id,)).fetchone()
            known = Path(row["sidecar_path"]) if row and row["sidecar_path"] else None
            candidates = ([known] if known else []) + self.sidecar_candidates(Path(media["path"]))
            sidecar = next((c for c in candidates if c.is_file()), None)
            if sidecar is None:
                return None
            data = sidecar.read_bytes()
            digest = model.sha256(data)
            if row and row["sidecar_sha"] == digest:
                return None  # exactly what we wrote
            try:
                external = model.parse_xmp(data)
            except model.EditError:
                return None  # unreadable sidecar: leave both it and our edit alone
            current = self._row_to_edit(row)
            self._store(conn, media_id, external, "external", f"sidecar changed on disk: {self._summary(current, external)}",
                        write_sidecar=False)
            conn.execute("INSERT INTO media_edits(media_id, sidecar_path, sidecar_sha) VALUES (?,?,?) "
                         "ON CONFLICT(media_id) DO UPDATE SET sidecar_path=excluded.sidecar_path, sidecar_sha=excluded.sidecar_sha",
                         (media_id, str(sidecar), digest))
            return external

    def reconcile_path(self, sidecar: Path) -> Optional[int]:
        """Find the media item a sidecar belongs to and reconcile it; returns the media id if it changed."""
        sidecar = Path(sidecar)
        row = self.db.one("SELECT media_id FROM media_edits WHERE sidecar_path=?", (str(sidecar),))
        media_id = row["media_id"] if row else None
        if media_id is None:
            stem = sidecar.with_suffix("")  # IMG.jpg.xmp -> IMG.jpg ; IMG.xmp -> IMG
            hit = self.db.one("SELECT id FROM media WHERE path=?", (str(stem),))
            if hit is None:
                hit = self.db.one("SELECT id FROM media WHERE path LIKE ? ESCAPE '\\' ORDER BY id LIMIT 1",
                                  (str(stem).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%",))
            media_id = hit["id"] if hit else None
        if media_id is None:
            return None
        return media_id if self.reconcile_media(media_id) is not None else None

    # -- rendering -------------------------------------------------------------------------
    def rendered(self, media_id: int, *, max_side: Optional[int] = 2560):
        """Upright image with the current edit applied (PIL). Reads the original, never writes it."""
        from .. import imaging

        row = self.db.one("SELECT path, kind FROM media WHERE id=?", (media_id,))
        if row is None or row["kind"] != "photo":
            raise KeyError("Photo not found")
        image = imaging.open_image(Path(row["path"]), max_side=max_side)
        return model.apply(image, self.get(media_id))
