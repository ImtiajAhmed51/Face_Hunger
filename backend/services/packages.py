"""Encrypted export / import of a library, a person or a selection (.fhpack, see ops/fhpack.py).

Inside the encrypted stream is a plain tar with:
  manifest.json                first member: scope, counts, model specs
  records/*.jsonl              media, people, faces (with vectors), albums, edits, misc
  records/vectors/<n>.jsonl    one file per media embedding space
  thumbnails/<old id>.jpg      optional
  media/<old id>/<name>        optional originals (stored, never re-encoded)

Export streams everything through the cipher; record files are spooled to disk first because
tar needs each member's size. Import decrypts and verifies the *whole* package into a private
staging folder before anything touches the library; a wrong passphrase or a single flipped
bit leaves nothing behind. Extraction only accepts the member names above (no absolute paths,
no "..", regular files only) and stops at the sizes the manifest declared.

Merging: media are matched by content hash, so the same photo is never duplicated. For
people and albums with the same name, and for photos that already have edits, the conflict
policy applies: ``skip`` keeps what is here, ``keep_both`` (default) adds the imported one
beside it ("Name (imported)", or an extra history entry for edits), ``overwrite`` replaces.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from .. import edits as edit_model
from ..ops import fhpack
from ..vectors.specs import ModelSpec

logger = logging.getLogger(__name__)

FORMAT = "face-hunger-package"
POLICIES = ("skip", "keep_both", "overwrite")
MEMBER = re.compile(r"^(manifest\.json|records/[a-z_]+\.jsonl|records/vectors/[0-9]+\.jsonl|thumbnails/[0-9]+\.jpg|"
                    r"media/[0-9]+/[^/\\\x00]{1,255})$")
MEDIA_COLS = ("name", "kind", "size", "mtime_ns", "captured_at", "width", "height", "duration", "content_hash", "phash",
              "date_source", "gps_lat", "gps_lon", "gps_alt", "camera_make", "camera_model", "lens", "meta_version")
FACE_COLS = ("bbox", "timestamp", "detection", "similarity", "review_state", "manual", "quality", "track_id", "landmarks")


def _b64(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype="<f4").tobytes()).decode()


def _vec(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype="<f4").astype(np.float32)


class PackageService:
    def __init__(self, services):
        self.s = services
        self._secrets: dict[str, str] = {}  # passphrases stay in memory only, never in the jobs table

    @property
    def db(self):
        return self.s.db

    def folder(self) -> Path:
        path = Path(self.s.config.data_dir) / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def hold(self, passphrase: str) -> str:
        token = secrets.token_urlsafe(16)
        self._secrets[token] = passphrase
        return token

    def take(self, token: str) -> str:
        try:
            return self._secrets.pop(token)
        except KeyError:
            raise fhpack.PackageError("The passphrase is no longer in memory (the app restarted). Start again.") from None

    def list(self) -> list[dict]:
        items = []
        for path in sorted(self.folder().glob("*.fhpack"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = path.stat()
            items.append({"name": path.name, "bytes": stat.st_size,
                          "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
        return items

    def resolve(self, source: str) -> Path:
        """A package in the exports folder (by name) or an absolute path to a .fhpack file."""
        candidate = Path(source).expanduser()
        if not candidate.is_absolute():
            if candidate.name != source or not source.endswith(".fhpack"):
                raise fhpack.PackageError("Invalid package name")
            candidate = self.folder() / source
        if candidate.suffix != ".fhpack" or not candidate.is_file():
            raise fhpack.PackageError("Package file not found")
        return candidate

    # ------------------------------------------------------------------ export
    def _scope_media(self, scope: dict) -> list[int]:
        kind = scope.get("type", "library")
        if kind == "library":
            return [r["id"] for r in self.db.all("SELECT id FROM media WHERE deleted_at IS NULL ORDER BY id")]
        if kind == "person":
            return [r["media_id"] for r in self.db.all(
                "SELECT DISTINCT media_id FROM faces WHERE person_id=? AND deleted_at IS NULL ORDER BY media_id",
                (int(scope["person_id"]),))]
        if kind == "selection":
            ids = [int(m) for m in scope.get("media_ids", [])]
            return [r["id"] for r in self.db.all(
                f"SELECT id FROM media WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", tuple(ids))] if ids else []
        raise fhpack.PackageError("scope.type must be library, person or selection")

    def _chunks(self, ids: list[int], size: int = 500) -> Iterable[list[int]]:
        for start in range(0, len(ids), size):
            yield ids[start:start + size]

    def export(self, scope: dict, passphrase: str, *, include_media: bool = False, include_thumbnails: bool = True,
               target: Optional[Path] = None, checkpoint: Callable[[], None] = lambda: None,
               progress: Callable[..., None] = lambda **_: None) -> dict:
        if len(passphrase) < 8:
            raise fhpack.PackageError("Use a passphrase of at least 8 characters")
        media_ids = self._scope_media(scope)
        if not media_ids:
            raise fhpack.PackageError("Nothing to export")
        library = scope.get("type", "library") == "library"
        person_only = int(scope["person_id"]) if scope.get("type") == "person" else None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = target or self.folder() / f"face-hunger-{scope.get('type', 'library')}-{stamp}.fhpack"
        part = target.with_suffix(".fhpack.part")
        spaces = [self.s.vectors.get(r["key"]) for r in self.s.vectors.rows() if r["subject"] == "media"]
        counts = {"media": len(media_ids), "faces": 0, "people": 0, "albums": 0, "edits": 0, "vectors": 0, "files": 0}
        total_steps = len(media_ids) * (2 + int(include_media) + int(include_thumbnails))
        step = 0

        def tick(n: int = 1):
            nonlocal step
            step += n
            progress(processed=step, total=total_steps)

        try:
            with tempfile.TemporaryDirectory(dir=self.folder(), prefix=".pack-") as tmp, part.open("wb") as raw, \
                    fhpack.EncryptedWriter(raw, passphrase) as enc, tarfile.open(fileobj=enc, mode="w|") as tar:
                spool = Path(tmp)

                def add_bytes(name: str, data: bytes):
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = len(data), 0
                    import io
                    tar.addfile(info, io.BytesIO(data))

                def add_file(name: str, path: Path):
                    info = tarfile.TarInfo(name)
                    info.size, info.mtime = path.stat().st_size, 0
                    with path.open("rb") as handle:
                        tar.addfile(info, handle)

                manifest = {"format": FORMAT, "version": 1, "scope": scope, "include_media": include_media,
                            "include_thumbnails": include_thumbnails, "created_at": stamp,
                            "schema_version": self.db.one("PRAGMA user_version")["user_version"],
                            "media_count": len(media_ids),
                            "spaces": [{"index": i, "key": sp.key, "model_id": sp.spec.model_id, "version": sp.spec.version,
                                        "dim": sp.spec.dim, "role": sp.spec.role} for i, sp in enumerate(spaces)]}
                add_bytes("manifest.json", json.dumps(manifest).encode())

                # -- records, spooled to disk in batches (constant memory) --
                people_ids: set[int] = set()
                with (spool / "media.jsonl").open("w") as fm, (spool / "faces.jsonl").open("w") as ff, \
                        (spool / "edits.jsonl").open("w") as fe:
                    for chunk in self._chunks(media_ids):
                        checkpoint()
                        ph = ",".join("?" * len(chunk))
                        for row in self.db.all(f"SELECT * FROM media WHERE id IN ({ph})", tuple(chunk)):
                            fm.write(json.dumps({"id": row["id"], **{c: row[c] for c in MEDIA_COLS}}) + "\n")
                        where = "AND person_id = ?" if person_only is not None else ""
                        args = (*chunk, person_only) if person_only is not None else tuple(chunk)
                        for face in self.db.all(f"SELECT * FROM faces WHERE media_id IN ({ph}) AND deleted_at IS NULL {where}", args):
                            try:
                                vec = self.s.store.read(face["embedding_offset"], face["embedding_sha"])
                            except Exception:
                                continue
                            if face["person_id"]:
                                people_ids.add(face["person_id"])
                            ff.write(json.dumps({"id": face["id"], "media_id": face["media_id"], "person_id": face["person_id"],
                                                 **{c: face[c] for c in FACE_COLS}, "vec": _b64(vec)}) + "\n")
                            counts["faces"] += 1
                        for row in self.db.all(f"SELECT media_id FROM media_edits WHERE media_id IN ({ph})", tuple(chunk)):
                            edit = self.s.edits.get(row["media_id"])
                            if edit != edit_model.EMPTY:
                                fe.write(json.dumps({"media_id": row["media_id"], "edit": edit}) + "\n")
                                counts["edits"] += 1
                        tick(len(chunk))
                with (spool / "people.jsonl").open("w") as fp:
                    for chunk in self._chunks(sorted(people_ids)):
                        for row in self.db.all(f"SELECT id, name FROM people WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
                            fp.write(json.dumps(row) + "\n")
                            counts["people"] += 1
                in_scope = set(media_ids)
                with (spool / "albums.jsonl").open("w") as fa:
                    for album in (self.db.all("SELECT * FROM albums ORDER BY id") if library else []):
                        items = [r["media_id"] for r in self.db.all(
                            "SELECT media_id FROM album_media WHERE album_id=? ORDER BY position", (album["id"],))]
                        fa.write(json.dumps({"name": album["name"], "cover": album["cover_media_id"],
                                             "media_ids": [m for m in items if m in in_scope]}) + "\n")
                        counts["albums"] += 1
                misc = {"favorites": [r["media_id"] for r in self.db.all("SELECT media_id FROM favorites") if r["media_id"] in in_scope]}
                if library:
                    misc["saved_searches"] = self.db.all("SELECT name, query, is_collection FROM saved_searches")
                    misc["exclusions"] = self.db.all("SELECT person_id, media_id FROM exclusions")
                (spool / "misc.jsonl").write_text(json.dumps(misc) + "\n")
                for name in ("media", "people", "faces", "albums", "edits", "misc"):
                    add_file(f"records/{name}.jsonl", spool / f"{name}.jsonl")
                for index, space in enumerate(spaces):
                    path = spool / f"v{index}.jsonl"
                    with path.open("w") as fv:
                        for chunk in self._chunks(media_ids):
                            checkpoint()
                            for media_id in chunk:
                                vec = space.vector(media_id)
                                if vec is not None:
                                    fv.write(json.dumps({"media_id": media_id, "vec": _b64(vec)}) + "\n")
                                    counts["vectors"] += 1
                    add_file(f"records/vectors/{index}.jsonl", path)
                    path.unlink()
                tick(len(media_ids))

                # -- files, streamed straight from disk --
                if include_thumbnails:
                    thumbs = Path(self.s.config.data_dir) / "thumbnails"
                    for media_id in media_ids:
                        checkpoint()
                        thumb = thumbs / f"media-{media_id}.jpg"
                        if thumb.is_file():
                            add_file(f"thumbnails/{media_id}.jpg", thumb)
                        tick()
                if include_media:
                    for chunk in self._chunks(media_ids):
                        for row in self.db.all(f"SELECT id, path, name FROM media WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
                            checkpoint()
                            source = Path(row["path"])
                            if source.is_file():
                                add_file(f"media/{row['id']}/{row['name']}", source)
                                counts["files"] += 1
                            tick()
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return {"name": target.name, "path": str(target), "bytes": target.stat().st_size, **counts,
                "processed": total_steps, "total": total_steps}

    # ------------------------------------------------------------------ import
    def inspect(self, source: Path, passphrase: str) -> dict:
        with source.open("rb") as raw:
            reader = fhpack.DecryptedReader(raw, passphrase)
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                first = tar.next()
                if first is None or first.name != "manifest.json" or first.size > 1 << 20:
                    raise fhpack.IntegrityError("The package has no manifest")
                manifest = json.loads(tar.extractfile(first).read())
        if manifest.get("format") != FORMAT:
            raise fhpack.PackageError("Not a Face Hunger package")
        return {"header": {k: reader.header[k] for k in ("version", "cipher", "kdf", "created_at")}, "manifest": manifest,
                "bytes": source.stat().st_size}

    def _extract(self, source: Path, passphrase: str, staging: Path, checkpoint: Callable[[], None]) -> dict:
        """Decrypt + verify the entire package into ``staging``. Nothing else is touched."""
        limit = source.stat().st_size + (16 << 20)  # payload is never larger than the (uncompressed) package
        written = members = 0
        manifest = None
        with source.open("rb") as raw:
            reader = fhpack.DecryptedReader(raw, passphrase)
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                for member in tar:
                    checkpoint()
                    members += 1
                    if not member.isfile() or not MEMBER.match(member.name) or ".." in member.name.split("/"):
                        raise fhpack.IntegrityError(f"The package contains an unexpected entry: {member.name[:80]!r}")
                    written += member.size
                    if written > limit or members > 5_000_000:
                        raise fhpack.IntegrityError("The package is larger than it declares")
                    target = staging / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(member)
                    with target.open("wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
                    if member.name == "manifest.json":
                        manifest = json.loads(target.read_text())
            reader.verify_to_end()
        if not manifest or manifest.get("format") != FORMAT or manifest.get("version") != 1:
            raise fhpack.PackageError("Not a Face Hunger package (or a newer version)")
        return manifest

    @staticmethod
    def _lines(path: Path):
        if path.is_file():
            with path.open() as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)

    def import_package(self, source: Path, passphrase: str, *, conflict: str = "keep_both",
                       checkpoint: Callable[[], None] = lambda: None,
                       progress: Callable[..., None] = lambda **_: None) -> dict:
        if conflict not in POLICIES:
            raise fhpack.PackageError(f"conflict must be one of {', '.join(POLICIES)}")
        staging = Path(tempfile.mkdtemp(prefix="import-", dir=self.folder()))
        try:
            progress(phase="verifying", processed=0, total=3)
            manifest = self._extract(source, passphrase, staging, checkpoint)
            progress(phase="merging", processed=1, total=3)
            report = self._merge(staging, manifest, conflict, checkpoint)
            progress(phase="done", processed=3, total=3)
            return report
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _merge(self, staging: Path, manifest: dict, conflict: str, checkpoint) -> dict:
        s, db = self.s, self.db
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = Path(s.config.data_dir) / "imported" / f"{manifest.get('created_at', stamp)}-{stamp}"
        report = {"media_new": 0, "media_matched": 0, "people_new": 0, "people_matched": 0, "faces": 0, "albums": 0,
                  "edits": 0, "vectors": 0, "files": 0, "conflicts": [], "policy": conflict}
        media_map: dict[int, int] = {}
        new_media: set[int] = set()
        person_map: dict[int, Optional[int]] = {}
        touched_people: set[int] = set()
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            library_id = None

            def library() -> int:
                nonlocal library_id
                if library_id is None:
                    dest.mkdir(parents=True, exist_ok=True)
                    library_id = conn.execute("INSERT INTO libraries(path, name) VALUES (?, ?)",
                                              (str(dest), f"Imported {stamp[:8]}")).lastrowid
                return library_id

            # -- media: the same content hash is the same item --
            for rec in self._lines(staging / "records" / "media.jsonl"):
                checkpoint()
                existing = None
                if rec.get("content_hash"):
                    existing = conn.execute("SELECT id FROM media WHERE content_hash=? AND deleted_at IS NULL ORDER BY id LIMIT 1",
                                            (rec["content_hash"],)).fetchone()
                if existing is None:
                    # No hash on one side (e.g. a video that was never hashed): same name, size and kind.
                    existing = conn.execute(
                        "SELECT id FROM media WHERE name=? AND size=? AND kind=? AND deleted_at IS NULL "
                        "AND (content_hash IS NULL OR ? IS NULL) ORDER BY id LIMIT 1",
                        (rec["name"], rec["size"], rec["kind"], rec.get("content_hash"))).fetchone()
                if existing:
                    media_map[rec["id"]] = existing["id"]
                    report["media_matched"] += 1
                    continue
                packed = staging / "media" / str(rec["id"]) / rec["name"]
                target = dest / rec["name"]
                counter = 1
                while target.exists() or conn.execute("SELECT 1 FROM media WHERE path=?", (str(target),)).fetchone():
                    target = dest / f"{Path(rec['name']).stem}-{counter}{Path(rec['name']).suffix}"
                    counter += 1
                lid = library()
                present = packed.is_file()
                if present:
                    shutil.move(str(packed), str(target))
                    report["files"] += 1
                cols = ("library_id", "path", *MEDIA_COLS, "status", "missing", "indexed_at")
                values = (lid, str(target), *(rec.get(c) for c in MEDIA_COLS), "indexed", 0 if present else 1,
                          datetime.now(timezone.utc).isoformat(timespec="seconds"))
                new_id = conn.execute(f"INSERT INTO media({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values).lastrowid
                media_map[rec["id"]] = new_id
                new_media.add(new_id)
                report["media_new"] += 1
                thumb = staging / "thumbnails" / f"{rec['id']}.jpg"
                if thumb.is_file():
                    shutil.move(str(thumb), str(Path(s.config.data_dir) / "thumbnails" / f"media-{new_id}.jpg"))

            # -- people --
            for rec in self._lines(staging / "records" / "people.jsonl"):
                name = (rec.get("name") or "").strip()
                existing = conn.execute("SELECT id FROM people WHERE lower(name)=lower(?) ORDER BY id LIMIT 1", (name,)).fetchone() if name else None
                if existing and conflict != "keep_both":
                    person_map[rec["id"]] = existing["id"]
                    report["people_matched"] += 1
                    report["conflicts"].append({"type": "person", "name": name, "action": "merged into existing"})
                else:
                    label = f"{name} (imported)" if existing else (name or None)
                    if existing:
                        report["conflicts"].append({"type": "person", "name": name, "action": f"kept both as {label!r}"})
                    person_map[rec["id"]] = conn.execute("INSERT INTO people(name) VALUES (?)", (label,)).lastrowid
                    report["people_new"] += 1

            # -- faces (only where this library has none for that media, unless overwriting) --
            has_faces = {r[0] for r in conn.execute("SELECT DISTINCT media_id FROM faces WHERE deleted_at IS NULL")}
            cleared: set[int] = set()
            for rec in self._lines(staging / "records" / "faces.jsonl"):
                checkpoint()
                media_id = media_map.get(rec["media_id"])
                if media_id is None:
                    continue
                if media_id in has_faces:
                    if conflict != "overwrite":
                        continue
                    if media_id not in cleared:
                        conn.execute("DELETE FROM faces WHERE media_id=?", (media_id,))
                        cleared.add(media_id)
                offset, sha = s.store.append(_vec(rec["vec"]))
                person_id = person_map.get(rec["person_id"]) if rec.get("person_id") else None
                conn.execute(
                    f"INSERT INTO faces(media_id, person_id, {', '.join(FACE_COLS)}, embedding_offset, embedding_sha) "
                    f"VALUES (?, ?, {', '.join('?' * len(FACE_COLS))}, ?, ?)",
                    (media_id, person_id, *(rec.get(c) for c in FACE_COLS), offset, sha))
                if person_id:
                    touched_people.add(person_id)
                report["faces"] += 1

            # -- edits --
            for rec in self._lines(staging / "records" / "edits.jsonl"):
                media_id = media_map.get(rec["media_id"])
                if media_id is None:
                    continue
                edit = edit_model.normalize(rec["edit"])
                current = s.edits._row_to_edit(conn.execute("SELECT * FROM media_edits WHERE media_id=?", (media_id,)).fetchone())
                on_disk = Path(conn.execute("SELECT path FROM media WHERE id=?", (media_id,)).fetchone()["path"]).is_file()
                if current == edit_model.EMPTY or conflict == "overwrite":
                    s.edits._store(conn, media_id, edit, "import", write_sidecar=on_disk)
                    report["edits"] += 1
                elif current != edit:
                    if conflict == "keep_both":  # keep ours as current; the imported edit stays one click away in the history
                        conn.execute("INSERT INTO edit_history(media_id, source, summary, before, after) VALUES (?,?,?,?,?)",
                                     (media_id, "import", "imported edit (kept beside yours)", json.dumps(edit), json.dumps(current)))
                    report["conflicts"].append({"type": "edit", "media_id": media_id,
                                                "action": "kept yours" if conflict == "skip" else "kept both (see history)"})

            # -- albums, favorites, saved searches, exclusions --
            for rec in self._lines(staging / "records" / "albums.jsonl"):
                ids = [media_map[m] for m in rec["media_ids"] if m in media_map]
                existing = conn.execute("SELECT id FROM albums WHERE lower(name)=lower(?) LIMIT 1", (rec["name"],)).fetchone()
                if existing and conflict == "skip":
                    report["conflicts"].append({"type": "album", "name": rec["name"], "action": "kept yours"})
                    continue
                if existing and conflict == "overwrite":
                    album_id = existing["id"]
                    conn.execute("DELETE FROM album_media WHERE album_id=?", (album_id,))
                    report["conflicts"].append({"type": "album", "name": rec["name"], "action": "replaced"})
                else:
                    name = f"{rec['name']} (imported)" if existing else rec["name"]
                    if existing:
                        report["conflicts"].append({"type": "album", "name": rec["name"], "action": f"kept both as {name!r}"})
                    album_id = conn.execute("INSERT INTO albums(name) VALUES (?)", (name,)).lastrowid
                conn.executemany("INSERT OR IGNORE INTO album_media(album_id, media_id, position) VALUES (?,?,?)",
                                 [(album_id, m, i + 1) for i, m in enumerate(ids)])
                conn.execute("UPDATE albums SET cover_media_id=? WHERE id=?", (media_map.get(rec.get("cover")) or (ids[0] if ids else None), album_id))
                report["albums"] += 1
            for misc in self._lines(staging / "records" / "misc.jsonl"):
                conn.executemany("INSERT OR IGNORE INTO favorites(media_id) VALUES (?)",
                                 [(media_map[m],) for m in misc.get("favorites", []) if m in media_map])
                for saved in misc.get("saved_searches", []):
                    if not conn.execute("SELECT 1 FROM saved_searches WHERE name=? AND query=?", (saved["name"], saved["query"])).fetchone():
                        conn.execute("INSERT INTO saved_searches(name, query, is_collection) VALUES (?,?,?)",
                                     (saved["name"], saved["query"], saved.get("is_collection", 0)))
                for ex in misc.get("exclusions", []):
                    if person_map.get(ex["person_id"]) and ex["media_id"] in media_map:
                        conn.execute("INSERT OR IGNORE INTO exclusions(person_id, media_id) VALUES (?,?)",
                                     (person_map[ex["person_id"]], media_map[ex["media_id"]]))
            if touched_people:
                s.cluster.refresh(conn, sorted(touched_people))

        # -- media vectors (after the rows are committed), new media only unless overwriting --
        for spec in manifest.get("spaces", []):
            path = staging / "records" / "vectors" / f"{spec['index']}.jsonl"
            if not path.is_file():
                continue
            space = s.vectors.register(ModelSpec(spec["model_id"], spec["version"], int(spec["dim"]), "media", spec["role"]))
            batch = []
            for rec in self._lines(path):
                media_id = media_map.get(rec["media_id"])
                if media_id is None or (media_id not in new_media and conflict != "overwrite"):
                    continue
                batch.append((media_id, _vec(rec["vec"])))
                if len(batch) >= 256:
                    checkpoint()
                    report["vectors"] += space.add(batch)
                    batch = []
            if batch:
                report["vectors"] += space.add(batch)
            space.maybe_save(force=True)
        s.cluster.invalidate()
        report["library"] = str(dest) if report["media_new"] else None
        return report
