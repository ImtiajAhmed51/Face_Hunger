"""Numerical fixtures test bookkeeping; @ai tests exercise only local real models."""

import hashlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from backend.clustering import Clustering
from backend.db import Database
from backend.embeddings import EmbeddingStore, VECTOR_BYTES, normalize
from backend.engine import Engine, deduplicate
from backend.media_processing import frame_at, load_image, video_frames
from backend.scanner import authorized_root, scan
from backend.worker import Worker


def vector(axis=0, secondary=None):
    result = np.zeros(512, dtype=np.float32)
    result[axis] = 1
    if secondary is not None:
        result[secondary] = 0.1
    return normalize(result)


@pytest.fixture
def pipeline(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    config = SimpleNamespace(data_dir=tmp_path / "data", model_dir=tmp_path / "models", roots=[library])
    db = Database(config.data_dir / "index.sqlite")
    with db.connect() as conn:
        lid = conn.execute("INSERT INTO libraries(path,name) VALUES (?,?)", (str(library), "Test")).lastrowid
    store = EmbeddingStore(config.data_dir / "embeddings.bin")
    cluster = Clustering(db, store)
    yield SimpleNamespace(config=config, db=db, store=store, cluster=cluster, library=library, lid=lid)
    store.close()


def media(p, name, kind="photo"):
    with p.db.connect() as conn:
        return conn.execute("INSERT INTO media(library_id,path,name,kind,size,mtime_ns,status) VALUES (?,?,?,?,1,1,'indexed')",
                            (p.lid, str(p.library / name), name, kind)).lastrowid


def face(p, mid, embedding, timestamp=None, person_id=None, **fields):
    offset, sha = p.store.append(embedding)
    with p.db.connect() as conn:
        fid = conn.execute("INSERT INTO faces(media_id,bbox,timestamp,detection,embedding_offset,embedding_sha,person_id) "
            "VALUES (?,'[0,0,10,10]',?,0.95,?,?,?)", (mid, timestamp, offset, sha, person_id)).lastrowid
        if person_id is None:
            person_id, similarity = p.cluster.assign(conn, embedding, mid, timestamp, fid)
            conn.execute("UPDATE faces SET person_id=?,similarity=? WHERE id=?", (person_id, similarity, fid))
        for key, value in fields.items():
            assert key in {"manual", "review_state", "deleted_at"}
            conn.execute(f"UPDATE faces SET {key}=? WHERE id=?", (value, fid))
        p.cluster.refresh(conn, [person_id])
    return fid, person_id


def test_embedding_roundtrip_checksum_and_batches(pipeline):
    p = pipeline
    offset, sha = p.store.append(vector())
    assert offset == 0
    assert p.store.path.stat().st_size == VECTOR_BYTES
    np.testing.assert_allclose(p.store.read(offset, sha), vector())
    offset2, sha2 = p.store.append(vector(1) * 3)
    assert offset2 == VECTOR_BYTES
    np.testing.assert_allclose(p.store.read(offset2, sha2), vector(1))
    for value in (np.zeros(512), np.ones(511), np.full(512, np.nan), np.ones((1, 512))):
        with pytest.raises(ValueError):
            p.store.append(value)
    for invalid_offset, invalid_sha in ((1, sha), (0, "bad"), (VECTOR_BYTES * 10, sha)):
        with pytest.raises(ValueError):
            p.store.read(invalid_offset, invalid_sha)
    mid = media(p, "a.png")
    face(p, mid, vector())
    assert len(list(p.store.iterate(p.db, batch_size=1))) == 1
    with p.store.path.open("r+b") as handle:
        handle.write(b"x")
    with pytest.raises(ValueError, match="checksum"):
        p.store.read(offset, sha)


def test_embedding_rejects_nonunit_and_truncated_records(tmp_path):
    path = tmp_path / "bad.bin"
    payload = np.zeros(512, dtype="<f4").tobytes()
    path.write_bytes(payload)
    with EmbeddingStore(path) as store:
        with pytest.raises(ValueError, match="unit"):
            store.read(0, hashlib.sha256(payload).hexdigest())
    path.write_bytes(b"broken")
    with EmbeddingStore(path) as store:
        with pytest.raises(ValueError, match="truncated"):
            store.append(vector())


def test_dedup_is_iou_not_identity():
    detections = [{"bbox": [0, 0, 10, 10], "detection": 0.9},
                  {"bbox": [1, 0, 10, 10], "detection": 0.8},
                  {"bbox": [30, 0, 10, 10], "detection": 0.9}]
    result = deduplicate(detections)
    assert len(result) == 2 and result.duplicate_count == 1
    assert deduplicate(result).duplicate_count == 1


@pytest.mark.parametrize("use_faiss", [True, False])
def test_matching_cannot_collapse_two_faces_in_one_frame(pipeline, use_faiss):
    p = pipeline
    if not use_faiss:
        p.cluster._faiss = None
    mid = media(p, "group.png")
    _, a = face(p, mid, vector())
    _, b = face(p, mid, vector())
    assert a != b
    video = media(p, "clip.mp4", "video")
    _, first = face(p, video, vector(3), timestamp=0)
    _, later = face(p, video, vector(3), timestamp=3)
    _, other = face(p, video, vector(3), timestamp=3)
    assert first == later and other != later


def test_refresh_and_invalidation_ignore_deleted_excluded_rejected(pipeline):
    p = pipeline
    one, two = media(p, "one.png"), media(p, "two.png")
    fid, pid = face(p, one, vector())
    face(p, two, vector(1), person_id=pid)
    with p.db.connect() as conn:
        conn.execute("INSERT INTO exclusions VALUES (?,?)", (pid, two))
        p.cluster.refresh(conn, [pid])
    row = p.db.one("SELECT * FROM people WHERE id=?", (pid,))
    assert row["face_count"] == 1 and row["representative_face_id"] == fid
    np.testing.assert_allclose(np.frombuffer(row["centroid"], dtype="<f4"), vector())
    with p.db.connect() as conn:
        conn.execute("UPDATE media SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (one,))
    p.cluster.invalidate()
    _, new = face(p, media(p, "new.png"), vector())
    assert new != pid
    assert p.db.one("SELECT face_count FROM people WHERE id=?", (pid,))["face_count"] == 0
    assert len(list(p.store.iterate(p.db))) == 1


def test_face_rejections_and_media_exclusions_block_matching(pipeline):
    p = pipeline
    _, pid = face(p, media(p, "first.png"), vector())
    mid = media(p, "excluded.png")
    with p.db.connect() as conn:
        conn.execute("INSERT INTO exclusions VALUES (?,?)", (pid, mid))
    p.cluster.invalidate()
    _, other = face(p, mid, vector())
    assert other != pid
    third = media(p, "rejected.png")
    offset, sha = p.store.append(vector())
    with p.db.connect() as conn:
        fid = conn.execute("INSERT INTO faces(media_id,bbox,detection,embedding_offset,embedding_sha) "
                           "VALUES (?,'[0,0,10,10]',0.9,?,?)", (third, offset, sha)).lastrowid
        conn.execute("INSERT INTO rejections VALUES (?,?)", (fid, pid))
        result, _ = p.cluster.assign(conn, vector(), third, face_id=fid)
        assert result != pid


@pytest.mark.parametrize("use_faiss", [True, False])
def test_reconcile_merges_only_unprotected_noncooccurring_people(pipeline, use_faiss):
    p = pipeline
    if not use_faiss:
        p.cluster._faiss = None
    people = []
    for i in range(5):
        with p.db.connect() as conn:
            pid = conn.execute("INSERT INTO people DEFAULT VALUES").lastrowid
        face(p, media(p, f"person-{i}.png"), vector(0, 1), person_id=pid)
        people.append(pid)
    with p.db.connect() as conn:
        conn.execute("UPDATE people SET name='Named' WHERE id=?", (people[2],))
        conn.execute("UPDATE faces SET manual=1 WHERE person_id=?", (people[3],))
        conn.execute("UPDATE faces SET review_state='confirmed' WHERE person_id=?", (people[4],))
    p.cluster.invalidate()
    progress = []
    result = p.cluster.reconcile(callback=progress.append)
    assert result["merged"] == 1 and progress[-1]["processed"] == 2
    assert p.db.one("SELECT face_count FROM people WHERE id=?", (people[0],))["face_count"] == 2
    for pid in people[2:]:
        assert p.db.one("SELECT face_count FROM people WHERE id=?", (pid,))["face_count"] == 1


def test_reconcile_respects_cooccurrence_and_separation(pipeline):
    p = pipeline
    mid = media(p, "twins.png")
    _, a = face(p, mid, vector())
    _, b = face(p, mid, vector())
    assert p.cluster.reconcile()["merged"] == 0
    with p.db.connect() as conn:
        conn.execute("INSERT INTO separate_people VALUES (?,?)", (min(a, b), max(a, b)))
    p.cluster.invalidate()
    assert p.cluster.reconcile()["total"] == 0


def test_scanner_security_and_ignores(pipeline, tmp_path):
    p = pipeline
    (p.library / "photo.jpg").write_bytes(b"fixture")
    ignored = p.library / "ignore"
    ignored.mkdir()
    (ignored / "private.png").write_bytes(b"fixture")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.jpg").write_bytes(b"fixture")
    (p.library / "escape").symlink_to(outside, target_is_directory=True)
    (p.library / "escape.jpg").symlink_to(outside / "external.jpg")
    assert list(scan({"path": str(p.library), "ignored": '["ignore"]'})) == [(p.library / "photo.jpg", "photo")]
    with pytest.raises(ValueError, match="authorized"):
        authorized_root(outside, [p.library])


def test_exif_orientation_and_bgr(tmp_path):
    path = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (20, 10), (255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)
    bgr = load_image(path)
    assert bgr.shape == (20, 10, 3) and bgr.flags.c_contiguous
    assert bgr[0, 0, 2] > 240 and bgr[0, 0, 0] < 10


class NumericalEngine:
    """Only test fixtures: no synthetic detections are shipped by the product."""
    def __init__(self):
        self.failure = False
        self.entered = self.release = None

    def configure(self, detection_size=None):
        self.detection_size = detection_size

    def load(self):
        return {"state": "ready"}

    def detect(self, image):
        if self.entered:
            self.entered.set()
            assert self.release.wait(10)
        if self.failure:
            raise ValueError("deliberate numerical-fixture failure")
        return [{"bbox": [1, 1, 12, 12], "detection": 0.99, "embedding": vector()},
                {"bbox": [2, 1, 12, 12], "detection": 0.8, "embedding": vector()}]


def wait_job(worker):
    worker._thread.join(15)
    assert not worker.busy
    return worker.latest()


def test_worker_incremental_atomic_retry_thumbnails_and_missing(pipeline):
    p = pipeline
    path = p.library / "photo.png"
    Image.new("RGB", (40, 40), (255, 0, 0)).save(path)
    engine = NumericalEngine()
    worker = Worker(p.db, p.config, engine, p.store, p.cluster)
    try:
        worker.start(p.lid)
        job = wait_job(worker)
        assert job["status"] == "completed" and job["processed"] == 1 and job["faces"] == 1
        indexed = p.db.one("SELECT * FROM media")
        old_face = p.db.one("SELECT * FROM faces")
        assert indexed["duplicate_count"] == 1 and Path(indexed["thumbnail"]).is_file()
        assert Path(old_face["thumbnail"]).is_file()
        worker.start(p.lid)
        assert wait_job(worker)["skipped"] == 1
        engine.failure = True
        Image.new("RGB", (40, 40), (0, 255, 0)).save(path)
        worker.start(p.lid)
        job = wait_job(worker)
        assert job["failed"] == 1 and "deliberate" in job["error"]
        assert p.db.one("SELECT * FROM faces") == old_face
        assert p.db.one("SELECT status FROM media")["status"] == "failed"
        engine.failure = False
        worker.start(p.lid)
        assert wait_job(worker)["failed"] == 0
        assert p.db.one("SELECT id FROM faces")["id"] == old_face["id"]
        path.unlink()
        worker.start(p.lid)
        assert wait_job(worker)["status"] == "completed"
        assert p.db.one("SELECT missing FROM media")["missing"] == 1
        assert p.db.one("SELECT face_count FROM people")["face_count"] == 0
    finally:
        worker.shutdown()


def test_worker_pause_cancel_guard_and_no_early_missing(pipeline):
    p = pipeline
    Image.new("RGB", (40, 40)).save(p.library / "photo.png")
    missing = media(p, "no-longer-exists.png")
    engine = NumericalEngine()
    engine.entered, engine.release = threading.Event(), threading.Event()
    worker = Worker(p.db, p.config, engine, p.store, p.cluster)
    other = Worker(p.db, p.config, engine, p.store, p.cluster)
    try:
        job = worker.start(p.lid)
        assert engine.entered.wait(10)
        with pytest.raises(RuntimeError, match="already running"):
            other.start(p.lid)
        assert worker.control(job["id"], "pause")["status"] == "paused"
        assert worker.control(job["id"], "resume")["status"] == "running"
        worker.control(job["id"], "cancel")
        engine.release.set()
        assert wait_job(worker)["status"] == "cancelled"
        assert p.db.one("SELECT missing FROM media WHERE id=?", (missing,))["missing"] == 0
        assert p.db.one("SELECT COUNT(*) n FROM faces")["n"] == 0
    finally:
        engine.release.set()
        worker.shutdown()
        other.shutdown()


def test_force_preserves_confirmed_named_and_deleted_faces(pipeline):
    p = pipeline
    Image.new("RGB", (40, 40)).save(p.library / "photo.png")
    worker = Worker(p.db, p.config, NumericalEngine(), p.store, p.cluster)
    try:
        worker.start(p.lid)
        wait_job(worker)
        original = p.db.one("SELECT * FROM faces")
        with p.db.connect() as conn:
            conn.execute("UPDATE people SET name='Protected' WHERE id=?", (original["person_id"],))
            conn.execute("UPDATE faces SET manual=1,review_state='confirmed',deleted_at=CURRENT_TIMESTAMP WHERE id=?", (original["id"],))
        p.cluster.invalidate()
        worker.start(p.lid, force=True)
        assert wait_job(worker)["status"] == "completed"
        current = p.db.one("SELECT * FROM faces")
        assert current["id"] == original["id"] and current["person_id"] == original["person_id"]
        assert current["manual"] == 1 and current["review_state"] == "confirmed" and current["deleted_at"]
        assert p.db.one("SELECT COUNT(*) n FROM people")["n"] == 1
    finally:
        worker.shutdown()


def test_video_sampling_has_no_60_frame_cap(tmp_path):
    import cv2

    path = tmp_path / "long.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (32, 32))
    if not writer.isOpened():
        pytest.skip("OpenCV build lacks the MJPEG test encoder")
    try:
        for i in range(650):
            writer.write(np.full((32, 32, 3), i % 255, dtype=np.uint8))
    finally:
        writer.release()
    stamps = [stamp for stamp, _ in video_frames(path, interval=1)]
    assert len(stamps) == 65 and stamps[-1] >= 64
    assert frame_at(path, 62).shape == (32, 32, 3)


def test_engine_local_missing_models_status_and_configure(tmp_path):
    engine = Engine(SimpleNamespace(model_dir=tmp_path))
    assert engine.status()["state"] == "unloaded"
    engine.configure(detection_size=960)
    with pytest.raises(RuntimeError, match="Automatic model downloads are disabled"):
        engine.load()
    assert engine.status()["state"] == "error" and engine.status()["provider"] is None
    assert str(tmp_path / "buffalo_l" / "det_10g.onnx") in engine.status()["error"]


@pytest.mark.ai
def test_real_local_buffalo_detection_and_reuse(monkeypatch):
    model_dir, image = os.environ.get("LFS_TEST_MODEL_DIR"), os.environ.get("LFS_TEST_IMAGE")
    if not model_dir or not image:
        pytest.skip("Set LFS_TEST_MODEL_DIR (base containing buffalo_l) and LFS_TEST_IMAGE; no downloads")
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("The local AI engine attempted an external connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    engine = Engine(SimpleNamespace(model_dir=Path(model_dir)))
    status = engine.load()
    assert status["state"] == "ready" and status["provider"] in status["available_providers"]
    identities = id(engine._detector), id(engine._recognizer)
    engine.configure(detection_size=320)
    assert (id(engine._detector), id(engine._recognizer)) == identities
    detections = engine.detect(load_image(image))
    assert detections, "Real AI fixture must contain a detectable face"
    for detection in detections:
        assert len(detection["bbox"]) == 4 and detection["bbox"][2] > 0
        assert detection["embedding"].shape == (512,)
        np.testing.assert_allclose(np.linalg.norm(detection["embedding"]), 1, atol=1e-5)
    assert engine.load()["provider"] == engine._recognizer.session.get_providers()[0]
    assert (id(engine._detector), id(engine._recognizer)) == identities


def test_worker_rolls_back_partial_publication(pipeline, monkeypatch):
    p = pipeline
    Image.new("RGB", (40, 40)).save(p.library / "photo.png")
    engine = NumericalEngine()
    worker = Worker(p.db, p.config, engine, p.store, p.cluster)
    try:
        worker.start(p.lid)
        wait_job(worker)
        old = p.db.one("SELECT * FROM faces")
        old_thumbnail = Path(old["thumbnail"]).read_bytes()
        monkeypatch.setattr(engine, "detect", lambda image: [
            {"bbox": [25, 25, 12, 12], "detection": 0.99, "embedding": vector(5)}])
        assign = p.cluster.assign

        def fail_after_assignment(*args, **kwargs):
            assign(*args, **kwargs)
            raise ValueError("publication interrupted")

        monkeypatch.setattr(p.cluster, "assign", fail_after_assignment)
        worker.start(p.lid, force=True)
        assert wait_job(worker)["failed"] == 1
        assert p.db.one("SELECT * FROM faces") == old
        assert Path(old["thumbnail"]).read_bytes() == old_thumbnail
        assert p.db.one("SELECT COUNT(*) n FROM people")["n"] == 1
    finally:
        worker.shutdown()


def test_assign_derives_media_and_rejects_inactive_faces(pipeline):
    p = pipeline
    mid = media(p, "a.png")
    fid, _ = face(p, mid, vector())
    with p.db.connect() as conn:
        conn.execute("UPDATE media SET missing=1 WHERE id=?", (mid,))
    p.cluster.invalidate()
    with p.db.connect() as conn:
        with pytest.raises(ValueError, match="inactive"):
            p.cluster.assign(conn, vector(), face_id=fid)


@pytest.mark.parametrize("available", [
    ["CUDAExecutionProvider", "CPUExecutionProvider"],
    ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    ["DmlExecutionProvider", "CPUExecutionProvider"],
])
def test_engine_checks_actual_session_provider_and_serializes_load(tmp_path, monkeypatch, available):
    import sys
    from concurrent.futures import ThreadPoolExecutor

    pack = tmp_path / "buffalo_l"
    pack.mkdir()
    for filename in ("det_10g.onnx", "w600k_r50.onnx"):
        (pack / filename).write_bytes(b"mock-loader fixture, not an ONNX product model")
    calls = []

    def get_model(path, **options):
        assert options["download"] is False and Path(path).parent == pack
        calls.append(options["providers"])
        # Compiled accelerators are advertised but cannot initialize on this fixture.
        return SimpleNamespace(taskname="detection" if "det_" in path else "recognition",
            prepare=lambda **kwargs: None, session=SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"]),
            detect=lambda image: None, get_feat=lambda image: vector()[None, :])

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        get_available_providers=lambda: available, SessionOptions=SimpleNamespace,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=0)))
    monkeypatch.setitem(sys.modules, "insightface.model_zoo", SimpleNamespace(get_model=get_model))
    engine = Engine(SimpleNamespace(model_dir=tmp_path))
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: engine.load(), range(4)))
    assert all(status["provider"] == "CPUExecutionProvider" for status in statuses)
    assert len(calls) == 4  # Two failed accelerator sessions, two reused CPU sessions.
    engine.configure(detection_size=960)
    assert engine._detector.input_size == (960, 960) and len(calls) == 4


