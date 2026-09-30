"""Health endpoints' logic: liveness summary, storage usage and a full integrity check."""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

STARTED = time.time()
_size_cache: dict[str, tuple[float, int, int]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dir_usage(path: Path, max_age: float = 300.0) -> tuple[int, int]:
    """(bytes, files) under ``path``; cached because thumbnails can be 100k+ files."""
    key = str(path)
    cached = _size_cache.get(key)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1], cached[2]
    total = files = 0
    if path.is_dir():
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                    files += 1
                except OSError:
                    continue
    elif path.is_file():
        total, files = path.stat().st_size, 1
    _size_cache[key] = (time.monotonic(), total, files)
    return total, files


def summary(services) -> dict:
    """Cheap liveness/readiness report for GET /api/health."""
    s = services
    checks: dict[str, dict] = {}
    try:
        version = s.db.one("PRAGMA user_version")["user_version"]
        s.db.one("SELECT 1 AS ok")
        checks["database"] = {"ok": True, "schema_version": version}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": str(exc)}
    free = shutil.disk_usage(s.config.data_dir).free
    checks["disk"] = {"ok": free > 1_000_000_000, "free_bytes": free}
    runner = s.jobs._thread
    checks["jobs"] = {"ok": runner is not None and runner.is_alive(),
                      "queued": s.db.one("SELECT COUNT(*) c FROM jobs WHERE status='queued'")["c"],
                      "running": s.db.one("SELECT COUNT(*) c FROM jobs WHERE status='running'")["c"]}
    checks["watcher"] = {"ok": s.watcher.running or not s.config.watch, **s.watcher.status()}
    loaded = [slot.status() for model in (s.models.siglip, s.models.dino) for slot in model.slots() if slot.loaded]
    checks["models"] = {"ok": True, "loaded": loaded, "face_engine": s.engine.status().get("state")}
    warnings = list(getattr(s, "config_warnings", []))
    degraded = [name for name, check in checks.items() if not check["ok"]]
    return {
        "status": "ok" if not degraded else ("down" if "database" in degraded else "degraded"),
        "degraded": degraded,
        "version": "1.0.0",
        "uptime_s": round(time.time() - STARTED, 1),
        "checks": checks,
        "config_warnings": warnings,
    }


def storage(services) -> dict:
    data = services.config.data_dir
    parts = {
        "database": [data / "index.sqlite", data / "index.sqlite-wal", data / "index.sqlite-shm"],
        "face_embeddings": [data / "embeddings.bin"],
        "media_embeddings": [data / "media_embeddings.bin"],
        "vector_stores": [data / "vectors"],
        "thumbnails": [data / "thumbnails"],
        "video_previews": [data / "hover_previews", data / "video_cache"],
        "backups": [data / "backups", data / "exports"],
    }
    out = {}
    for name, paths in parts.items():
        size = files = 0
        for path in paths:
            b, f = dir_usage(path)
            size, files = size + b, files + f
        out[name] = {"bytes": size, "files": files}
    out["total_bytes"] = sum(v["bytes"] for v in out.values() if isinstance(v, dict))
    disk = shutil.disk_usage(data)
    out["disk"] = {"total": disk.total, "free": disk.free}
    return out


def library_counts(db) -> dict:
    row = db.one("""SELECT
        SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS media,
        SUM(CASE WHEN deleted_at IS NULL AND missing=1 THEN 1 ELSE 0 END) AS missing,
        SUM(CASE WHEN deleted_at IS NULL AND status='failed' THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted,
        COALESCE(SUM(CASE WHEN deleted_at IS NULL THEN size END), 0) AS original_bytes
      FROM media""") or {}
    return {k: int(v or 0) for k, v in row.items()} | {
        "faces": db.one("SELECT COUNT(*) c FROM faces WHERE deleted_at IS NULL")["c"],
        "people": db.one("SELECT COUNT(*) c FROM people WHERE face_count > 0")["c"],
    }


# ---------------------------------------------------------------------------
# Full integrity check (runs as the `integrity_check` job)
# ---------------------------------------------------------------------------

