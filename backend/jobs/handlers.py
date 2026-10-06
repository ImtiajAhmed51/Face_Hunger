"""Job handlers. Each is idempotent so a crash-recovered job can simply re-run."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..scanner import IMAGE, VIDEO, authorized_root
from ..vectors.spaces import run_backfill
from .manager import PRIORITY, Cancelled, JobContext

logger = logging.getLogger(__name__)


def media_kind(path: Path):
    suffix = path.suffix.lower()
    return "photo" if suffix in IMAGE else "video" if suffix in VIDEO else None


def ignorable(path: Path) -> bool:
    name = path.name
    return name.startswith(".") or name.endswith(".lfs_original") or ".lfs_converted" in name or media_kind(path) is None


def library_for(db, config, path: Path):
    """The registered library containing ``path`` (deepest match) and its canonical root."""
    best = None
    for lib in db.all("SELECT * FROM libraries"):
        try:
            root = authorized_root(lib["path"], config.roots)
        except Exception:
            continue
        if path == root or path.is_relative_to(root):
            if best is None or len(str(root)) > len(str(best[1])):
                best = (lib, root)
    return best


def _excluded(lib, root: Path, path: Path) -> bool:
    import fnmatch

    patterns = [str(p).replace("\\", "/").strip("/") for p in json.loads(lib.get("ignored") or "[]") if str(p).strip("/")]
    rel = path.relative_to(root).as_posix()
    return any(rel == p or rel.startswith(p + "/") or fnmatch.fnmatchcase(rel, p)
               or ("/" not in p and any(fnmatch.fnmatchcase(part, p) for part in path.relative_to(root).parts))
               for p in patterns)


def merge_paths(old: dict, new: dict) -> dict:
    paths = list(dict.fromkeys([*old.get("paths", []), *new.get("paths", [])]))
    return {**old, "paths": paths}


def make_ingest(services):
    """Index specific files (from the watcher or an API call) without a full scan."""

    def handler(ctx: JobContext):
        paths = [str(p) for p in ctx.payload.get("paths", [])]
        done = set(ctx.progress_state.get("done_paths", []))
        worker = services.ingest_worker
        # Share the legacy scanner's lock so file-level and full-library indexing never interleave.
        while True:
            ctx.checkpoint()
            try:
                worker._acquire_guard()
                break
            except RuntimeError:
                time.sleep(0.2)
        worker._checkpoint = ctx.checkpoint

        def cancel_only():
            if ctx.cancelled():
                raise Cancelled("Cancelled before media commit")

        worker._cancel_only = cancel_only
        worker.on_video_progress = lambda path, seconds, duration: ctx.progress(
            current=str(path), video_seconds=round(seconds, 1), video_duration=round(duration, 1))
        indexed = missing = failed = skipped = 0
        try:
            settings = services.db.settings()
            loaded = False
            for raw in paths:
                if raw in done:
                    continue
                ctx.checkpoint()
                path = Path(raw)
                ctx.progress(current=raw, processed=len(done), total=len(paths))
                if not path.exists():
                    with services.db.connect() as conn:
                        missing += conn.execute("UPDATE media SET missing=1 WHERE path=? AND missing=0", (raw,)).rowcount
                    services.cluster.invalidate()
                else:
                    found = library_for(services.db, services.config, path.resolve())
                    kind = media_kind(path)
                    if found is None or kind is None or ignorable(path) or _excluded(found[0], found[1], path.resolve()):
                        skipped += 1
                    else:
                        if not loaded:
                            services.engine.configure(detection_size=settings.get("detection_size", 640),
                                                      multi_scale=bool(settings.get("multi_scale", False)))
                            services.engine.load()
                            loaded = True
                        lib, root = found
                        try:
                            outcome, _ = worker._index_file(lib, root, path.resolve(), kind, settings, False, False)
                            indexed += outcome == "indexed"
                            skipped += outcome == "skipped"
                        except Cancelled:
                            raise
                        except Exception as exc:
                            if type(exc).__name__ in ("Cancelled", "_Shutdown"):
                                raise
                            failed += 1
                            worker._record_failure(lib["id"], path.resolve(), kind, f"{type(exc).__name__}: {exc}")
                done.add(raw)
                ctx.progress(done_paths=sorted(done), processed=len(done), total=len(paths),
                             indexed=indexed, missing=missing, failed=failed, skipped=skipped)
        finally:
            worker._release_guard()
            services.cluster.invalidate()
        if indexed:
            services.after_indexing()
        return {"processed": len(done), "total": len(paths), "indexed": indexed, "missing": missing,
                "failed": failed, "skipped": skipped, "current": None}

    return handler


def make_embed_backfill(services):
    def handler(ctx: JobContext):
        wanted = ctx.payload.get("key")
        totals = {"embedded": 0, "failed": 0}
        for space in services.embedding_spaces_for_backfill():
            if wanted and space.key != wanted:
                continue
            embedder = services.embedder_for(space.key)
            if embedder is None:
                continue
            base = dict(totals)

            def report(p, key=space.key, base=base):
                ctx.progress(model=key, processed=p["filled"], total=p["total"],
                             embedded=base["embedded"] + p["embedded"], failed=base["failed"] + p["failed"])

            ctx.progress(force=True, model=space.key, current=None)
            result = run_backfill(space, embedder, checkpoint=ctx.checkpoint, progress=report,
                                  should_yield=ctx.should_yield, batch_size=8)
            totals["embedded"] += result["embedded"]
            totals["failed"] += result["failed"]
            if result["yielded"]:
                return {**totals, "yielded": True}
        return totals

    return handler


def make_rebuild_index(services):
    def handler(ctx: JobContext):
        ctx.checkpoint()
        key = ctx.payload.get("key")
        result = services.vectors.get(key).sync(force_rebuild=True)
        return {**result, "processed": result["size"], "total": result["size"]}

    return handler


def make_integrity_check(services):
    from ..ops.health import integrity_check

    def handler(ctx: JobContext):
        report = integrity_check(services, checkpoint=ctx.checkpoint, progress=lambda **p: ctx.progress(**p),
                                 verify_sample=int(ctx.payload.get("verify_sample", 5000)))
        return {"ok": report["ok"], "problems": len(report["problems"]), "processed": 5, "total": 5}

    return handler


def make_backup_export(services):
    from ..ops.backup import export_backup

    def handler(ctx: JobContext):
        result = export_backup(services.db, services.config.data_dir,
                               include_thumbnails=bool(ctx.payload.get("include_thumbnails")),
                               checkpoint=ctx.checkpoint, progress=lambda **p: ctx.progress(**p))
        return {"name": result["name"], "bytes": result["bytes"], "current": None}

    return handler


def make_backup_restore(services):
    from ..ops.backup import stage_restore

    def handler(ctx: JobContext):
        name = ctx.payload["name"]
        path = services.config.data_dir / "exports" / name
        result = stage_restore(path, services.config.data_dir, checkpoint=ctx.checkpoint)
        return {**result, "current": None}

    return handler


def make_metadata_backfill(services):
    """Fill capture date / GPS / camera for media indexed before migration 9 (resumable:
    progress is the meta_version column, so a restarted job continues where it stopped)."""
    from .. import metadata as metadata_mod

    def handler(ctx: JobContext):
        db = services.db
        total = db.one("SELECT COUNT(*) c FROM media WHERE deleted_at IS NULL")["c"]
        done = 0
        while True:
            ctx.checkpoint()
            if ctx.should_yield():
                return {"yielded": True, "processed": total - _pending(db), "total": total}
            rows = db.all("SELECT id, path, kind, mtime_ns, captured_at FROM media WHERE deleted_at IS NULL "
                          "AND meta_version < ? ORDER BY id LIMIT 100", (metadata_mod.META_VERSION,))
            if not rows:
                break
            updates = []
            for row in rows:
                ctx.checkpoint()
                path = Path(row["path"])
                if path.is_file():
                    try:
                        meta = metadata_mod.extract(path, row["kind"], mtime_ns=row["mtime_ns"])
                    except Exception:
                        meta = None
                else:
                    meta = None
                if meta is None:  # unreadable/missing: keep what we have, never lose an existing date
                    meta = {k: None for k in metadata_mod.COLUMNS}
                    meta["meta_version"] = metadata_mod.META_VERSION
                    if row["captured_at"]:
                        meta["captured_at"], meta["date_source"] = row["captured_at"], "exif"
                    elif row["mtime_ns"]:
                        from datetime import datetime
                        meta["captured_at"] = datetime.fromtimestamp(row["mtime_ns"] / 1e9).replace(microsecond=0).isoformat()
                        meta["date_source"] = "mtime"
                updates.append((*(meta[k] for k in metadata_mod.COLUMNS), row["id"]))
            with db.connect() as conn:
                conn.executemany("UPDATE media SET " + ",".join(f"{k}=?" for k in metadata_mod.COLUMNS) + " WHERE id=?",
                                 updates)
            done += len(rows)
            ctx.progress(processed=total - _pending(db), total=total, updated=done)
        return {"processed": total, "total": total, "updated": done}

    def _pending(db):
        return db.one("SELECT COUNT(*) c FROM media WHERE deleted_at IS NULL AND meta_version < ?",
                      (metadata_mod.META_VERSION,))["c"]

    return handler


def make_quality_scoring(services):
    def handler(ctx: JobContext):
        return services.quality.run(ctx.checkpoint, lambda **p: ctx.progress(**p), ctx.should_yield)

    return handler


def make_event_detection(services):
    """Full detection the first time (or on request), then incremental from a media-id watermark."""
    def handler(ctx: JobContext):
        db = services.db
        settings = db.settings()
        watermark = settings.get("events_watermark")
        top = db.one("SELECT COALESCE(MAX(id), 0) AS m FROM media")["m"]
        from ..services.events import EVENTS_VERSION

        full = bool(ctx.payload.get("full")) or watermark is None or settings.get("events_version") != EVENTS_VERSION
        ctx.progress(force=True, phase="full" if full else "incremental", processed=0, total=1)
        result = services.events.detect(since_media_id=None if full else int(watermark), checkpoint=ctx.checkpoint)
        with db.connect() as conn:
            for key, value in (("events_watermark", top), ("events_version", EVENTS_VERSION)):
                conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        return {**{k: v for k, v in result.items() if k != "window"}, "processed": 1, "total": 1, "full": full}

    return handler


def make_video_analysis(services):
    def handler(ctx: JobContext):
        return services.video.run(ctx.checkpoint, lambda **p: ctx.progress(**p), ctx.should_yield)

    return handler


def make_sidecar_sync(services):
    """A .xmp changed on disk: import it if it is not what we last wrote."""
    def handler(ctx: JobContext):
        changed = []
        for raw in ctx.payload.get("paths", []):
            ctx.checkpoint()
            media_id = services.edits.reconcile_path(Path(raw))
            if media_id is not None:
                changed.append(media_id)
        return {"processed": len(ctx.payload.get("paths", [])), "total": len(ctx.payload.get("paths", [])),
                "changed": changed}

    return handler


def make_share_export(services):
    """Anonymized copies for sharing; resumable from the job's saved progress."""
    def handler(ctx: JobContext):
        return services.sharing.run(ctx.id, ctx.payload, ctx.progress_state, checkpoint=ctx.checkpoint,
                                    progress=ctx.progress)

    return handler


