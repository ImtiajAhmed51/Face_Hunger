"""Process-wide service container.

Routers never construct services themselves; they read them from the active
container so tests can build an isolated app against a temporary data dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..clustering import Clustering
from ..config import Config
from ..db import Database
from ..embeddings import EmbeddingStore
from ..engine import Engine
from ..jobs.handlers import register_all as register_handlers
from ..jobs.manager import PRIORITY, JobManager
from ..jobs.watcher import LibraryWatcher
from ..ml.models import ModelHub
from ..ops.backup import apply_pending_restore
from ..vectors.spaces import Space, VectorSpaces
from ..vectors.specs import FACE_ARCFACE
from ..video_compat import init_video_compat
from ..worker import Worker
from .search import HybridSearch


class Services:
    def __init__(self, config: Config, engine: Optional[Engine] = None):
        config.prepare()
        self.config = config
        self.config_warnings = config.validate_runtime()
        # A restore staged by /api/backup/restore is swapped in before anything opens the files.
        self.restored = apply_pending_restore(config.data_dir)
        self.db = Database(config.data_dir / "index.sqlite")
        self.store = EmbeddingStore(config.data_dir / "embeddings.bin")
        self.engine = engine if engine is not None else Engine(config)
        self.cluster = Clustering(self.db, self.store)
        self.vectors = VectorSpaces(self.db, config.data_dir)
        self.models = ModelHub(config.model_dir, idle_seconds=config.model_idle_seconds,
                               dino_variant=config.dino_variant)
        for spec in self.models.installed_specs():
            self.vectors.register(spec)
        self.worker = self._make_worker()
        # File-level indexing for ingest jobs (shares the worker lock with full scans).
        self.ingest_worker = Worker(self.db, config, self.engine, self.store, self.cluster)
        self.ingest_worker.on_indexed = self.on_media_indexed
        self.search = HybridSearch(self)
        self.extra_embedders: dict = {}  # model key -> embedder (tests, plugins)
        self.jobs = JobManager(self.db)
        register_handlers(self)
        self.watcher = LibraryWatcher(self)
        self.models.start()
        self.video_compat = init_video_compat(
            config.data_dir / "video_cache",
            on_replaced=self._on_video_replaced,
        )

    def _on_video_replaced(
        self,
        media_id: int,
        new_path: Path,
        new_name: str,
        size: int,
        mtime_ns: int,
        width,
        height,
        duration,
        original_path=None,
    ) -> None:
        """Persist path/name/size after conversion; keep soft-original path for UI."""
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE media SET path=?, name=?, size=?, mtime_ns=?,
                       width=COALESCE(?, width), height=COALESCE(?, height),
                       duration=COALESCE(?, duration), error=NULL,
                       original_path=COALESCE(?, original_path)
                   WHERE id=?""",
                (
                    str(new_path),
                    new_name,
                    size,
                    mtime_ns,
                    width,
                    height,
                    duration,
                    str(original_path) if original_path else None,
                    media_id,
                ),
            )

    def _make_worker(self) -> Worker:
        worker = Worker(self.db, self.config, self.engine, self.store, self.cluster)
        worker.on_indexed = self.on_media_indexed
        worker.on_finished = lambda status: self.schedule_embedding_backfill() if status == "completed" else None
        return worker

    def start_background(self) -> None:
        """Start the job runner and file watcher (called from the app lifespan)."""
        self.jobs.start()
        if self.config.watch:
            self.watcher.start()
        self.schedule_embedding_backfill()

    def schedule_embedding_backfill(self) -> None:
        for space in self.embedding_spaces_for_backfill():
            if self.embedder_for(space.key) is not None and space.pending(1):
                self.jobs.enqueue("embed_backfill", {}, priority=PRIORITY["background"], dedupe_key="embed_backfill")
                return

    def embedder_for(self, key: str):
        if key in self.extra_embedders:
            return self.extra_embedders[key]
        for role in ("text_image", "visual"):
            embedder = self.models.embedder(role)
            if embedder is not None and embedder.spec.key == key:
                return embedder
        return None

    # -- embeddings ------------------------------------------------------
    def embedding_spaces_for_backfill(self) -> list[Space]:
        """Spaces whose model is installed (or injected) and can compute new vectors."""
        specs = {s.key: s for s in self.models.installed_specs()}
        specs.update({k: e.spec for k, e in self.extra_embedders.items()})
        return [self.vectors.register(spec) for spec in specs.values()]

    def visual_space(self):
        """Space used for visual similarity / near-duplicates (newest complete one)."""
        return self.vectors.active("visual")

    def on_media_indexed(self, media_id: int, content_changed: bool) -> None:
        """New or changed media: drop stale vectors and move it to the front of the backfill."""
        if content_changed:
            with self.db.connect() as conn:
                conn.execute("DELETE FROM media_vectors WHERE media_id=?", (media_id,))
        for space in self.embedding_spaces_for_backfill():
            space.prioritize([media_id], priority=100)

    def reopen_face_store(self) -> None:
        """Re-create the face embedding store and everything bound to it."""
        self.store.close()
        self.store = EmbeddingStore(self.config.data_dir / "embeddings.bin")
        self.vectors.forget(FACE_ARCFACE.key, drop_index=True)
        self.cluster = Clustering(self.db, self.store)
        self.worker = self._make_worker()
        self.ingest_worker = Worker(self.db, self.config, self.engine, self.store, self.cluster)
        self.ingest_worker.on_indexed = self.on_media_indexed

    def close(self) -> None:
        self.watcher.stop()
        self.jobs.stop()
        self.worker.shutdown(timeout=5)
        for closer in (self.store.close, self.vectors.close, self.models.close):
            try:
                closer()
            except Exception:
                pass


_current: Optional[Services] = None


def set_current(services: Services) -> None:
    global _current
    _current = services


def current() -> Services:
    if _current is None:
        raise RuntimeError("Services not initialised; call create_app() first")
    return _current
