"""Storage dashboard (what takes space, what looks like clutter) and the model-upgrade flow.

Clutter categories are suggestions computed from data the app already has:
  large        the biggest files
  duplicates   extra copies in exact/near-duplicate groups (keep-best from the resolver)
  old_video    videos older than two years that are low resolution or score poorly
  screenshots  named like a screenshot, or a PNG with no camera and a phone/desktop screen shape
  blurry       sharpness signal below 0.25
  dark         exposure signal below 0.2 (near-black frames, lens-cap shots)

An item can be in several categories; totals count each file once. Estimates use the file
sizes on disk when the file is reachable (falling back to the indexed size), so the estimate
matches what a cleanup actually frees. Cleanup is the same audited, undoable move-to-bin as
duplicate resolution: nothing is deleted until the bin is emptied.

Model upgrade: a new embedding space is built next to the current one. While it builds the
current model stays pinned as active; the user can compare both, switch, or roll back. Neither
action deletes vectors, so going back is instant and lossless.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Optional

SCREENSHOT_NAME = re.compile(r"(screen[ _-]?shot|screenshot|screen[ _-]?capture|screen recording|^capture[ _-]?\d|^snip)", re.I)
SCREEN_SHAPES = {(1170, 2532), (1179, 2556), (1284, 2778), (1290, 2796), (1125, 2436), (1080, 1920), (1080, 2340), (1080, 2400),
                 (1440, 2560), (1440, 3200), (1920, 1080), (2560, 1440), (2880, 1800), (3024, 1964), (3456, 2234), (1366, 768),
                 (2560, 1600), (3840, 2160), (750, 1334), (828, 1792), (1242, 2688)}
CATEGORIES = ("large", "duplicates", "old_video", "screenshots", "blurry", "dark")


class StorageService:
    def __init__(self, services):
        self.s = services

    @property
    def db(self):
        return self.s.db

    # ------------------------------------------------------------------ dashboard
    @staticmethod
    def _disk_size(row: dict) -> int:
        try:
            return os.stat(row["path"]).st_size
        except OSError:
            return int(row["size"] or 0)

    def _item(self, row: dict, reason: str) -> dict:
        return {"id": row["id"], "name": row["name"], "kind": row["kind"], "size": self._disk_size(row),
                "captured_at": row["captured_at"], "width": row["width"], "height": row["height"], "reason": reason}

    def candidates(self, category: str, limit: int = 500) -> list[dict]:
        base = "FROM media m WHERE m.deleted_at IS NULL AND m.missing = 0"
        if category == "large":
            rows = self.db.all(f"SELECT m.* {base} ORDER BY m.size DESC LIMIT ?", (min(limit, 200),))
            return [self._item(r, "large file") for r in rows]
        if category == "duplicates":
            from .. import dino_duplicates as dup
            from .presenters import _media_row

            threshold = float(self.db.settings().get("dino_similarity_threshold", 0.92))
            groups = dup.find_duplicate_groups(self.db, self.s.visual_space(), similarity_threshold=threshold, limit=2000,
                                               media_row_fn=_media_row)
            remove = [m for g in self.s.dedupe.annotate(groups) for m in g["suggestion"]["remove"]][:limit]
            if not remove:
                return []
            rows = self.db.all(f"SELECT * FROM media WHERE id IN ({','.join('?' * len(remove))})", tuple(remove))
            return [self._item(r, "extra copy") for r in rows]
        if category == "old_video":
            cutoff = (datetime.now() - timedelta(days=730)).isoformat()
            rows = self.db.all(
                """SELECT m.*, q.score FROM media m LEFT JOIN quality_scores q ON q.media_id = m.id
                      AND q.formula_version = (SELECT MAX(formula_version) FROM quality_scores)
                    WHERE m.deleted_at IS NULL AND m.missing = 0 AND m.kind = 'video'
                      AND COALESCE(m.captured_at, m.indexed_at) < ?
                      AND (COALESCE(m.height, 0) < 720 OR COALESCE(q.score, 1) < 0.4)
                    ORDER BY m.size DESC LIMIT ?""", (cutoff, limit))
            return [self._item(r, "old, low-resolution video" if (r["height"] or 0) < 720 else "old, low-quality video") for r in rows]
        if category == "screenshots":
            rows = self.db.all(f"""SELECT m.* {base} AND m.kind = 'photo' AND m.camera_make IS NULL AND m.camera_model IS NULL
                                   AND (lower(m.name) LIKE '%screen%' OR lower(m.name) LIKE '%capture%' OR lower(m.name) LIKE 'snip%'
                                        OR lower(m.name) LIKE '%.png')
                                   ORDER BY m.size DESC LIMIT ?""", (limit * 4,))
            out = []
            for r in rows:
                named = bool(SCREENSHOT_NAME.search(r["name"]))
                shape = (r["width"], r["height"]) in SCREEN_SHAPES or (r["height"], r["width"]) in SCREEN_SHAPES
                if named or (r["name"].lower().endswith(".png") and shape):
                    out.append(self._item(r, "screenshot"))
            return out[:limit]
        if category in ("blurry", "dark"):
            column, threshold, reason = ("sharpness", 0.25, "blurry") if category == "blurry" else ("exposure", 0.2, "very dark")
            rows = self.db.all(f"""SELECT m.* FROM media m JOIN quality_signals q ON q.media_id = m.id
                                   WHERE m.deleted_at IS NULL AND m.missing = 0 AND m.kind = 'photo' AND q.error IS NULL
                                     AND q.{column} < ? ORDER BY q.{column} ASC LIMIT ?""", (threshold, limit))
            return [self._item(r, reason) for r in rows]
        raise KeyError(f"Unknown category {category}")

    def dashboard(self) -> dict:
        totals = self.db.one("""SELECT COUNT(*) AS items, COALESCE(SUM(size), 0) AS bytes,
                                       COALESCE(SUM(CASE WHEN kind='video' THEN size END), 0) AS video_bytes
                                FROM media WHERE deleted_at IS NULL AND missing = 0""")
        categories = []
        clutter: dict[int, int] = {}
        for name in CATEGORIES:
            items = self.candidates(name)
            if name != "large":  # a big file is not clutter by itself
                clutter.update({i["id"]: i["size"] for i in items})
            categories.append({"category": name, "count": len(items), "bytes": sum(i["size"] for i in items),
                               "preview_ids": [i["id"] for i in items[:6]]})
        return {"library": totals, "categories": categories,
                "clutter": {"count": len(clutter), "bytes": sum(clutter.values())},
                "bin": self.s.dedupe.bin_usage()}

    def estimate(self, media_ids: list[int]) -> dict:
        ids = list(dict.fromkeys(int(m) for m in media_ids))
        if not ids:
            return {"count": 0, "bytes": 0}
        rows = self.db.all(f"SELECT id, path, size FROM media WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(ids))})", tuple(ids))
        return {"count": len(rows), "bytes": sum(self._disk_size(r) for r in rows)}

    def cleanup(self, media_ids: list[int], *, free_space: bool = True) -> dict:
        estimate = self.estimate(media_ids)
        result = self.s.dedupe.resolve([{"keep": [], "remove": media_ids}], free_space=free_space, label="Storage cleanup")
        return {**result, "estimated_bytes": estimate["bytes"]}

    # ------------------------------------------------------------------ model upgrade
    def _state(self) -> Optional[dict]:
        return self.db.settings().get("model_upgrade")

    def _save(self, key: str, value) -> None:
        import json

        with self.db.connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(value)))

    def _pin(self, role: str, key: Optional[str]) -> None:
        pins = dict(self.db.settings().get("active_models") or {})
        if key is None:
            pins.pop(role, None)
        else:
            pins[role] = key
        self._save("active_models", pins)

    def upgrade_options(self) -> list[dict]:
        """Installed (or injected) models that can embed, per role, with coverage and which one is active."""
        out = []
        for space in self.s.embedding_spaces_for_backfill():
            active = self.s.vectors.active(space.spec.role)
            out.append({"key": space.key, "role": space.spec.role, "dim": space.spec.dim, "coverage": space.coverage(max_age=0),
                        "active": active is not None and active.key == space.key})
        for row in self.s.vectors.rows():
            if row["subject"] == "media" and row["key"] not in {o["key"] for o in out}:
                space = self.s.vectors.get(row["key"])
                active = self.s.vectors.active(row["role"])
                out.append({"key": row["key"], "role": row["role"], "dim": row["dim"], "coverage": space.coverage(max_age=0),
                            "active": active is not None and active.key == row["key"], "can_embed": False})
        return out

    def upgrade_status(self) -> dict:
        state = self._state()
        if not state:
            return {"upgrade": None, "options": self.upgrade_options()}
        target = self.s.vectors.get(state["to_key"])
        coverage = target.coverage(max_age=0)
        ready = bool(coverage["total"]) and coverage["ratio"] >= 0.95
        if state["status"] == "building" and ready:
            state = {**state, "status": "ready"}
            self._save("model_upgrade", state)
        active = self.s.vectors.active(state["role"])
        return {"upgrade": {**state, "coverage": coverage, "ready": ready, "active_key": active.key if active else None},
                "options": self.upgrade_options()}

    def start_upgrade(self, to_key: str) -> dict:
        from ..jobs.manager import PRIORITY

        current = self._state()
        if current and current["status"] in ("building", "ready") and current["to_key"] != to_key:
            raise ValueError("Another model upgrade is in progress; switch or roll it back first")
        if self.s.embedder_for(to_key) is None:
            raise ValueError("That model is not installed, so it cannot embed your library")
        target = self.s.vectors.get(to_key)
        role = target.spec.role
        active = self.s.vectors.active(role)
        from_key = active.key if active is not None and active.key != to_key else (current or {}).get("from_key")
        if from_key:
            self._pin(role, from_key)  # keep searching with the current model while the new one builds
        state = {"role": role, "from_key": from_key, "to_key": to_key, "status": "building",
                 "started_at": datetime.now().isoformat(timespec="seconds")}
        self._save("model_upgrade", state)
        self.s.jobs.enqueue("embed_backfill", {"key": to_key}, priority=PRIORITY["normal"], dedupe_key=f"embed_backfill:{to_key}")
        return self.upgrade_status()

    def resume_upgrade(self) -> None:
        """Called at startup: an upgrade interrupted by a crash continues where it stopped."""
        from ..jobs.manager import PRIORITY

        state = self._state()
        if state and state["status"] == "building" and self.s.embedder_for(state["to_key"]) is not None:
            if self.s.vectors.get(state["to_key"]).pending(1):
                self.s.jobs.enqueue("embed_backfill", {"key": state["to_key"]}, priority=PRIORITY["normal"],
                                    dedupe_key=f"embed_backfill:{state['to_key']}")

    def switch(self, *, force: bool = False) -> dict:
        state = self._state()
        if not state:
            raise ValueError("No model upgrade in progress")
        coverage = self.s.vectors.get(state["to_key"]).coverage(max_age=0)
        if not force and (not coverage["total"] or coverage["ratio"] < 0.95):
            raise ValueError(f"The new index covers {coverage['filled']} of {coverage['total']} items; wait for it to finish")
        self._pin(state["role"], state["to_key"])
        self._save("model_upgrade", {**state, "status": "switched", "switched_at": datetime.now().isoformat(timespec="seconds")})
        return self.upgrade_status()

    def rollback(self) -> dict:
        state = self._state()
        if not state:
            raise ValueError("No model upgrade to roll back")
        if not state.get("from_key"):
            raise ValueError("There was no previous model to go back to")
        self._pin(state["role"], state["from_key"])  # the new store stays on disk; nothing is deleted
        self._save("model_upgrade", {**state, "status": "rolled_back"})
        return self.upgrade_status()

    def finish(self) -> dict:
        """Close the flow, keeping whichever model is active. Both stores stay on disk."""
        self._save("model_upgrade", None)
        return self.upgrade_status()

    def compare(self, *, similar_media_id: Optional[int] = None, text: Optional[str] = None, limit: int = 12) -> dict:
        """The same query against the old and the new index, side by side."""
        state = self._state()
        if not state or not state.get("from_key"):
            raise ValueError("No model upgrade with two indexes to compare")
        sides = {}
        for label, key in (("current", state["from_key"]), ("candidate", state["to_key"])):
            space = self.s.vectors.get(key)
            if similar_media_id is not None:
                result = self.s.search.run({"similar_media_id": similar_media_id, "limit": limit,
                                            "space_overrides": {"visual": key}})
                items = result["items"]
            else:
                encoder = self.s.embedder_for(key)
                embed_text = getattr(self.s.models.text_encoder(), "embed_texts", None) if space.spec.role == "text_image" else None
                if not text or embed_text is None or encoder is None:
                    raise ValueError("Text comparison needs a text model for this role; compare with a photo instead")
                space.ensure_ready()
                keys, sims = space.ann.search(embed_text([text])[0], limit)
                from .search import serialize_media

                items = serialize_media(self.db, [int(k) for k in keys])
            sides[label] = {"key": key, "ids": [i["id"] for i in items], "items": items}
        a, b = set(sides["current"]["ids"]), set(sides["candidate"]["ids"])
        return {**sides, "overlap": len(a & b), "limit": limit}

