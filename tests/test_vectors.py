"""Multi-model embedding store: add/remove/rebuild, upgrades, crash resume, migration."""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from backend.db import Database
from backend.vectors.file import VectorFile
from backend.vectors.spaces import VectorSpaces, run_backfill
from backend.vectors.specs import LEGACY_DINO
from tests.helpers.fixtures import FAKE_VISUAL, FAKE_VISUAL_V2, FakeEmbedder, add_media, fake_vector

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "index.sqlite")
    spaces = VectorSpaces(db, tmp_path)
    yield db, spaces, tmp_path
    spaces.close()


def test_vector_file_roundtrip_and_tail_repair(tmp_path):
    path = tmp_path / "v.f32"
    with VectorFile(path, 8) as f:
        (o1, s1), (o2, s2) = f.append_many([np.ones(8), np.arange(1, 9)])
        assert (o1, o2) == (0, 32)
        assert np.allclose(f.read(o2, s2), np.arange(1, 9) / np.linalg.norm(np.arange(1, 9)))
        with pytest.raises(ValueError, match="checksum"):
            f.read(o1, s2)
    with path.open("ab") as raw:  # simulate a torn write
        raw.write(b"\x00" * 13)
    with VectorFile(path, 8) as f:
        assert f.repaired_bytes == 13 and f.records == 2
        assert f.append(np.ones(8))[0] == 64


def test_add_search_remove_and_rebuild(env):
    db, spaces, data_dir = env
    ids = add_media(db, 200)
    space = spaces.register(FAKE_VISUAL)
    space.ensure_ready()
    space.add([(i, fake_vector(i)) for i in ids])
    keys, sims = space.ann.search(fake_vector(42), 5)
    assert keys[0] == 42 and sims[0] > 0.99
    assert space.coverage() == {"filled": 200, "total": 200, "ratio": 1.0}

    space.remove([42])
    assert 42 not in space.ann.search(fake_vector(42), 5)[0]
    assert space.count() == 199

    # Re-embedding replaces the row (new id) and the index vector.
    space.add([(7, fake_vector(7, salt=1))])
    assert space.ann.search(fake_vector(7, salt=1), 1)[0][0] == 7
    space.maybe_save(force=True)

    # Rebuild from the source of truth after the index files disappear.
    spaces.forget(FAKE_VISUAL.key, drop_index=True)
    fresh = spaces.get(FAKE_VISUAL.key)
    assert fresh.sync()["action"] == "rebuild"
    assert len(fresh.ann) == 199
    assert fresh.ann.search(fake_vector(7, salt=1), 1)[0][0] == 7
    assert 42 not in fresh.ann.keys()


def test_index_catches_up_on_reload_without_rebuild(env):
    db, spaces, _ = env
    ids = add_media(db, 50)
    space = spaces.register(FAKE_VISUAL)
    space.ensure_ready()
    space.add([(i, fake_vector(i)) for i in ids[:30]])
    space.maybe_save(force=True)
    # Rows written by another process after the index was saved.
    spaces.forget(FAKE_VISUAL.key)
    other = VectorSpaces(db, env[2]).register(FAKE_VISUAL)
    other.add([(i, fake_vector(i)) for i in ids[30:]])
    other.file.close()
    again = spaces.get(FAKE_VISUAL.key)
    result = again.sync()
    assert result["action"] == "catch_up" and result["added"] == 20 and len(again.ann) == 50


def test_backfill_is_resumable_prioritised_and_records_failures(env):
    db, spaces, _ = env
    ids = add_media(db, 40)
    space = spaces.register(FAKE_VISUAL)
    space.prioritize([3, 5], priority=100)
    embedder = FakeEmbedder(FAKE_VISUAL, fail_ids={9})
    first = run_backfill(space, embedder, batch_size=4, max_items=4)
    assert first["embedded"] == 4
    done = {r["media_id"] for r in db.all("SELECT media_id FROM media_vectors WHERE model_key=?", (space.key,))}
    assert {3, 5} <= done  # priority first
    rest = run_backfill(space, embedder, batch_size=8)
    assert rest["failed"] == 1 and space.count() == 39
    # Failures retry up to MAX_ATTEMPTS then stop being pending.
    for _ in range(3):
        run_backfill(space, embedder, batch_size=8)
    assert space.pending(10) == [] and space.count() == 39
    assert set(ids) - {9} == {r["media_id"] for r in db.all("SELECT media_id FROM media_vectors")}


