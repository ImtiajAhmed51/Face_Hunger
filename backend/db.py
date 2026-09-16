import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 ignored TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS media (
 id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
 path TEXT NOT NULL UNIQUE, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('photo','video')),
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, captured_at TEXT,
 width INTEGER, height INTEGER, duration REAL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 thumbnail TEXT, duplicate_count INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0,
 deleted_at TEXT, indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS people (
 id INTEGER PRIMARY KEY, name TEXT, centroid BLOB, face_count INTEGER NOT NULL DEFAULT 0,
 representative_face_id INTEGER, variance REAL NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS faces (
 id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
 person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
 bbox TEXT NOT NULL, timestamp REAL, detection REAL NOT NULL,
 similarity REAL, embedding_offset INTEGER NOT NULL, embedding_sha TEXT NOT NULL,
 thumbnail TEXT, review_state TEXT NOT NULL DEFAULT 'unreviewed'
 CHECK(review_state IN ('unreviewed','confirmed','rejected')),
 manual INTEGER NOT NULL DEFAULT 0, deleted_at TEXT,
 quality REAL NOT NULL DEFAULT 0.5,
 track_id INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS exclusions (
 person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
 media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
 PRIMARY KEY(person_id, media_id)
);
CREATE TABLE IF NOT EXISTS separate_people (
 person_a INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
 person_b INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
 PRIMARY KEY(person_a, person_b), CHECK(person_a < person_b)
);
CREATE TABLE IF NOT EXISTS rejections (
 face_id INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
 person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
 PRIMARY KEY(face_id, person_id)
);
CREATE TABLE IF NOT EXISTS hard_negatives (
 id INTEGER PRIMARY KEY,
 face_id INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
 person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
 similarity REAL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(face_id, person_id)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'queued', phase TEXT NOT NULL DEFAULT 'queued',
 total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
 faces INTEGER NOT NULL DEFAULT 0, people INTEGER NOT NULL DEFAULT 0,
 skipped INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
 current_file TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 finished_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS media_library ON media(library_id, status);
CREATE INDEX IF NOT EXISTS media_date ON media(captured_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS media_kind_date ON media(kind, deleted_at, captured_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS faces_person_media ON faces(person_id, deleted_at, media_id);
CREATE INDEX IF NOT EXISTS faces_media ON faces(media_id, deleted_at);
CREATE INDEX IF NOT EXISTS faces_review ON faces(review_state, deleted_at, similarity);
CREATE INDEX IF NOT EXISTS media_status ON media(status, missing);
CREATE INDEX IF NOT EXISTS faces_embedding ON faces(embedding_offset);
CREATE INDEX IF NOT EXISTS hard_negatives_person ON hard_negatives(person_id);
-- faces_track index is created in _migrate after track_id column is ensured
PRAGMA user_version=2;
"""

DEFAULTS = {
    "matching_threshold": 0.48,
    "review_threshold": 0.62,
    "detection_size": 640,
    "video_interval": 3.0,
    "theme": "system",
    "local_only": True,
    "multi_scale": False,
    "adaptive_video": True,
    # Drop weak detections earlier so they never hit the review queue.
    "min_face_quality": 0.35,
    # When true, high-confidence matches skip the review queue.
    "auto_confirm": True,
    # Auto-confirm at this cosine similarity (can be lower than review_threshold).
    # Default 0.52 sits between matching (0.48) and review (0.62).
    "auto_confirm_threshold": 0.52,
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
            for key, value in DEFAULTS.items():
                conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            conn.execute("UPDATE jobs SET status='interrupted', phase='interrupted', "
                         "error='Application stopped. Start a scan to resume unchanged-file recovery.' "
                         "WHERE status IN ('running','paused','queued')")

    def _migrate(self, conn):
        """Additive migrations for DBs created before quality/variance/hard_negatives."""
        cols_faces = {r[1] for r in conn.execute("PRAGMA table_info(faces)")}
        if "quality" not in cols_faces:
            conn.execute("ALTER TABLE faces ADD COLUMN quality REAL NOT NULL DEFAULT 0.5")
        if "track_id" not in cols_faces:
            conn.execute("ALTER TABLE faces ADD COLUMN track_id INTEGER")
        cols_people = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
        if "variance" not in cols_people:
            conn.execute("ALTER TABLE people ADD COLUMN variance REAL NOT NULL DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hard_negatives (
             id INTEGER PRIMARY KEY,
             face_id INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
             person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
             similarity REAL,
             created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
             UNIQUE(face_id, person_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS hard_negatives_person ON hard_negatives(person_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS faces_track ON faces(media_id, track_id)")
        conn.execute("PRAGMA user_version=2")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all(self, sql, params=()):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def settings(self):
        return {r["key"]: json.loads(r["value"]) for r in self.all("SELECT * FROM settings")}