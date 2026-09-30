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
from ..ml.models import ModelHub
from ..vectors.spaces import Space, VectorSpaces
from ..vectors.specs import FACE_ARCFACE
from ..video_compat import init_video_compat
from ..worker import Worker
from .search import HybridSearch


class Services:
    def __init__(self, config: Config, engine: Optional[Engine] = None):
        config.prepare()
        self.config = config
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
        self.search = HybridSearch(self)
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
        return worker

    # -- embeddings ------------------------------------------------------
    def embedding_spaces_for_backfill(self) -> list[Space]:
        """Spaces whose model is installed and can compute new vectors."""
        return [self.vectors.register(spec) for spec in self.models.installed_specs()]

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

    def close(self) -> None:
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