def test_model_upgrade_keeps_old_store_until_new_one_is_complete(env):
    db, spaces, _ = env
    ids = add_media(db, 100)
    v1 = spaces.register(FAKE_VISUAL)
    run_backfill(v1, FakeEmbedder(FAKE_VISUAL), batch_size=50)
    v2 = spaces.register(FAKE_VISUAL_V2)
    assert v1.key != v2.key
    run_backfill(v2, FakeEmbedder(FAKE_VISUAL_V2, salt=2), batch_size=10, max_items=50)
    assert spaces.active("visual").key == v1.key
    assert v1.ann.search(fake_vector(12), 1)[0][0] == 12  # old index untouched
    run_backfill(v2, FakeEmbedder(FAKE_VISUAL_V2, salt=2), batch_size=50)
    assert spaces.active("visual").key == v2.key
    assert v2.ann.search(fake_vector(12, salt=2), 1)[0][0] == 12
    assert v1.count() == len(ids)  # upgrade never deleted v1


def test_kill_9_mid_backfill_resumes_without_duplicates(env):
    db, spaces, data_dir = env
    ids = add_media(db, 150)
    spaces.register(FAKE_VISUAL)
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests/helpers/backfill_proc.py"), str(data_dir)],
                            stdout=subprocess.PIPE, text=True, cwd=ROOT)
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.strip().isdigit() and int(line) >= 30:
            break
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    partial = db.one("SELECT COUNT(*) c FROM media_vectors")["c"]
    assert 30 <= partial < 150, partial

    # Restart: reopen everything from disk and finish.
    spaces.forget(FAKE_VISUAL.key)
    space = VectorSpaces(Database(data_dir / "index.sqlite"), data_dir).register(FAKE_VISUAL)
    run_backfill(space, FakeEmbedder(FAKE_VISUAL), batch_size=16)
    rows = db.all("SELECT media_id, offset, sha FROM media_vectors WHERE model_key=?", (FAKE_VISUAL.key,))
    assert sorted(r["media_id"] for r in rows) == ids  # every item exactly once
    assert len({r["offset"] for r in rows}) == len(rows)
    for r in rows:
        assert np.allclose(space.file.read(r["offset"], r["sha"]), fake_vector(r["media_id"]), atol=1e-6)
    assert space.file.records >= len(rows)
    assert len(space.ann) == 150 and space.ann.search(fake_vector(77), 1)[0][0] == 77


def test_migration_backs_up_and_adopts_legacy_dino_vectors(tmp_path):
    path = tmp_path / "index.sqlite"
    db = Database(path)
    add_media(db, 3)
    with db.connect() as conn:
        conn.execute("UPDATE media SET dino_offset=id*3072, dino_sha='x' || id")
        # Pretend this database predates the versioned migrations.
        for table in ("schema_migrations", "media_vectors", "embedding_queue", "embedding_models", "saved_searches"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=5")
    migrated = Database(path)
    assert migrated.applied_migrations == ["embedding_stores", "saved_searches", "job_queue"]
    backups = list((tmp_path / "backups").glob("index-pre-v6-*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as old:
        assert old.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 3
    rows = migrated.all("SELECT media_id, offset FROM media_vectors WHERE model_key=?", (LEGACY_DINO.key,))
    assert [(r["media_id"], r["offset"]) for r in rows] == [(1, 3072), (2, 6144), (3, 9216)]
    assert migrated.one("PRAGMA user_version")["user_version"] == 8
    # Idempotent: a second start applies nothing and makes no new backup.
    assert Database(path).applied_migrations == []
    assert len(list((tmp_path / "backups").glob("*.sqlite"))) == 1
