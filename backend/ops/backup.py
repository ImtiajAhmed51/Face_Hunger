"""Full backup (index, embeddings, people) and staged restore.

Export writes ``data/exports/face-hunger-backup-<utc>.zip`` containing a
consistent SQLite snapshot (online backup API, so the app keeps running),
every embedding store and a ``manifest.json`` with SHA-256 and size per
file. Thumbnails and ANN indexes are derived data and are left out unless
asked for; indexes rebuild themselves from the stores.

Restore never overwrites live files while the app is using them. It verifies
the archive, extracts it to ``data/restore-pending/`` and asks for a restart.
On the next start, before the database is opened, :func:`apply_pending_restore`
moves the current files to ``data/backups/pre-restore-<utc>/`` (nothing is
deleted) and moves the restored files into place.

CLI (with the app stopped)::

    python -m backend.ops.backup export
    python -m backend.ops.backup restore path/to/backup.zip
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

FORMAT = "face-hunger-backup"
FORMAT_VERSION = 1
PENDING = "restore-pending"
DB_NAME = "index.sqlite"
LIVE_FILES = (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", "embeddings.bin", "media_embeddings.bin")


class BackupError(ValueError):
    pass


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path, checkpoint: Callable[[], None] = lambda: None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for i, chunk in enumerate(iter(lambda: handle.read(4 << 20), b"")):
            if i % 16 == 0:
                checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def _payload_files(data_dir: Path, include_thumbnails: bool) -> list[tuple[str, Path]]:
    files = []
    for name in ("embeddings.bin", "media_embeddings.bin"):
        if (data_dir / name).is_file():
            files.append((name, data_dir / name))
    vectors = data_dir / "vectors"
    if vectors.is_dir():
        files += [(f"vectors/{p.name}", p) for p in sorted(vectors.glob("*.f32"))]
    if include_thumbnails and (data_dir / "thumbnails").is_dir():
        files += [(f"thumbnails/{p.name}", p) for p in sorted((data_dir / "thumbnails").glob("*.jpg"))]
    return files


def export_backup(db, data_dir: Path, *, include_thumbnails: bool = False, checkpoint: Callable[[], None] = lambda: None,
                  progress: Callable[..., None] = lambda **_: None) -> dict:
    data_dir = Path(data_dir)
    out_dir = data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"face-hunger-backup-{_stamp()}.zip"
    files = _payload_files(data_dir, include_thumbnails)
    total = sum(p.stat().st_size for _, p in files)
    need = total + (data_dir / DB_NAME).stat().st_size
    if shutil.disk_usage(out_dir).free < need * 1.1:
        raise BackupError(f"Not enough free space for a {need / 1e9:.2f} GB backup in {out_dir}")
    manifest = {"format": FORMAT, "version": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "schema_version": db.one("PRAGMA user_version")["user_version"], "files": {}}
    partial = target.with_suffix(".zip.part")
    done = 0
    try:
        with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
            snapshot = Path(tmp) / DB_NAME
            progress(phase="database", processed=0, total=total)
            with db.connect() as conn:
                dest = sqlite3.connect(snapshot)
                try:
                    conn.backup(dest)  # consistent snapshot while the app keeps writing
                finally:
                    dest.close()
            checkpoint()
            with zipfile.ZipFile(partial, "w", allowZip64=True) as zf:
                entries = [(DB_NAME, snapshot, zipfile.ZIP_DEFLATED)] + [(n, p, zipfile.ZIP_STORED) for n, p in files]
                for name, path, compression in entries:
                    checkpoint()
                    size_before = path.stat().st_size  # append-only stores: copy exactly this many bytes
                    digest = hashlib.sha256()
                    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    entry.compress_type = compression
                    with path.open("rb") as src, zf.open(entry, "w", force_zip64=True) as dst:
                        remaining = size_before
                        while remaining:
                            chunk = src.read(min(4 << 20, remaining))
                            if not chunk:
                                raise BackupError(f"{name} shrank while being backed up")
                            digest.update(chunk)
                            dst.write(chunk)
                            remaining -= len(chunk)
                            done += len(chunk)
                            checkpoint()
                            progress(phase="archiving", current=name, processed=done, total=total)
                    manifest["files"][name] = {"sha256": digest.hexdigest(), "bytes": size_before}
                zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {"path": str(target), "name": target.name, "bytes": target.stat().st_size, "files": len(manifest["files"]),
            "manifest": manifest}


MAX_ENTRIES, MAX_RATIO, BOMB_FLOOR = 2_000_000, 200, 32 << 20


def _expected_name(name: str) -> bool:
    """Only the files a backup can contain, at the places it puts them (no traversal, nothing executable)."""
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or name != path.as_posix():
        return False
    if name in (DB_NAME, "embeddings.bin", "media_embeddings.bin"):
        return True
    return len(path.parts) == 2 and ((path.parts[0] == "vectors" and path.suffix == ".f32")
                                     or (path.parts[0] == "thumbnails" and path.suffix == ".jpg"))


def verify_archive(path: Path, checkpoint: Callable[[], None] = lambda: None) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            if manifest.get("format") != FORMAT or manifest.get("version") != FORMAT_VERSION:
                raise BackupError("Not a Face Hunger backup (or an unsupported version)")
            names = set(zf.namelist())
            if not isinstance(manifest.get("files"), dict) or len(manifest["files"]) > MAX_ENTRIES:
                raise BackupError("The backup's file list is invalid")
            for name, info in manifest["files"].items():
                if name not in names or not _expected_name(name):
                    raise BackupError(f"Archive entry {name} is missing or unsafe")
                # Decompression-bomb guard: the declared size must be the stored size, and nothing in a
                # real backup compresses anywhere near this well (only the database is deflated).
                entry = zf.getinfo(name)
                if entry.file_size != info.get("bytes") or \
                        (entry.file_size > BOMB_FLOOR and entry.file_size > MAX_RATIO * max(1, entry.compress_size)):
                    raise BackupError(f"Archive entry {name} has an implausible size: the backup is not trusted")
                digest = hashlib.sha256()
                size = 0
                with zf.open(name) as src:
                    for chunk in iter(lambda: src.read(4 << 20), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        checkpoint()
                if digest.hexdigest() != info["sha256"] or size != info["bytes"]:
                    raise BackupError(f"Checksum mismatch for {name}: the backup is damaged")
            if DB_NAME not in manifest["files"]:
                raise BackupError("Backup has no database")
    except zipfile.BadZipFile as exc:
        raise BackupError(f"Not a readable zip archive: {exc}") from exc
    return manifest


def stage_restore(path: Path, data_dir: Path, checkpoint: Callable[[], None] = lambda: None) -> dict:
    """Verify and extract a backup so it is swapped in on the next start."""
    manifest = verify_archive(path, checkpoint)
    need = sum(int(info["bytes"]) for info in manifest["files"].values())
    if shutil.disk_usage(data_dir).free < need * 1.1:
        raise BackupError(f"Not enough free space to restore this backup ({need / 1e9:.2f} GB needed)")
    pending = Path(data_dir) / PENDING
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    with zipfile.ZipFile(path) as zf:
        for name in manifest["files"]:
            checkpoint()
            dest = pending / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, 4 << 20)
    (pending / "READY").write_text(json.dumps({"source": str(path), "manifest": manifest}))
    return {"staged": True, "restart_required": True, "files": len(manifest["files"]),
            "created_at": manifest["created_at"]}


def apply_pending_restore(data_dir: Path) -> dict | None:
    """Swap a staged restore into place. Call before opening the database."""
    data_dir = Path(data_dir)
    pending = data_dir / PENDING
    if not (pending / "READY").is_file():
        return None
    aside = data_dir / "backups" / f"pre-restore-{_stamp()}"
    aside.mkdir(parents=True)
    for name in LIVE_FILES:
        if (data_dir / name).exists():
            os.replace(data_dir / name, aside / name)
    if (data_dir / "vectors").is_dir():
        os.replace(data_dir / "vectors", aside / "vectors")
    moved = 0
    for src in sorted(pending.rglob("*")):
        if src.is_file() and src.name != "READY":
            rel = src.relative_to(pending)
            dest = data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rel.parts[0] == "thumbnails" and dest.exists():
                continue
            os.replace(src, dest)
            moved += 1
    info = json.loads((pending / "READY").read_text())
    shutil.rmtree(pending)
    logger.warning("Restored backup from %s (%d files); previous data kept in %s", info.get("source"), moved, aside)
    return {"restored_files": moved, "previous_data": str(aside), "source": info.get("source")}


def list_backups(data_dir: Path) -> list[dict]:
    folder = Path(data_dir) / "exports"
    items = []
    for path in sorted(folder.glob("face-hunger-backup-*.zip"), reverse=True):
        stat = path.stat()
        items.append({"name": path.name, "bytes": stat.st_size,
                      "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return items


def main(argv=None) -> int:
    import argparse

    from ..config import Config
    from ..db import Database

    parser = argparse.ArgumentParser(prog="python -m backend.ops.backup", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--thumbnails", action="store_true")
    res = sub.add_parser("restore")
    res.add_argument("archive")
    args = parser.parse_args(argv)
    config = Config()
    config.prepare()
    if args.command == "export":
        result = export_backup(Database(config.data_dir / DB_NAME), config.data_dir, include_thumbnails=args.thumbnails)
        print(f"Wrote {result['path']} ({result['bytes'] / 1e6:.1f} MB)")
    else:
        print(stage_restore(Path(args.archive), config.data_dir))
        print(apply_pending_restore(config.data_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