def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _problem(problems: list, severity: str, kind: str, message: str, count: int = 1, sample=None) -> None:
    problems.append({"severity": severity, "kind": kind, "message": message, "count": count,
                     "sample": (sample or [])[:20]})


def _check_vector_rows(rows: list[dict], file, verify_sample: int, label: str, problems: list,
                       checkpoint: Callable[[], None]) -> dict:
    size = file.path.stat().st_size if file.path.exists() else 0
    record = file.record
    torn = size % record
    dangling = [r for r in rows if r["offset"] < 0 or r["offset"] % record or r["offset"] + record > size]
    if torn:
        _problem(problems, "error", "vector_file_torn", f"{label}: file ends with a partial {torn}-byte record")
    if dangling:
        _problem(problems, "error", "vector_rows_dangling",
                 f"{label}: {len(dangling)} rows point outside the vector file", len(dangling),
                 [r["key"] for r in dangling])
    dangling_ids = {id(r) for r in dangling}
    valid = [r for r in rows if id(r) not in dangling_ids]
    sample = valid if len(valid) <= verify_sample else random.sample(valid, verify_sample)
    bad = []
    for i, row in enumerate(sample):
        if i % 500 == 0:
            checkpoint()
        try:
            file.read(row["offset"], row["sha"])
        except ValueError:
            bad.append(row["key"])
    if bad:
        estimate = round(len(bad) / max(1, len(sample)) * len(valid))
        _problem(problems, "error", "vector_checksum",
                 f"{label}: {len(bad)} of {len(sample)} checked vectors fail their checksum "
                 f"(~{estimate} of {len(valid)} overall)", len(bad), bad)
    return {"rows": len(rows), "file_bytes": size, "checked": len(sample), "checksum_failures": len(bad),
            "dangling": len(dangling), "torn_bytes": torn}


