"""Storage dashboard estimates vs bytes actually freed, and the resumable model-upgrade flow."""

import hashlib
import os
import time
from pathlib import Path

import numpy as np
import pytest

from backend.services.container import Services
from tests.conftest import FakeEngine
from tests.helpers.fixtures import FAKE_VISUAL, FAKE_VISUAL_V2, FakeEmbedder, add_media, fake_vector


def folder_bytes(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(root) for f in fs)


@pytest.fixture
def cluttered(client, app_services, tmp_path):
    """Real files: screenshots, blurry and dark photos, an old low-res video, big files, normal photos."""
    s = app_services
    lib = tmp_path / "lib"
    lib.mkdir()
    rng = np.random.default_rng(8)
    rows = []

    def add(name, size, **cols):
        path = lib / name
        path.write_bytes(rng.bytes(size))
        rows.append({"path": str(path), "name": name, "size": size, "kind": "photo", "captured_at": "2025-05-01T10:00:00",
                     "width": 4000, "height": 3000, "camera_make": "Cam", **cols})

    for i in range(4):
        add(f"Screenshot 2025-01-0{i + 1} at 10.00.00.png", 30_000 + i * 1000, camera_make=None, width=1170, height=2532)
    add("IMG_9000.PNG", 41_000, camera_make=None, width=1179, height=2556)          # screen-shaped PNG without a camera
    add("diagram.png", 20_000, camera_make=None, width=800, height=600)             # a PNG, but not a screenshot
    for i in range(5):
        add(f"blurry_{i}.jpg", 50_000 + i * 500)
    for i in range(3):
        add(f"dark_{i}.jpg", 25_000)
    add("old_clip.mp4", 400_000, kind="video", captured_at="2019-03-01T10:00:00", width=640, height=360)
    add("new_clip.mp4", 300_000, kind="video", captured_at="2026-03-01T10:00:00", width=640, height=360)
    for i in range(6):
        add(f"good_{i}.jpg", 60_000)
    add("huge_panorama.jpg", 900_000)
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'lib')", (str(lib),))
        for i, r in enumerate(rows, start=1):
            # One indexed size is stale on purpose: estimates must use what is really on disk.
            size = r["size"] + (5000 if r["name"] == "blurry_0.jpg" else 0)
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, width, height, camera_make, "
                         "status) VALUES (?,1,?,?,?,?,1,?,?,?,?,'indexed')",
                         (i, r["path"], r["name"], r["kind"], size, r["captured_at"], r["width"], r["height"], r["camera_make"]))
            sharp = 0.1 if r["name"].startswith("blurry") else 0.8
            exposure = 0.05 if r["name"].startswith("dark") else 0.9
            conn.execute("INSERT INTO quality_signals(media_id, version, sharpness, exposure, noise) VALUES (?,1,?,?,0.9)",
                         (i, sharp, exposure))
    return s, lib


def test_categories_find_the_right_items(client, cluttered):
    def names(cat):
        return sorted(i["name"] for i in client.get(f"/api/storage/{cat}").json()["items"])

    assert names("screenshots") == sorted([f"Screenshot 2025-01-0{i} at 10.00.00.png" for i in range(1, 5)] + ["IMG_9000.PNG"])
    assert names("blurry") == [f"blurry_{i}.jpg" for i in range(5)]
    assert names("dark") == [f"dark_{i}.jpg" for i in range(3)]
    assert names("old_video") == ["old_clip.mp4"]
    assert client.get("/api/storage/large").json()["items"][0]["name"] == "huge_panorama.jpg"
    assert client.get("/api/storage/nonsense").status_code == 404
    dash = client.get("/api/storage").json()
    by = {c["category"]: c for c in dash["categories"]}
    assert by["screenshots"]["count"] == 5 and by["blurry"]["count"] == 5 and dash["clutter"]["count"] == 14
    assert dash["library"]["items"] == 23


def test_savings_estimate_matches_freed_bytes_and_cleanup_is_undoable(client, cluttered):
    s, lib = cluttered
    ids = []
    for cat in ("screenshots", "blurry", "dark", "old_video"):
        ids += [i["id"] for i in client.get(f"/api/storage/{cat}").json()["items"]]
    estimate = client.post("/api/storage/estimate", json={"media_ids": ids}).json()
    assert estimate["count"] == 14
    before = folder_bytes(lib)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in lib.iterdir()}
    result = client.post("/api/storage/cleanup", json={"media_ids": ids, "free_space": True}).json()
    freed = before - folder_bytes(lib)
    assert freed > 0 and abs(estimate["bytes"] - freed) / freed <= 0.02, (estimate["bytes"], freed)
    assert result["estimated_bytes"] == estimate["bytes"] == freed == result["moved_bytes"]  # exact, in fact
    assert s.db.one("SELECT COUNT(*) c FROM media WHERE deleted_at IS NOT NULL")["c"] == 14
    assert client.get("/api/storage").json()["clutter"]["count"] == 0
    assert client.post(f"/api/audit/{result['audit_id']}/undo").status_code == 200
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in lib.iterdir()} == hashes
    assert client.post("/api/storage/cleanup", json={"media_ids": []}).status_code == 400


