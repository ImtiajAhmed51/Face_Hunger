"""Recoverable per-video staging; originals and authoritative face rows stay untouched."""
import json
import sqlite3
from pathlib import Path

SCHEMA = '''CREATE TABLE IF NOT EXISTS detections(id INTEGER PRIMARY KEY,bbox TEXT,timestamp REAL,detection REAL,
 embedding_offset INTEGER,embedding_sha TEXT,jpeg BLOB,face_id INTEGER,quality REAL DEFAULT .5,track_id INTEGER,landmarks TEXT);
 CREATE INDEX IF NOT EXISTS checkpoint_timestamp ON detections(timestamp,id);
 CREATE TABLE IF NOT EXISTS checkpoint(key TEXT PRIMARY KEY,value BLOB);'''


class InvalidCheckpoint(ValueError):
    pass


def get(conn, key, default=None):
    row=conn.execute('SELECT value FROM checkpoint WHERE key=?',(key,)).fetchone()
    try:
        return json.loads(row[0]) if row else default
    except (ValueError,TypeError) as exc:
        raise InvalidCheckpoint('Invalid checkpoint metadata') from exc


def put(conn, key, value):
    conn.execute('INSERT INTO checkpoint(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key,json.dumps(value)))


class VideoCheckpoint:
    def __init__(self, data_dir, media_id, fingerprint):
        root=Path(data_dir)/'indexing_checkpoints'
        root.mkdir(parents=True,exist_ok=True)
        self.path=root/f'{media_id}.sqlite'
        self.fingerprint=fingerprint
        self.conn=None

    def __enter__(self):
        if self.path.is_symlink():
            raise ValueError('Video checkpoint must not be a symlink')
        try:
            self.conn=sqlite3.connect(self.path)
            self.conn.row_factory=sqlite3.Row
            self.conn.executescript(SCHEMA)
            if get(self.conn,'fingerprint') != self.fingerprint:
                self.conn.execute('DELETE FROM detections')
                self.conn.execute('DELETE FROM checkpoint')
                put(self.conn,'fingerprint',self.fingerprint)
                self.conn.commit()
            return self.conn
        except (sqlite3.DatabaseError,ValueError):
            if self.conn: self.conn.close()
            # Only this app-owned staging cache is regenerated.
            self.path.unlink(missing_ok=True)
            self.conn=sqlite3.connect(self.path)
            self.conn.row_factory=sqlite3.Row
            self.conn.executescript(SCHEMA)
            put(self.conn,'fingerprint',self.fingerprint);self.conn.commit()
            return self.conn

    def __exit__(self,*args):
        self.conn.close()

    def completed(self):
        self.path.unlink(missing_ok=True)


def source_fingerprint(path, stat, settings, engine, store):
    relevant={k:settings.get(k) for k in ('video_interval','adaptive_video','detection_size','multi_scale','min_face_quality')}
    metadata=engine.status().get('metadata')
    return dict(version=1,source=str(Path(path).resolve()),size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                inode=stat.st_ino,device=stat.st_dev,configuration=relevant,model=metadata,
                embedding_store=str(store.path),embedding_inode=store.path.stat().st_ino,embedding_device=store.path.stat().st_dev)
