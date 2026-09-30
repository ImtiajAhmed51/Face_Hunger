"""Health & ops: request ids, health, config validation, integrity check, backup/restore."""

import json
import logging
import os
import zipfile

import pytest
from PIL import Image

from backend.config import Config, ConfigError
from backend.ops.backup import BackupError, stage_restore
from backend.ops.health import last_integrity
from backend.ops.logging import JsonFormatter, RequestIdFilter, request_id
from backend.services.container import Services
from tests.conftest import FakeEngine
from tests.helpers.fixtures import FAKE_VISUAL, fake_vector


def test_request_ids_are_assigned_and_echoed(client):
    r = client.get("/api/health")
    assert len(r.headers["x-request-id"]) == 16
    assert client.get("/api/health", headers={"X-Request-ID": "trace-me-42"}).headers["x-request-id"] == "trace-me-42"


def test_json_logs_carry_the_request_id():
    token = request_id.set("abc123")
    try:
        record = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "hello %s", "args": ("x",),
                                        "path": "/api/x"})
        RequestIdFilter().filter(record)
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id.reset(token)
    assert line["msg"] == "hello x" and line["request_id"] == "abc123" and line["path"] == "/api/x"


def test_health_summary(client):
    h = client.get("/api/health").json()
    assert h["status"] in ("ok", "degraded")
    from backend.migrations import LATEST
    assert h["checks"]["database"]["ok"] and h["checks"]["database"]["schema_version"] == LATEST
    assert h["checks"]["jobs"]["ok"] and h["checks"]["watcher"]["running"]


def test_config_validation(tmp_path):
    with pytest.raises(ValueError):
        Config(port=70000)
    with pytest.raises(ValueError):
        Config(dino_variant="giant")
    locked = tmp_path / "locked"
    locked.mkdir()
    cfg = Config(data_dir=locked, model_dir=tmp_path / "m", allowed_roots=str(tmp_path / "nope"), host="0.0.0.0")
    cfg.data_dir = locked.resolve()
    warnings = cfg.validate_runtime()
    assert any("does not exist" in w for w in warnings) and any("buffalo_l" in w for w in warnings)
    assert any("network" in w for w in warnings)
    os.chmod(locked, 0o500)
    try:
        with pytest.raises(ConfigError, match="not writable"):
            cfg.validate_runtime()
    finally:
        os.chmod(locked, 0o700)


@pytest.fixture
def fixture_library(app_services, tmp_path):
    """Three real photos with rows, faces, face vectors and a visual space with a saved index."""
    s = app_services
    lib = tmp_path / "lib"
    lib.mkdir()
    with s.db.connect() as conn:
        lid = conn.execute("INSERT INTO libraries(path, name) VALUES (?, 'lib')", (str(lib),)).lastrowid
        conn.execute("INSERT INTO people(id, name, face_count) VALUES (1, 'Ada', 3)")
    for i in range(1, 4):
        path = lib / f"p{i}.jpg"
        Image.new("RGB", (40, 30), (i * 60, 20, 20)).save(path)
        Image.new("RGB", (40, 30), (i * 60, 20, 20)).save(s.config.data_dir / "thumbnails" / f"media-{i}.jpg")
        offset, sha = s.store.append(fake_vector(i, 512))
        with s.db.connect() as conn:
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status) "
                         "VALUES (?,?,?,?, 'photo', ?, 0, 'indexed')", (i, lid, str(path), path.name, path.stat().st_size))
            conn.execute("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha) "
                         "VALUES (?, 1, '[0,0,5,5]', 0.9, ?, ?)", (i, offset, sha))
    space = s.vectors.register(FAKE_VISUAL)
    space.ensure_ready()
    space.add([(i, fake_vector(i, FAKE_VISUAL.dim)) for i in range(1, 4)])
    space.maybe_save(force=True)
    return s, lib, space


def run_check(s):
    job = s.jobs.enqueue("integrity_check", {"verify_sample": 100000})
    s.jobs.start()
    done = s.jobs.wait(job["id"], timeout=30)
    assert done["status"] == "completed", done
    return last_integrity(s)


def kinds(report):
    return {p["kind"]: p for p in report["problems"]}


def test_integrity_check_clean_library(fixture_library):
    s, _, _ = fixture_library
    report = run_check(s)
    assert report["ok"], report["problems"]
    assert report["files"]["checked"] == 3
    assert report["embeddings"][FAKE_VISUAL.key]["checksum_failures"] == 0


