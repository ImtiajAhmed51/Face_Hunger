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
from ..media_embeddings import MediaEmbeddingStore
from ..video_compat import init_video_compat
from ..worker import Worker


class Services:
    def __init__(self, config: Config, engine: Optional[Engine] = None):
        config.prepare()
        self.config = config
        self.db = Database(config.data_dir / "index.sqlite")
        self.store = EmbeddingStore(config.data_dir / "embeddings.bin")
        self.media_store = MediaEmbeddingStore(config.data_dir / "media_embeddings.bin")
        self.engine = engine if engine is not None else Engine(config)
        self.cluster = Clustering(self.db, self.store)
        self.worker = Worker(self.db, config, self.engine, self.store, self.cluster)
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

    def reopen_face_store(self) -> None:
        """Re-create the face embedding store and everything bound to it."""
        self.store.close()
        self.store = EmbeddingStore(self.config.data_dir / "embeddings.bin")
        self.cluster = Clustering(self.db, self.store)
        self.worker = Worker(self.db, self.config, self.engine, self.store, self.cluster)

    def close(self) -> None:
        self.worker.shutdown(timeout=5)
        for closer in (self.store.close, self.media_store.close):
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