def make_package_export(services):
    def handler(ctx: JobContext):
        passphrase = services.packages.take(ctx.payload["secret"])
        return services.packages.export(ctx.payload["scope"], passphrase, include_media=bool(ctx.payload.get("include_media")),
                                        include_thumbnails=bool(ctx.payload.get("include_thumbnails", True)),
                                        checkpoint=ctx.checkpoint, progress=lambda **p: ctx.progress(**p))

    return handler


def make_package_import(services):
    def handler(ctx: JobContext):
        passphrase = services.packages.take(ctx.payload["secret"])
        source = services.packages.resolve(ctx.payload["source"])
        return services.packages.import_package(source, passphrase, conflict=ctx.payload.get("conflict", "keep_both"),
                                                checkpoint=ctx.checkpoint, progress=lambda **p: ctx.progress(**p))

    return handler


def register_all(services) -> None:
    jobs = services.jobs
    jobs.register("package_export", make_package_export(services))
    jobs.register("package_import", make_package_import(services))
    jobs.register("share_export", make_share_export(services))
    jobs.register("sidecar_sync", make_sidecar_sync(services))
    jobs.register("video_analysis", make_video_analysis(services))
    jobs.register("event_detection", make_event_detection(services))
    jobs.register("quality_scoring", make_quality_scoring(services))
    jobs.register("metadata_backfill", make_metadata_backfill(services))
    jobs.register("integrity_check", make_integrity_check(services))
    jobs.register("backup_export", make_backup_export(services))
    jobs.register("backup_restore", make_backup_restore(services))
    jobs.register("ingest", make_ingest(services))
    jobs.register("embed_backfill", make_embed_backfill(services))
    jobs.register("rebuild_index", make_rebuild_index(services))


__all__ = ["register_all", "merge_paths", "ignorable", "media_kind", "library_for", "PRIORITY"]