def integrity_check(services, *, checkpoint: Callable[[], None] = lambda: None,
                    progress: Callable[..., None] = lambda **_: None, verify_sample: int = 5000) -> dict:
    s = services
    db = s.db
    problems: list[dict] = []
    report: dict = {"started_at": _now()}
    progress(phase="database", processed=0, total=5)

    # 1. SQLite structure
    rows = db.all("PRAGMA quick_check")
    messages = [list(r.values())[0] for r in rows]
    report["database"] = {"quick_check": messages[:20]}
    if messages != ["ok"]:
        _problem(problems, "error", "database_corrupt", "SQLite quick_check reported problems", len(messages), messages)
    checkpoint()

    # 2. Originals: newly missing files, recovered files, broken symlinks, missing thumbnails
    progress(phase="files", processed=1, total=5)
    newly_missing, recovered, broken_links, thumbs_missing, originals_missing = [], [], [], [], []
    thumbs = s.config.data_dir / "thumbnails"
    last = 0
    checked = 0
    while True:
        batch = db.all("SELECT id, path, missing, status, original_path FROM media "
                       "WHERE deleted_at IS NULL AND id > ? ORDER BY id LIMIT 2000", (last,))
        if not batch:
            break
        for row in batch:
            path = Path(row["path"])
            exists = path.is_file()
            if path.is_symlink() and not path.exists():
                broken_links.append(row["id"])
            if not exists and not row["missing"]:
                newly_missing.append(row["id"])
            elif exists and row["missing"]:
                recovered.append(row["id"])
            if row["status"] == "indexed" and not (thumbs / f"media-{row['id']}.jpg").is_file():
                thumbs_missing.append(row["id"])
            if row["original_path"] and not Path(row["original_path"]).is_file():
                originals_missing.append(row["id"])
        checked += len(batch)
        last = batch[-1]["id"]
        checkpoint()
        progress(phase="files", processed=1, total=5, files_checked=checked)
    report["files"] = {"checked": checked, "newly_missing": len(newly_missing), "recovered": len(recovered),
                       "broken_links": len(broken_links), "thumbnails_missing": len(thumbs_missing)}
    if newly_missing:
        _problem(problems, "warning", "missing_files",
                 f"{_n(len(newly_missing), 'original is', 'originals are')} gone from disk but not yet marked missing "
                 "(run Cleanup > Check files to mark them)", len(newly_missing), newly_missing)
    if recovered:
        _problem(problems, "info", "recovered_files", f"{_n(len(recovered), 'file', 'files')} marked missing {'is' if len(recovered) == 1 else 'are'} back on disk",
                 len(recovered), recovered)
    if broken_links:
        _problem(problems, "warning", "broken_links", f"{_n(len(broken_links), 'media path is a broken symbolic link', 'media paths are broken symbolic links')}",
                 len(broken_links), broken_links)
    if thumbs_missing:
        _problem(problems, "info", "thumbnails_missing",
                 f"{_n(len(thumbs_missing), 'thumbnail is', 'thumbnails are')} missing (they regenerate on demand)", len(thumbs_missing),
                 thumbs_missing)
    if originals_missing:
        _problem(problems, "info", "soft_originals_missing",
                 f"{len(originals_missing)} recorded pre-conversion originals are no longer on disk",
                 len(originals_missing), originals_missing)

    # 3. Embedding stores (checksums, dangling offsets, torn tails)
    progress(phase="embeddings", processed=2, total=5)
    stores = {}
    for space_row in s.vectors.rows():
        space = s.vectors.get(space_row["key"])
        if space.spec.subject == "face":
            vrows = db.all("SELECT id AS key, embedding_offset AS offset, embedding_sha AS sha FROM faces")
        else:
            vrows = db.all("SELECT media_id AS key, offset, sha FROM media_vectors WHERE model_key=?", (space.key,))
        stores[space.key] = _check_vector_rows(vrows, space.file, verify_sample, space.key, problems, checkpoint)
    report["embeddings"] = stores

    # 4. ANN indexes on disk
    progress(phase="indexes", processed=3, total=5)
    indexes = {}
    for space_row in s.vectors.rows():
        space = s.vectors.get(space_row["key"])
        ann = space.ann
        entry: dict = {"path": str(ann.path), "exists": ann.path.is_file()}
        if ann.path.is_file():
            try:
                meta = json.loads(ann.meta_path.read_text())
                from usearch.index import Index

                index = Index.restore(str(ann.path))
                if index is None or len(index) != meta.get("count") or index.ndim != space.spec.dim:
                    raise ValueError(f"index has {None if index is None else len(index)} vectors, "
                                     f"metadata says {meta.get('count')}")
                entry.update(size=len(index), rows=space.count())
                if abs(len(index) - space.count()) > 0:
                    entry["stale"] = True
                    _problem(problems, "info", "index_stale",
                             f"{space.key}: index has {len(index)} vectors for {space.count()} rows "
                             "(it catches up automatically on next use)", 1, [space.key])
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                _problem(problems, "error", "index_corrupt",
                         f"{space.key}: saved vector index cannot be loaded ({exc}); rebuild it", 1, [space.key])
        indexes[space.key] = entry
        checkpoint()
    report["indexes"] = indexes

    # 5. Relational consistency
    progress(phase="references", processed=4, total=5)
    orphans = {
        "faces_without_media": db.one("SELECT COUNT(*) c FROM faces f LEFT JOIN media m ON m.id=f.media_id WHERE m.id IS NULL")["c"],
        "faces_with_unknown_person": db.one(
            "SELECT COUNT(*) c FROM faces f LEFT JOIN people p ON p.id=f.person_id WHERE f.person_id IS NOT NULL AND p.id IS NULL")["c"],
        "vectors_without_media": db.one(
            "SELECT COUNT(*) c FROM media_vectors v LEFT JOIN media m ON m.id=v.media_id WHERE m.id IS NULL")["c"],
    }
    report["references"] = orphans
    for kind, count in orphans.items():
        if count:
            _problem(problems, "warning", kind, f"{count} {kind.replace('_', ' ')}", count)

    report["problems"] = problems
    report["ok"] = not any(p["severity"] == "error" for p in problems)
    report["finished_at"] = _now()
    progress(phase="done", processed=5, total=5)
    target = s.config.data_dir / "health" / "last_integrity.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1))
    os.replace(tmp, target)
    return report


def last_integrity(services) -> Optional[dict]:
    path = services.config.data_dir / "health" / "last_integrity.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
