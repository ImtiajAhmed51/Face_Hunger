"""Video intelligence: tracker, scene keyframes, moments, person moments, clips, cancel, budget."""

import json
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest

from backend.video.tracker import FaceTracker
from tests.helpers import video_fixture as vf

BUFFALO = Path(__file__).resolve().parents[1] / "models" / "buffalo_l"
needs_engine = pytest.mark.skipif(not (BUFFALO / "det_10g.onnx").is_file(), reason="buffalo_l not installed")


def emb(seed, noise=0.0, salt=0):
    v = np.random.default_rng(seed).standard_normal(512)
    v = v + noise * np.random.default_rng(10_000 + salt).standard_normal(512)
    return (v / np.linalg.norm(v)).astype(np.float32)


def det(bbox, person, salt, q=0.8):
    return {"bbox": bbox, "embedding": emb(person, 0.4, salt), "quality": q, "detection": q}


# -- tracker -------------------------------------------------------------------

def test_tracker_keeps_identities_when_faces_cross():
    tr = FaceTracker()
    ids = []
    for step in range(8):  # A moves right, B moves left; they cross at step ~4
        a = det([20 + 40 * step, 50, 60, 60], 1, step)
        b = det([340 - 40 * step, 50, 60, 60], 2, 100 + step)
        ids.append(tr.update(step * 3.0, [a, b]))
    assert len({i[0] for i in ids}) == 1 and len({i[1] for i in ids}) == 1 and ids[0][0] != ids[0][1]


def test_low_confidence_faces_extend_tracks_and_gaps_end_them():
    tr = FaceTracker(max_gap=6.0)
    first = tr.update(0.0, [det([10, 10, 50, 50], 1, 0)])[0]
    assert tr.update(3.0, [det([14, 12, 50, 50], 1, 1, q=0.3)]) == [first]  # blurry: second-stage match
    later = tr.update(20.0, [det([14, 12, 50, 50], 1, 2)])[0]  # long gap: new tracklet
    assert later != first
    tracks = tr.all_tracks()
    assert [t.history for t in tracks] == [[0.0, 3.0], [20.0]]


def test_strong_identity_matches_despite_motion_between_samples():
    tr = FaceTracker()
    a = tr.update(0.0, [det([10, 10, 60, 60], 1, 0)])[0]
    assert tr.update(3.0, [det([500, 300, 60, 60], 1, 1)]) == [a]  # moved far, same person
    assert tr.update(6.0, [det([500, 300, 60, 60], 7, 2)]) != [a]  # same place, different person


# -- keyframes + moments --------------------------------------------------------

class ColourEncoder:
    """Embeds a frame as its mean colour; text 'red'/'green'/... maps to that colour."""

    class spec:
        key, dim, slug = "colour@1:3", 3, "colour_1_3"

    COLOURS = {"blue": (40, 60, 90), "grey": (120, 120, 120), "brown": (80, 50, 40), "green": (30, 80, 60)}

    def embed_images(self, images):
        out = np.stack([im.reshape(-1, 3)[:, ::-1].mean(axis=0) for im in images]).astype(np.float32) - 85
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    def embed_texts(self, texts):
        out = np.array([self.COLOURS[t] for t in texts], np.float32) - 85
        return out / np.linalg.norm(out, axis=1, keepdims=True)


@pytest.fixture(scope="module")
def fixture_video(tmp_path_factory):
    return vf.make_video(tmp_path_factory.mktemp("video") / "two_people.mp4")


def _add_video(s, path, duration=60.0):
    with s.db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO libraries(id, path, name) VALUES (1, ?, 'v')", (str(path.parent),))
        return conn.execute("INSERT INTO media(library_id, path, name, kind, size, mtime_ns, duration, status) "
                            "VALUES (1, ?, ?, 'video', 1, 1, ?, 'indexed')", (str(path), path.name, duration)).lastrowid


def test_keyframes_and_moment_search(client, app_services, fixture_video):
    s = app_services
    s.keyframe_encoder = ColourEncoder()
    mid = _add_video(s, fixture_video)
    s.jobs.start()
    job = s.jobs.enqueue("video_analysis")
    done = s.jobs.wait(job["id"], timeout=60)
    assert done["status"] == "completed", done
    detail = client.get(f"/api/media/{mid}/video").json()
    times = [k["t"] for k in detail["keyframes"]]
    for cut in [0.0, *vf.CUTS]:  # every shot boundary is a keyframe (+-0.5 s)
        assert any(abs(t - cut) <= 0.5 for t in times), (cut, times)
    assert client.get(f"/api/keyframes/{detail['keyframes'][0]['id']}/image").headers["content-type"] == "image/jpeg"
    # "grey" is the 12-20 s shot; "green" is 32-44 s.
    grey = client.post("/api/search/moments", json={"text": "grey"}).json()["items"][0]
    green = client.post("/api/search/moments", json={"text": "green"}).json()["items"][0]
    assert grey["media_id"] == mid and 12.0 - 0.5 <= grey["t"] <= 20.0
    assert 32.0 - 0.5 <= green["t"] <= 44.0
    # Re-running is incremental: nothing pending.
    assert s.video.pending() == []