@pytest.mark.ai
def test_real_photo_and_video_worker(pipeline):
    import shutil

    model_dir = os.environ.get("LFS_TEST_MODEL_DIR")
    image, video = os.environ.get("LFS_TEST_IMAGE"), os.environ.get("LFS_TEST_VIDEO")
    if not all((model_dir, image, video)):
        pytest.skip("Set LFS_TEST_MODEL_DIR, LFS_TEST_IMAGE, and LFS_TEST_VIDEO to local real fixtures")
    p = pipeline
    shutil.copyfile(image, p.library / Path(image).name)
    shutil.copyfile(video, p.library / Path(video).name)
    p.config.model_dir = Path(model_dir)
    engine = Engine(p.config)
    worker = Worker(p.db, p.config, engine, p.store, p.cluster)
    try:
        worker.start(p.lid)
        worker._thread.join(90)
        assert not worker.busy
        job = worker.latest()
        assert job["status"] == "completed" and job["failed"] == 0 and job["processed"] == 2, job
        assert all(row["status"] == "indexed" for row in p.db.all("SELECT * FROM media"))
        assert p.db.one("SELECT COUNT(*) n FROM faces f JOIN media m ON m.id=f.media_id WHERE m.kind='photo'")["n"] > 0
        assert p.db.one("SELECT COUNT(*) n FROM faces f JOIN media m ON m.id=f.media_id WHERE m.kind='video'")["n"] > 0
        for row, embedding in p.store.iterate(p.db):
            assert np.isfinite(embedding).all() and Path(row["thumbnail"]).is_file()
    finally:
        worker.shutdown()
