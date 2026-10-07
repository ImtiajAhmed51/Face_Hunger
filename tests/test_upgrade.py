"""Upgrades from real older data folders: one written by the Phase 1 code (schema 8) and one by the
Phase 2 code (schema 13). See scripts/make_upgrade_fixture.py for how they were produced."""

import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import migrations
from backend.app import create_app
from backend.services.container import Services
from tests.conftest import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "upgrade"
LATEST = max(m[0] for m in migrations.MIGRATIONS)


def schema_of(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()


def facts(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "people": [tuple(r) for r in conn.execute("SELECT id, name, face_count FROM people ORDER BY id")],
            "media": [tuple(r) for r in conn.execute("SELECT id, path, name, kind, size FROM media ORDER BY id")],
            "faces": [tuple(r) for r in conn.execute("SELECT media_id, person_id, embedding_offset, embedding_sha FROM faces ORDER BY id")],
            "saved": [tuple(r) for r in conn.execute("SELECT name, query FROM saved_searches ORDER BY id")],
            "threshold": conn.execute("SELECT value FROM settings WHERE key='matching_threshold'").fetchone()[0],
        }
    finally:
        conn.close()


@pytest.mark.parametrize("phase,schema", [("phase1", 8), ("phase2", 13)])
def test_upgrade_from_an_older_data_dir(make_config, tmp_path, phase, schema):
    config = make_config()
    data = Path(config.data_dir)
    shutil.copytree(FIXTURES / phase, data)
    old_db = (FIXTURES / phase / "index.sqlite").read_bytes()
    old_vectors = (FIXTURES / phase / "embeddings.bin").read_bytes()
    before = facts(data / "index.sqlite")
    assert schema_of(data / "index.sqlite") == schema
    assert len(before["media"]) == 40 and len(before["faces"]) == 12

    services = Services(config, engine=FakeEngine())
    try:
        # Migrated to the latest schema, with every step recorded.
        assert services.db.one("PRAGMA user_version")["user_version"] == LATEST
        applied = [r["version"] for r in services.db.all("SELECT version FROM schema_migrations ORDER BY version")]
        assert applied[-1] == LATEST and set(range(schema + 1, LATEST + 1)) <= set(applied)
        # An automatic backup of the old database was taken first, and it is the old database.
        backups = sorted((data / "backups").glob(f"index-pre-v{schema + 1}-*.sqlite"))
        assert len(backups) == 1
        assert schema_of(backups[0]) == schema
        assert facts(backups[0]) == before
        # Nothing the user had was lost or rewritten.
        assert facts(data / "index.sqlite") == before
        assert (data / "embeddings.bin").read_bytes() == old_vectors
        first = before["faces"][0]
        assert np.isclose(np.linalg.norm(services.store.read(first[2], first[3])), 1.0, atol=1e-3)
        space = services.vectors.get("fixture-visual@1:16")
        assert space.coverage(max_age=0)["filled"] == 40 and space.vector(7) is not None

        # The app works on the upgraded data, old API paths included, and the new features start empty.
        with TestClient(create_app(services=services)) as client:
            client.headers.update({"X-LFS-Request": "1"})
            assert client.get("/api/health").status_code == 200
            people = client.get("/api/people").json()
            names = {p["name"] for p in (people["items"] if isinstance(people, dict) else people)}
            assert {"Ada", "Grace"} <= names
            assert client.get("/api/media?limit=100").json()["total"] == 40
            assert client.post("/api/search/hybrid", json={"people": [1], "limit": 50}).json()["total"] == 6
            saved = client.get("/api/search/saved").json()["items"]
            assert saved[0]["name"] == "Ada in photos"
            assert client.post(f"/api/search/saved/{saved[0]['id']}/run").json()["total"] == 6
            assert client.post("/api/search/hybrid", json={"similar_media_id": 7, "limit": 5}).json()["items"]
            assert client.get("/api/settings").json()["matching_threshold"] == 0.47
            assert client.get("/api/media/1/edits").status_code == 200
            assert client.get("/api/plugins").json()["items"] == []
            assert client.get("/api/diagnostics").json()["enabled"] is False
            assert client.get("/api/lock").json()["enabled"] is False
            assert client.get("/api/storage").json()["library"]["items"] == 40
            albums = client.get("/api/albums").json()["items"]
            assert ([a["name"] for a in albums] == ["Summer"]) if phase == "phase2" else albums == []
            created = client.post("/api/albums", json={"name": "After upgrade", "media_ids": [1, 2]})
            assert created.status_code == 200
    finally:
        services.close()

    # Idempotent: a second start changes nothing and takes no second backup.
    again = Services(config, engine=FakeEngine())
    try:
        assert again.db.one("PRAGMA user_version")["user_version"] == LATEST
        assert len(list((data / "backups").glob("index-pre-v*.sqlite"))) == 1
    finally:
        again.close()
    assert (FIXTURES / phase / "index.sqlite").read_bytes() == old_db  # the fixture itself is never modified
