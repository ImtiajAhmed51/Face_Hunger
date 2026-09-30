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
            services.schedule_embedding_backfill()
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


def register_all(services) -> None:
    jobs = services.jobs
    jobs.register("ingest", make_ingest(services))
    jobs.register("embed_backfill", make_embed_backfill(services))
    jobs.register("rebuild_index", make_rebuild_index(services))


__all__ = ["register_all", "merge_paths", "ignorable", "media_kind", "library_for", "PRIORITY"]