# -- model upgrade ------------------------------------------------------------------------------

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_model_upgrade_resumes_after_crash_and_rolls_back_without_touching_the_old_index(make_config):
    cfg = make_config(watch=False)
    s = Services(cfg, engine=FakeEngine())
    ids = add_media(s.db, 240)
    old = s.vectors.register(FAKE_VISUAL)
    old.add([(i, fake_vector(i, FAKE_VISUAL.dim, salt=1)) for i in ids])
    old.maybe_save(force=True)
    old_file = cfg.data_dir / "vectors" / f"{FAKE_VISUAL.slug}.f32"
    old_hash = sha(old_file)
    old_rows = s.db.all("SELECT media_id, offset, sha FROM media_vectors WHERE model_key=? ORDER BY media_id", (FAKE_VISUAL.key,))

    slow = FakeEmbedder(FAKE_VISUAL_V2, delay=0.05, salt=2)
    s.extra_embedders[FAKE_VISUAL_V2.key] = slow
    s.vectors.register(FAKE_VISUAL_V2)
    with pytest.raises(ValueError, match="not installed"):
        s.storage.start_upgrade(FAKE_VISUAL.key)  # nothing can embed v1 any more
    s.jobs.start()
    status = s.storage.start_upgrade(FAKE_VISUAL_V2.key)["upgrade"]
    assert status["status"] == "building" and status["from_key"] == FAKE_VISUAL.key
    assert s.visual_space().key == FAKE_VISUAL.key  # search keeps using the current model
    with pytest.raises(ValueError, match="wait for it to finish"):
        s.storage.switch()
    deadline = time.monotonic() + 20
    while s.vectors.get(FAKE_VISUAL_V2.key).count() < 40:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # Crash: the process dies mid-build (no clean shutdown of the job).
    with s.db.connect() as conn:
        partial = conn.execute("SELECT COUNT(*) FROM media_vectors WHERE model_key=?", (FAKE_VISUAL_V2.key,)).fetchone()[0]
    s.jobs.stop()
    s.close()
    assert 0 < partial < 240

    s2 = Services(cfg, engine=FakeEngine())
    try:
        fast = FakeEmbedder(FAKE_VISUAL_V2, salt=2)
        s2.extra_embedders[FAKE_VISUAL_V2.key] = fast
        s2.vectors.register(FAKE_VISUAL_V2)
        assert s2.storage.upgrade_status()["upgrade"]["status"] == "building"  # state survived
        s2.start_background()  # resumes the build by itself
        deadline = time.monotonic() + 30
        while not s2.storage.upgrade_status()["upgrade"]["ready"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        new = s2.vectors.get(FAKE_VISUAL_V2.key)
        assert new.count() == 240
        resumed = sum(1 for _ in range(fast.calls))  # batches after the restart
        assert resumed * 8 <= (240 - partial) + 16  # only the missing items were embedded again
        assert s2.visual_space().key == FAKE_VISUAL.key

        # A/B: the same query against both indexes.
        compare = s2.storage.compare(similar_media_id=ids[10], limit=8)
        assert compare["current"]["key"] == FAKE_VISUAL.key and compare["candidate"]["key"] == FAKE_VISUAL_V2.key
        assert len(compare["current"]["ids"]) == 8 and len(compare["candidate"]["ids"]) == 8
        assert compare["current"]["ids"] != compare["candidate"]["ids"]  # different models, different neighbours

        assert s2.storage.switch()["upgrade"]["active_key"] == FAKE_VISUAL_V2.key
        assert s2.visual_space().key == FAKE_VISUAL_V2.key
        rolled = s2.storage.rollback()["upgrade"]
        assert rolled["status"] == "rolled_back" and rolled["active_key"] == FAKE_VISUAL.key
        assert s2.visual_space().key == FAKE_VISUAL.key
        # The previous index was never touched, and the new one is still there (no data loss either way).
        assert sha(old_file) == old_hash
        assert s2.db.all("SELECT media_id, offset, sha FROM media_vectors WHERE model_key=? ORDER BY media_id",
                         (FAKE_VISUAL.key,)) == old_rows
        assert new.count() == 240
        assert s2.storage.switch()["upgrade"]["active_key"] == FAKE_VISUAL_V2.key  # and forward again
        assert s2.storage.finish()["upgrade"] is None and s2.visual_space().key == FAKE_VISUAL_V2.key
    finally:
        s2.close()


def test_upgrade_api(client, app_services):
    ids = add_media(app_services.db, 30)
    app_services.vectors.register(FAKE_VISUAL).add([(i, fake_vector(i, FAKE_VISUAL.dim)) for i in ids])
    app_services.extra_embedders[FAKE_VISUAL_V2.key] = FakeEmbedder(FAKE_VISUAL_V2, salt=3)
    app_services.vectors.register(FAKE_VISUAL_V2)
    options = client.get("/api/models/upgrade").json()["options"]
    assert {o["key"]: o["active"] for o in options}[FAKE_VISUAL.key] is True
    assert client.post("/api/models/upgrade/switch").status_code == 409
    assert client.post("/api/models/upgrade", json={"to_key": "nope@1:2"}).status_code in (404, 409)
    assert client.post("/api/models/upgrade", json={"to_key": FAKE_VISUAL_V2.key}).json()["upgrade"]["status"] == "building"
    deadline = time.monotonic() + 20
    while not client.get("/api/models/upgrade").json()["upgrade"]["ready"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.post("/api/models/upgrade/compare", json={"similar_media_id": ids[0]}).json()["limit"] == 12
    assert client.post("/api/models/upgrade/switch").json()["upgrade"]["status"] == "switched"
    assert client.post("/api/models/upgrade/rollback").json()["upgrade"]["active_key"] == FAKE_VISUAL.key
