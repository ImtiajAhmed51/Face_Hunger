"""Albums, favorites, smart collections, people merge/split and the audit log with undo.

Every batch action writes one ``audit_log`` row holding exactly what is needed
to reverse it. ``undo(entry_id)`` replays that inverse inside one transaction
and marks the row undone; undoing twice is refused. Actions are idempotent
where it matters (adding an item already in an album records nothing for it),
so an undo never removes something the action did not add.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Iterable, Optional

import numpy as np

from ..ops.logging import request_id


def _now() -> str:
    """Same format as presenters._now (importing it here would be circular)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class UndoError(ValueError):
    pass


def _ph(values) -> str:
    return ",".join("?" * len(values))


class LibraryService:
    def __init__(self, services):
        self.s = services

    @property
    def db(self):
        return self.s.db

    # -- audit ---------------------------------------------------------------
    def _log(self, conn, action: str, summary: str, undo: dict, count: int = 0) -> int:
        return conn.execute(
            "INSERT INTO audit_log(action, summary, undo, item_count, request_id) VALUES (?,?,?,?,?)",
            (action, summary, json.dumps(undo), count, request_id.get())).lastrowid

    def audit(self, *, limit: int = 100, before: Optional[int] = None) -> list[dict]:
        rows = self.db.all(
            "SELECT id, at, action, summary, item_count, undone_at, request_id FROM audit_log "
            + ("WHERE id < ? " if before else "") + "ORDER BY id DESC LIMIT ?",
            ((before, limit) if before else (limit,)))
        return [{**r, "undoable": r["undone_at"] is None} for r in rows]

    def undo(self, entry_id: int) -> dict:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM audit_log WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                raise KeyError("Audit entry not found")
            if row["undone_at"] == "purged":
                raise UndoError("The duplicate bin was emptied; these files are gone")
            if row["undone_at"] is not None:
                raise UndoError("Already undone")
            data = json.loads(row["undo"])
            handler = getattr(self, f"_undo_{row['action'].replace('.', '_')}", None)
            if handler is None:
                raise UndoError(f"{row['action']} cannot be undone")
            handler(conn, data)
            conn.execute("UPDATE audit_log SET undone_at=CURRENT_TIMESTAMP WHERE id=?", (entry_id,))
            self._log(conn, "undo", f"Undid: {row['summary']}", {"of": entry_id}, row["item_count"])
        self.s.cluster.invalidate()
        return {"ok": True, "undone": entry_id, "summary": row["summary"]}

    # -- albums -----------------------------------------------------------------
    def create_album(self, name: str, media_ids: Iterable[int] = ()) -> dict:
        with self.db.connect() as conn:
            album_id = conn.execute("INSERT INTO albums(name) VALUES (?)", (name,)).lastrowid
            added = self._add(conn, album_id, list(dict.fromkeys(media_ids)))
            entry = self._log(conn, "album.create", f"Created album “{name}”", {"album_id": album_id}, len(added))
        return {"album_id": album_id, "added": len(added), "audit_id": entry}

    def _add(self, conn, album_id: int, media_ids: list[int]) -> list[int]:
        if not media_ids:
            return []
        present = {r[0] for r in conn.execute(
            f"SELECT media_id FROM album_media WHERE album_id=? AND media_id IN ({_ph(media_ids)})", (album_id, *media_ids))}
        existing = {r[0] for r in conn.execute(f"SELECT id FROM media WHERE id IN ({_ph(media_ids)})", tuple(media_ids))}
        new = [m for m in media_ids if m not in present and m in existing]
        start = conn.execute("SELECT COALESCE(MAX(position), 0) FROM album_media WHERE album_id=?", (album_id,)).fetchone()[0]
        conn.executemany("INSERT INTO album_media(album_id, media_id, position) VALUES (?,?,?)",
                         [(album_id, m, start + i + 1) for i, m in enumerate(new)])
        conn.execute("UPDATE albums SET updated_at=CURRENT_TIMESTAMP, cover_media_id=COALESCE(cover_media_id, ?) WHERE id=?",
                     (new[0] if new else None, album_id))
        return new

    def add_to_album(self, album_id: int, media_ids: list[int]) -> dict:
        with self.db.connect() as conn:
            album = conn.execute("SELECT name FROM albums WHERE id=?", (album_id,)).fetchone()
            if album is None:
                raise KeyError("Album not found")
            added = self._add(conn, album_id, list(dict.fromkeys(media_ids)))
            entry = self._log(conn, "album.add", f"Added {len(added)} item(s) to “{album['name']}”",
                              {"album_id": album_id, "media_ids": added}, len(added))
        return {"added": len(added), "audit_id": entry}

    def remove_from_album(self, album_id: int, media_ids: list[int]) -> dict:
        with self.db.connect() as conn:
            album = conn.execute("SELECT name, cover_media_id FROM albums WHERE id=?", (album_id,)).fetchone()
            if album is None:
                raise KeyError("Album not found")
            rows = [dict(r) for r in conn.execute(
                f"SELECT media_id, position, added_at FROM album_media WHERE album_id=? AND media_id IN ({_ph(media_ids)})",
                (album_id, *media_ids))] if media_ids else []
            conn.execute(f"DELETE FROM album_media WHERE album_id=? AND media_id IN ({_ph(media_ids)})", (album_id, *media_ids))
            if album["cover_media_id"] in {r["media_id"] for r in rows}:
                conn.execute("UPDATE albums SET cover_media_id=(SELECT media_id FROM album_media WHERE album_id=? "
                             "ORDER BY position LIMIT 1) WHERE id=?", (album_id, album_id))
            entry = self._log(conn, "album.remove", f"Removed {len(rows)} item(s) from “{album['name']}”",
                              {"album_id": album_id, "rows": rows, "cover": album["cover_media_id"]}, len(rows))
        return {"removed": len(rows), "audit_id": entry}

    def rename_album(self, album_id: int, name: str) -> dict:
        with self.db.connect() as conn:
            album = conn.execute("SELECT name FROM albums WHERE id=?", (album_id,)).fetchone()
            if album is None:
                raise KeyError("Album not found")
            conn.execute("UPDATE albums SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (name, album_id))
            entry = self._log(conn, "album.rename", f"Renamed album “{album['name']}” to “{name}”",
                              {"album_id": album_id, "name": album["name"]})
        return {"audit_id": entry}

    def delete_album(self, album_id: int) -> dict:
        with self.db.connect() as conn:
            album = conn.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
            if album is None:
                raise KeyError("Album not found")
            rows = [dict(r) for r in conn.execute("SELECT media_id, position, added_at FROM album_media WHERE album_id=?",
                                                  (album_id,))]
            conn.execute("DELETE FROM albums WHERE id=?", (album_id,))  # cascades album_media
            entry = self._log(conn, "album.delete", f"Deleted album “{album['name']}” (photos untouched)",
                              {"album": dict(album), "rows": rows}, len(rows))
        return {"audit_id": entry}

    def _undo_album_create(self, conn, d):
        conn.execute("DELETE FROM albums WHERE id=?", (d["album_id"],))

    def _undo_album_add(self, conn, d):
        if d["media_ids"]:
            conn.execute(f"DELETE FROM album_media WHERE album_id=? AND media_id IN ({_ph(d['media_ids'])})",
                         (d["album_id"], *d["media_ids"]))

    def _undo_album_remove(self, conn, d):
        conn.executemany("INSERT OR IGNORE INTO album_media(album_id, media_id, position, added_at) VALUES (?,?,?,?)",
                         [(d["album_id"], r["media_id"], r["position"], r["added_at"]) for r in d["rows"]])
        conn.execute("UPDATE albums SET cover_media_id=? WHERE id=?", (d["cover"], d["album_id"]))

    def _undo_album_rename(self, conn, d):
        conn.execute("UPDATE albums SET name=? WHERE id=?", (d["name"], d["album_id"]))

    def _undo_album_delete(self, conn, d):
        album = d["album"]
        cols = list(album)
        conn.execute(f"INSERT INTO albums({','.join(cols)}) VALUES ({_ph(cols)})", tuple(album[c] for c in cols))
        self._undo_album_remove(conn, {"album_id": album["id"], "rows": d["rows"], "cover": album["cover_media_id"]})

    def albums(self) -> list[dict]:
        return self.db.all("""SELECT a.*, (SELECT COUNT(*) FROM album_media am JOIN media m ON m.id=am.media_id
                                         WHERE am.album_id=a.id AND m.deleted_at IS NULL) AS item_count
                              FROM albums a ORDER BY a.updated_at DESC, a.id DESC""")

    # -- favorites -------------------------------------------------------------------
    def set_favorite(self, media_ids: list[int], favorite: bool) -> dict:
        ids = list(dict.fromkeys(media_ids))
        with self.db.connect() as conn:
            current = {r[0] for r in conn.execute(f"SELECT media_id FROM favorites WHERE media_id IN ({_ph(ids)})", tuple(ids))} if ids else set()
            changed = [m for m in ids if (m in current) != favorite]
            if favorite:
                conn.executemany("INSERT OR IGNORE INTO favorites(media_id) SELECT id FROM media WHERE id=?", [(m,) for m in changed])
            elif changed:
                conn.execute(f"DELETE FROM favorites WHERE media_id IN ({_ph(changed)})", tuple(changed))
            verb = "Favorited" if favorite else "Unfavorited"
            entry = self._log(conn, "favorite.set", f"{verb} {len(changed)} item(s)",
                              {"media_ids": changed, "favorite": favorite}, len(changed))
        return {"changed": len(changed), "audit_id": entry}

    def _undo_favorite_set(self, conn, d):
        if not d["media_ids"]:
            return
        if d["favorite"]:
            conn.execute(f"DELETE FROM favorites WHERE media_id IN ({_ph(d['media_ids'])})", tuple(d["media_ids"]))
        else:
            conn.executemany("INSERT OR IGNORE INTO favorites(media_id) VALUES (?)", [(m,) for m in d["media_ids"]])

    # -- media batch delete / restore ----------------------------------------------------
    def soft_delete(self, media_ids: list[int], deleted: bool) -> dict:
        ids = list(dict.fromkeys(media_ids))
        with self.db.connect() as conn:
            if not ids:
                rows = []
            else:
                cond = "deleted_at IS NULL" if deleted else "deleted_at IS NOT NULL"
                rows = [dict(r) for r in conn.execute(
                    f"SELECT id, deleted_at FROM media WHERE {cond} AND id IN ({_ph(ids)})", tuple(ids))]
            changed = [r["id"] for r in rows]
            if changed:
                stamp = _now() if deleted else None  # same format as the single-item delete
                conn.execute(f"UPDATE media SET deleted_at=? WHERE id IN ({_ph(changed)})", (stamp, *changed))
            verb = "Moved {n} item(s) to Deleted" if deleted else "Restored {n} item(s)"
            entry = self._log(conn, "media.delete" if deleted else "media.restore", verb.format(n=len(changed)),
                              {"rows": rows}, len(changed))
        self.s.cluster.invalidate()
        return {"changed": len(changed), "audit_id": entry}

    def _undo_media_delete(self, conn, d):
        conn.executemany("UPDATE media SET deleted_at=NULL WHERE id=?", [(r["id"],) for r in d["rows"]])

    def _undo_media_restore(self, conn, d):
        conn.executemany("UPDATE media SET deleted_at=? WHERE id=?", [(r["deleted_at"], r["id"]) for r in d["rows"]])

    # -- people: merge / split with confidence -----------------------------------------
    def _centroid(self, person_id: int) -> Optional[np.ndarray]:
        rows = self.db.all("SELECT embedding_offset, embedding_sha, quality FROM faces WHERE person_id=? AND deleted_at IS NULL "
                           "AND review_state != 'rejected' ORDER BY quality DESC LIMIT 200", (person_id,))
        vecs = []
        for r in rows:
            try:
                vecs.append(self.s.store.read(r["embedding_offset"], r["embedding_sha"]))
            except Exception:
                continue
        if not vecs:
            return None
        c = np.mean(vecs, axis=0)
        return c / max(float(np.linalg.norm(c)), 1e-12)

    def merge_preview(self, source_id: int, target_id: int, limit: int = 24) -> dict:
        """How alike two people are, and the source faces least like the target (to double-check)."""
        a, b = self._centroid(source_id), self._centroid(target_id)
        similarity = float(a @ b) if a is not None and b is not None else None
        faces = []
        if b is not None:
            for r in self.db.all("SELECT id, embedding_offset, embedding_sha FROM faces WHERE person_id=? AND deleted_at IS NULL",
                                 (source_id,)):
                try:
                    faces.append({"face_id": r["id"], "similarity": round(float(self.s.store.read(r["embedding_offset"], r["embedding_sha"]) @ b), 4)})
                except Exception:
                    continue
            faces.sort(key=lambda f: f["similarity"])
        label = None if similarity is None else "high" if similarity >= 0.6 else "medium" if similarity >= 0.4 else "low"
        return {"similarity": None if similarity is None else round(similarity, 4), "confidence": label,
                "least_similar": faces[:limit], "face_count": len(faces)}

    @staticmethod
    def _row_json(row) -> dict:
        return {k: (v.hex() if isinstance(v, (bytes, bytearray, memoryview)) else v) for k, v in dict(row).items()}

    def merge_people(self, source_id: int, target_id: int) -> dict:
        with self.db.connect() as conn:
            source = conn.execute("SELECT * FROM people WHERE id=?", (source_id,)).fetchone()
            target = conn.execute("SELECT * FROM people WHERE id=?", (target_id,)).fetchone()
            if source is None or target is None:
                raise KeyError("Person not found")
            pair = (source_id, target_id)
            undo = {
                "source": self._row_json(source), "target_id": target_id, "target_name": target["name"],
                "faces": [r[0] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (source_id,))],
                # Exact snapshots of everything the merge rewrites or cascades away.
                "exclusions": [dict(r) for r in conn.execute("SELECT * FROM exclusions WHERE person_id IN (?,?)", pair)],
                "rejections": [dict(r) for r in conn.execute("SELECT * FROM rejections WHERE person_id IN (?,?)", pair)],
                "hard_negatives": [dict(r) for r in conn.execute("SELECT * FROM hard_negatives WHERE person_id=?", (source_id,))],
            }
            conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (target_id, source_id))
            conn.execute("UPDATE OR IGNORE exclusions SET person_id=? WHERE person_id=?", (target_id, source_id))
            conn.execute("DELETE FROM exclusions WHERE person_id=?", (source_id,))
            conn.execute("UPDATE OR IGNORE rejections SET person_id=? WHERE person_id=?", (target_id, source_id))
            conn.execute("DELETE FROM rejections WHERE person_id=?", (source_id,))
            if not (target["name"] or "").strip() and (source["name"] or "").strip():
                conn.execute("UPDATE people SET name=? WHERE id=?", (source["name"], target_id))
            conn.execute("DELETE FROM people WHERE id=?", (source_id,))
            self.s.cluster.refresh(conn, [target_id])
            name = (source["name"] or "").strip() or f"Person {source_id}"
            tname = (target["name"] or "").strip() or f"Person {target_id}"
            entry = self._log(conn, "people.merge", f"Merged {name} into {tname}", undo, len(undo["faces"]))
        self.s.cluster.invalidate()
        return {"person_id": target_id, "audit_id": entry}

    @staticmethod
    def _insert(conn, table: str, rows: list[dict]) -> None:
        for row in rows:
            cols = list(row)
            conn.execute(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES ({_ph(cols)})", tuple(row[c] for c in cols))

    def _undo_people_merge(self, conn, d):
        src = dict(d["source"])
        if src.get("centroid") is not None:
            src["centroid"] = bytes.fromhex(src["centroid"])
        cols = list(src)
        conn.execute(f"INSERT OR REPLACE INTO people({','.join(cols)}) VALUES ({_ph(cols)})", tuple(src[c] for c in cols))
        if d["faces"]:
            conn.execute(f"UPDATE faces SET person_id=? WHERE id IN ({_ph(d['faces'])})", (src["id"], *d["faces"]))
        pair = (src["id"], d["target_id"])
        for table in ("exclusions", "rejections"):
            conn.execute(f"DELETE FROM {table} WHERE person_id IN (?,?)", pair)
            self._insert(conn, table, d[table])
        self._insert(conn, "hard_negatives", d["hard_negatives"])
        conn.execute("UPDATE people SET name=? WHERE id=?", (d["target_name"], d["target_id"]))
        self.s.cluster.refresh(conn, [src["id"], d["target_id"]])

    def move_faces(self, face_ids: list[int], *, target_id: Optional[int] = None, name: Optional[str] = None,
                   expect_person: Optional[int] = None) -> dict:
        """Reassign faces to a person, or to a new person (``target_id`` None) — a split."""
        ids = list(dict.fromkeys(face_ids))
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT id, person_id, manual, review_state FROM faces WHERE id IN ({_ph(ids)})", tuple(ids))] if ids else []
            if len(rows) != len(ids):
                raise KeyError("One or more faces not found")
            if expect_person is not None and any(r["person_id"] != expect_person for r in rows):
                raise ValueError("Every face must currently belong to this person")
            created = None
            if target_id is None:
                target_id = created = conn.execute("INSERT INTO people(name) VALUES (?)", ((name or "").strip() or None,)).lastrowid
            elif not conn.execute("SELECT 1 FROM people WHERE id=?", (target_id,)).fetchone():
                raise KeyError("Target person not found")
            conn.execute(f"UPDATE faces SET person_id=?, manual=1, review_state='confirmed' WHERE id IN ({_ph(ids)})",
                         (target_id, *ids))
            touched = {r["person_id"] for r in rows if r["person_id"]} | {target_id}
            self.s.cluster.refresh(conn, list(touched))
            action, summary = ("people.split", f"Split {len(ids)} face(s) into a new person") if created else \
                ("faces.move", f"Moved {len(ids)} face(s)")
            entry = self._log(conn, action, summary, {"faces": rows, "created": created, "target": target_id}, len(ids))
        self.s.cluster.invalidate()
        return {"person_id": target_id, "audit_id": entry}

    def split_person(self, person_id: int, face_ids: list[int], name: Optional[str] = None) -> dict:
        return self.move_faces(face_ids, name=name, expect_person=person_id)

    def _undo_people_split(self, conn, d):
        touched = {d["target"]}
        for r in d["faces"]:
            conn.execute("UPDATE faces SET person_id=?, manual=?, review_state=? WHERE id=?",
                         (r["person_id"], r["manual"], r["review_state"], r["id"]))
            if r["person_id"]:
                touched.add(r["person_id"])
        if d["created"] and not conn.execute("SELECT 1 FROM faces WHERE person_id=? LIMIT 1", (d["created"],)).fetchone():
            conn.execute("DELETE FROM people WHERE id=?", (d["created"],))
            touched.discard(d["created"])
        self.s.cluster.refresh(conn, list(touched))

    _undo_faces_move = _undo_people_split

    def _undo_duplicates_resolve(self, conn, d):
        from .dedupe import undo_hook

        undo_hook(self.s.dedupe, conn, d)

    # -- smart collections ------------------------------------------------------------
    def collections(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM saved_searches WHERE is_collection=1 ORDER BY name COLLATE NOCASE")
        out = []
        for r in rows:
            query = json.loads(r["query"])
            result = self.s.search.run({**query, "limit": 4, "page": 1})
            out.append({"id": r["id"], "name": r["name"], "query": query, "item_count": result["total"],
                        "preview_ids": [i["id"] for i in result["items"]]})
        return out

    def collection_items(self, collection_id: int, page: int = 1, limit: int = 60) -> dict:
        row = self.db.one("SELECT * FROM saved_searches WHERE id=? AND is_collection=1", (collection_id,))
        if row is None:
            raise KeyError("Collection not found")
        # Re-run on every read: the collection is the query, so new matching files appear by themselves.
        return self.s.search.run({**json.loads(row["query"]), "page": page, "limit": limit})

    # -- memories ---------------------------------------------------------------------------
    def memories(self, today: Optional[date] = None, per_section: int = 12) -> list[dict]:
        today = today or date.today()
        sections = []
        md = today.strftime("%m-%d")
        years = self.db.all(
            """SELECT substr(captured_at, 1, 4) AS year, COUNT(*) AS n FROM media
               WHERE deleted_at IS NULL AND captured_at IS NOT NULL AND substr(captured_at, 6, 5) = ?
                 AND substr(captured_at, 1, 4) < ? AND COALESCE(date_source, '') != 'mtime'
               GROUP BY year ORDER BY year DESC""", (md, str(today.year)))
        for y in years:
            ids = [r["id"] for r in self.db.all(
                """SELECT m.id FROM media m LEFT JOIN quality_scores q ON q.media_id=m.id
                     AND q.formula_version=(SELECT MAX(formula_version) FROM quality_scores)
                   WHERE m.deleted_at IS NULL AND substr(m.captured_at, 1, 10) = ? AND COALESCE(m.date_source, '') != 'mtime'
                   ORDER BY COALESCE(q.score, 0) DESC, m.id LIMIT ?""", (f"{y['year']}-{md}", per_section))]
            ago = today.year - int(y["year"])
            sections.append({"kind": "on_this_day", "title": f"{ago} year{'s' if ago != 1 else ''} ago today",
                             "year": int(y["year"]), "total": y["n"], "media_ids": ids})
        # Events from this week in earlier years (trips and gatherings), best-shot covers.
        week = [(today.toordinal() + k) for k in range(-3, 4)]
        mds = {date.fromordinal(o).strftime("%m-%d") for o in week}
        events = self.db.all(
            f"""SELECT * FROM events WHERE item_count >= 5 AND substr(start_at, 6, 5) IN ({_ph(mds)})
                AND substr(start_at, 1, 4) < ? ORDER BY item_count DESC LIMIT 4""", (*mds, str(today.year)))
        for e in events:
            ids = [r["media_id"] for r in self.db.all(
                """SELECT em.media_id FROM event_media em LEFT JOIN quality_scores q ON q.media_id=em.media_id
                     AND q.formula_version=(SELECT MAX(formula_version) FROM quality_scores)
                   WHERE em.event_id=? ORDER BY COALESCE(q.score, 0) DESC LIMIT ?""", (e["id"], per_section))]
            sections.append({"kind": "event", "title": e["name"], "event_id": e["id"], "year": int(e["start_at"][:4]),
                             "total": e["item_count"], "media_ids": ids})
        return sections