def test_integrity_check_detects_deleted_file_and_corruption(fixture_library):
    s, lib, space = fixture_library
    (lib / "p2.jpg").unlink()  # deliberately deleted original
    with open(space.ann.path, "r+b") as f:  # corrupt the saved ANN index
        f.seek(0)
        f.write(b"\x00garbage" * 64)
    data = bytearray(space.file.path.read_bytes())  # flip bytes of one stored vector
    data[FAKE_VISUAL.dim * 4 + 3] ^= 0xFF
    space.file.path.write_bytes(bytes(data))
    report = run_check(s)
    found = kinds(report)
    assert not report["ok"]
    assert found["missing_files"]["sample"] == [2]
    assert found["index_corrupt"]["sample"] == [FAKE_VISUAL.key]
    assert found["vector_checksum"]["sample"] == [2]
    # Reported, not changed: the row is not silently marked missing.
    assert s.db.one("SELECT missing FROM media WHERE id=2")["missing"] == 0


def test_library_health_endpoint(client, fixture_library):
    body = client.get("/api/health/library").json()
    assert body["counts"]["media"] == 3 and body["counts"]["people"] == 1
    assert body["storage"]["thumbnails"]["files"] == 3 and body["storage"]["total_bytes"] > 0
    r = client.post("/api/health/check", json={"verify_sample": 100})
    assert r.json()["kind"] == "integrity_check"


def snapshot(s):
    tables = {t: s.db.all(f"SELECT * FROM {t} ORDER BY 1") for t in ("people", "faces", "media", "media_vectors", "libraries")}
    blobs = {name: (s.config.data_dir / name).read_bytes() for name in ("embeddings.bin",)}
    blobs["vectors"] = sorted((p.name, p.read_bytes()) for p in (s.config.data_dir / "vectors").glob("*.f32"))
    return tables, blobs


def test_backup_restore_round_trip(client, fixture_library, make_config):
    s, lib, space = fixture_library
    before = snapshot(s)
    job = client.post("/api/backup/export").json()
    done = s.jobs.wait(job["id"], timeout=30)
    assert done["status"] == "completed", done
    name = json.loads(done["progress"])["name"]
    assert [b["name"] for b in client.get("/api/backup").json()["items"]] == [name]
    assert client.get(f"/api/backup/files/{name}").status_code == 200
    assert client.get("/api/backup/files/..%2Findex.sqlite").status_code in (400, 404)

    # Change everything after the backup.
    with s.db.connect() as conn:
        conn.execute("UPDATE people SET name='Changed'")
        conn.execute("DELETE FROM faces WHERE media_id=3")
    space.add([(3, fake_vector(3, FAKE_VISUAL.dim, salt=9))])
    s.store.append(fake_vector(99, 512))

    staged = client.post("/api/backup/restore", json={"name": name}).json()
    done = s.jobs.wait(staged["id"], timeout=30)
    assert done["status"] == "completed" and json.loads(done["progress"])["restart_required"]
    assert client.post("/api/backup/restore", json={"name": "../../etc/passwd"}).status_code == 400

    # "Restart": close everything, start a new Services on the same data dir.
    s.close()
    restarted = Services(make_config(), engine=FakeEngine())
    try:
        assert restarted.restored["restored_files"] >= 3
        after = snapshot(restarted)
        assert after == before
        aside = restarted.config.data_dir / "backups"
        previous = [p for p in aside.iterdir() if p.name.startswith("pre-restore-")]
        assert len(previous) == 1 and (previous[0] / "index.sqlite").is_file()
        # The rebuilt index answers queries over the restored vectors.
        restored_space = restarted.vectors.get(FAKE_VISUAL.key)
        restored_space.sync(force_rebuild=True)
        assert restored_space.ann.search(fake_vector(3, FAKE_VISUAL.dim), 1)[0][0] == 3
    finally:
        restarted.close()


def test_damaged_backup_is_rejected(fixture_library, tmp_path):
    from backend.ops.backup import export_backup
    s, _, _ = fixture_library
    result = export_backup(s.db, s.config.data_dir)
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(result["path"]) as src, zipfile.ZipFile(damaged, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "embeddings.bin":
                data = data[:-1] + bytes([data[-1] ^ 1])
            dst.writestr(item, data)
    with pytest.raises(BackupError, match="damaged"):
        stage_restore(damaged, s.config.data_dir)
    assert not (s.config.data_dir / "restore-pending").exists()