def test_clip_export_stream_copies_without_touching_original(client, app_services, fixture_video):
    mid = _add_video(app_services, fixture_video)
    before = fixture_video.read_bytes()
    r = client.get(f"/api/media/{mid}/clip?start=20&end=26")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    out = app_services.config.data_dir / "exports" / "probe.mp4"
    out.write_bytes(r.content)
    from backend.media_processing import _ffprobe_metadata
    meta = _ffprobe_metadata(out)
    assert 5.0 <= meta["duration"] <= 8.5  # stream copy snaps to the keyframe before 20 s (GOP 2 s)
    assert fixture_video.read_bytes() == before
    assert client.get(f"/api/media/{mid}/clip?start=5&end=2").status_code == 400


# -- real engine ------------------------------------------------------------------

def _index(s, folder, interval=None):
    if interval is not None:
        with s.db.connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES ('video_interval', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(interval),))
    with s.db.connect() as conn:
        lid = conn.execute("INSERT INTO libraries(path, name) VALUES (?, 'v')", (str(folder.resolve()),)).lastrowid
    job = s.worker.start(lid)
    s.worker._thread.join(900)
    return s.db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))


@pytest.fixture
def engine_services(make_config):
    from backend.services.container import Services
    s = Services(make_config(model_dir=BUFFALO.parent, watch=False))
    yield s
    s.close()


@pytest.mark.ai
@needs_engine
def test_two_people_tracks_merge_and_moments_match_ground_truth(engine_services, fixture_video, tmp_path):
    s = engine_services
    folder = tmp_path / "lib"
    folder.mkdir()
    (folder / "two_people.mp4").write_bytes(fixture_video.read_bytes())
    done = _index(s, folder)
    assert done["status"] == "completed", done["error"]
    media = s.db.one("SELECT id FROM media")
    detail = s.video.detail(media["id"])
    people = {p["person_id"] for p in detail["people"]}
    tracks = [t for t in detail["tracks"] if t["person_id"] is not None]
    assert len(people) == 2, detail["people"]
    assert len(tracks) >= 3  # A leaves and re-enters: several tracklets, merged into one person
    # Name people by which ground-truth person their first segment matches.
    by_start = {round(p["segments"][0]["start"]): p for p in detail["people"]}
    a = min(detail["people"], key=lambda p: p["segments"][0]["start"])
    b = next(p for p in detail["people"] if p is not a)
    for person, truth in ((a, vf.TRUTH["A"]), (b, vf.TRUTH["B"])):
        moments = s.video.person_moments(person["person_id"])
        segs = moments[0]["segments"]
        assert len(segs) == len(truth), (segs, truth)
        for seg, (start, end) in zip(segs, truth):
            assert abs(seg["start"] - start) <= 2.0 and abs(seg["end"] - end) <= 2.0, (seg, (start, end))
    assert by_start  # (keeps the mapping visible in failures)
    # Per-person clip export: one clip per segment, stream copied, plus a manifest.
    zpath = s.video.person_clips_zip(media["id"], a["person_id"])
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert len([n for n in names if n.endswith(".mp4")]) == 3 and "manifest.json" in names


@pytest.mark.ai
@needs_engine
def test_cancel_mid_video(engine_services, tmp_path):
    s = engine_services
    folder = tmp_path / "lib"
    folder.mkdir()
    vf.make_video(folder / "long.mp4", duration=240)
    with s.db.connect() as conn:
        lid = conn.execute("INSERT INTO libraries(path, name) VALUES (?, 'v')", (str(folder.resolve()),)).lastrowid
    job = s.worker.start(lid)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        progress = json.loads(s.db.one("SELECT progress FROM jobs WHERE id=?", (job["id"],))["progress"] or "{}")
        if progress.get("video_seconds", 0) >= 30:
            break
        time.sleep(0.1)
    assert progress.get("video_duration") == 240.0  # progress streams per video position
    started = time.monotonic()
    s.worker.control(job["id"], "cancel")
    s.worker._thread.join(30)
    assert time.monotonic() - started < 2.5
    assert s.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "cancelled"
    assert s.db.one("SELECT COUNT(*) c FROM media WHERE status='indexed'")["c"] == 0  # nothing half-committed


@pytest.mark.perf
@pytest.mark.ai
@needs_engine
def test_ten_minute_1080p_video_budget(engine_services, tmp_path):
    s = engine_services
    folder = tmp_path / "lib"
    folder.mkdir()
    vf.make_long_video(folder / "ten_minutes.mp4")
    started = time.monotonic()
    done = _index(s, folder)
    elapsed = time.monotonic() - started
    assert done["status"] == "completed", done["error"]
    s.keyframe_encoder = ColourEncoder()
    t2 = time.monotonic()
    s.video.run(lambda: None, lambda **_: None)
    keyframe_s = time.monotonic() - t2
    print(f"10-min 1080p: faces+tracks {elapsed:.1f} s, keyframes {keyframe_s:.1f} s")
    assert elapsed + keyframe_s < 300  # documented budget: 5 min on CPU
