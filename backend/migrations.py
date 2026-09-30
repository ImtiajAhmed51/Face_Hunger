"""Versioned, idempotent schema migrations with an automatic backup first.

``Database._migrate`` still performs the historical additive fixes (schema
versions <= 5). Everything newer lives here as an ordered list. Before any
pending migration touches an existing database, a consistent copy is written
with SQLite's online backup API to ``<data_dir>/backups/``. Every migration
must be safe to re-run (``IF NOT EXISTS`` / ``INSERT OR IGNORE``) so a crash
between the DDL and the version bump cannot wedge startup.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_VERSION = 5  # last version produced by Database._migrate


def _m6_embedding_stores(conn: sqlite3.Connection) -> None:
    """Side-by-side embedding stores keyed by (model_id, version, dim)."""
    from .vectors.specs import FACE_ARCFACE, LEGACY_DINO

    conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_models (
          key TEXT PRIMARY KEY,
          model_id TEXT NOT NULL, version TEXT NOT NULL, dim INTEGER NOT NULL,
          subject TEXT NOT NULL CHECK(subject IN ('media','face')),
          role TEXT NOT NULL,
          file TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(model_id, version, dim)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_vectors (
          id INTEGER PRIMARY KEY,
          model_key TEXT NOT NULL REFERENCES embedding_models(key) ON DELETE CASCADE,
          media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
          offset INTEGER NOT NULL, sha TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(model_key, media_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS media_vectors_media ON media_vectors(media_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_queue (
          model_key TEXT NOT NULL, media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
          priority INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          PRIMARY KEY(model_key, media_id)
        )""")
    for spec, file in ((LEGACY_DINO, "media_embeddings.bin"), (FACE_ARCFACE, "embeddings.bin")):
        conn.execute(
            "INSERT OR IGNORE INTO embedding_models(key,model_id,version,dim,subject,role,file) VALUES (?,?,?,?,?,?,?)",
            (spec.key, spec.model_id, spec.version, spec.dim, spec.subject, spec.role, file),
        )
    # Adopt the existing DINOv2 (torch hub) vectors without copying bytes.
    conn.execute(
        """INSERT OR IGNORE INTO media_vectors(model_key, media_id, offset, sha)
           SELECT ?, id, dino_offset, dino_sha FROM media
           WHERE dino_offset IS NOT NULL AND dino_sha IS NOT NULL ORDER BY id""",
        (LEGACY_DINO.key,),
    )


def _m7_saved_searches(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_searches (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_run_at TEXT, run_count INTEGER NOT NULL DEFAULT 0
        )""")


def _m8_job_queue(conn: sqlite3.Connection) -> None:
    """Generic priority job queue on the existing jobs table."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in (
        ("kind", "TEXT NOT NULL DEFAULT 'index'"),
        ("priority", "INTEGER NOT NULL DEFAULT 50"),
        ("payload", "TEXT NOT NULL DEFAULT '{}'"),
        ("progress", "TEXT NOT NULL DEFAULT '{}'"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("started_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("dedupe_key", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, priority, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key, status)")


MIGRATIONS = [
    (6, "embedding_stores", _m6_embedding_stores),
    (7, "saved_searches", _m7_saved_searches),
    (8, "job_queue", _m8_job_queue),
]
LATEST = MIGRATIONS[-1][0]


def backup_database(conn: sqlite3.Connection, db_path: Path, label: str) -> Path:
    folder = Path(db_path).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = folder / f"{Path(db_path).stem}-{label}-{stamp}.sqlite"
    n = 1
    while target.exists():
        target = folder / f"{Path(db_path).stem}-{label}-{stamp}-{n}.sqlite"
        n += 1
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def run(conn: sqlite3.Connection, db_path: Path, had_data: bool) -> list[str]:
    """Apply pending migrations. Returns the names applied."""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    todo = [m for m in MIGRATIONS if m[0] not in done]
    if not todo:
        conn.execute(f"PRAGMA user_version={int(max(done))}")
        return []
    if had_data:
        conn.commit()
        path = backup_database(conn, db_path, f"pre-v{todo[0][0]}")
        logger.info("Database backed up to %s before migrating to v%s", path, todo[-1][0])
    applied = []
    for version, name, fn in todo:
        fn(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?,?)", (version, name))
        conn.execute(f"PRAGMA user_version={int(version)}")
        conn.commit()
        applied.append(name)
    return applied
