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


def _m9_media_metadata(conn: sqlite3.Connection) -> None:
    """Capture metadata for timeline/map; rows are filled by the metadata_backfill job."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
    for name, ddl in (("date_source", "TEXT"), ("gps_lat", "REAL"), ("gps_lon", "REAL"), ("gps_alt", "REAL"),
                      ("camera_make", "TEXT"), ("camera_model", "TEXT"), ("lens", "TEXT"),
                      ("meta_version", "INTEGER NOT NULL DEFAULT 0")):
        if name not in cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS media_geo ON media(gps_lat, gps_lon) WHERE gps_lat IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS media_meta_version ON media(meta_version)")
    # Timeline order: the same expression /api/media sorts by.
    conn.execute("CREATE INDEX IF NOT EXISTS media_timeline ON media(deleted_at, kind, "
                 "COALESCE(captured_at, indexed_at) DESC, id DESC)")


def _m10_quality(conn: sqlite3.Connection) -> None:
    """Raw quality signals (by signals version) and composite scores (by formula version)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(faces)")}
    if "landmarks" not in cols:
        conn.execute("ALTER TABLE faces ADD COLUMN landmarks TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_signals (
          media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          sharpness REAL, exposure REAL, noise REAL, face_quality REAL, eyes_open REAL, smile REAL,
          aesthetic REAL, faces INTEGER NOT NULL DEFAULT 0, error TEXT,
          computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_scores (
          media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
          formula_version INTEGER NOT NULL,
          score REAL NOT NULL, breakdown TEXT NOT NULL,
          PRIMARY KEY (media_id, formula_version)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS quality_scores_rank ON quality_scores(formula_version, score DESC)")


def _m11_events(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL,
          start_at TEXT NOT NULL, end_at TEXT NOT NULL,
          cover_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
          lat REAL, lon REAL, item_count INTEGER NOT NULL DEFAULT 0, people TEXT NOT NULL DEFAULT '[]',
          user_edited INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS events_time ON events(start_at DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_media (
          media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
          event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
          locked INTEGER NOT NULL DEFAULT 0
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS event_media_event ON event_media(event_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_edits (
          id INTEGER PRIMARY KEY, action TEXT NOT NULL, snapshot TEXT NOT NULL,
          created_events TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, undone_at TEXT
        )""")


def _m12_video(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_keyframes (
          id INTEGER PRIMARY KEY,
          media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
          t REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS video_keyframes_media ON video_keyframes(media_id, t)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keyframe_vectors (
          keyframe_id INTEGER PRIMARY KEY REFERENCES video_keyframes(id) ON DELETE CASCADE,
          model_key TEXT NOT NULL, offset INTEGER NOT NULL, sha TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS keyframe_vectors_model ON keyframe_vectors(model_key)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_analysis (
          media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, keyframes INTEGER NOT NULL DEFAULT 0, model_key TEXT, error TEXT,
          analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS faces_media_track ON faces(media_id, track_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS faces_person_time ON faces(person_id, media_id, timestamp)")


MIGRATIONS = [
    (6, "embedding_stores", _m6_embedding_stores),
    (7, "saved_searches", _m7_saved_searches),
    (8, "job_queue", _m8_job_queue),
    (9, "media_metadata", _m9_media_metadata),
    (10, "quality", _m10_quality),
    (11, "events", _m11_events),
    (12, "video", _m12_video),
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
