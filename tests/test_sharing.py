"""Share safely: targeted faces unrecognisable in outputs, metadata gone, originals untouched, resumable."""

import hashlib
import io
import json
import shutil
import subprocess
import time
import zipfile
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from backend import anonymize as anon
from backend.jobs.manager import Cancelled

INSIGHT = Path(__import__("insightface").__file__).parent / "data" / "images"
BUFFALO = Path(__file__).resolve().parents[1] / "models" / "buffalo_l"
needs_engine = pytest.mark.skipif(not (BUFFALO / "det_10g.onnx").is_file(), reason="buffalo_l not installed")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def gps_exif() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    exif[0x0132] = "2021:06:01 10:00:00"
    exif.get_ifd(0x8825).update({1: "N", 2: (Fraction(48), Fraction(51), Fraction(30)), 3: "E", 4: (Fraction(2), Fraction(21), Fraction(8))})
    return exif.tobytes()


# -- pure anonymization -------------------------------------------------------------

@pytest.mark.parametrize("method", anon.METHODS)
def test_region_is_changed_and_outside_is_untouched(method):
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (300, 400, 3), dtype=np.uint8)
    face = {"bbox": [150, 100, 80, 100], "landmarks": [[170, 140], [210, 142], [190, 160], [175, 180], [205, 181]]}
    out = anon.anonymize(img, [face], method=method, strength=0.0)  # weakest slider position
    assert out is not img and (img[5:20, 5:20] == out[5:20, 5:20]).all()  # far corner untouched
    inner_before, inner_after = img[120:180, 165:215].astype(float), out[120:180, 165:215].astype(float)
    assert np.abs(inner_before - inner_after).mean() > 30
    assert inner_after.std() < inner_before.std() * 0.5  # detail destroyed
    assert anon.covers([face], [160, 110, 60, 80]) and not anon.covers([face], [10, 10, 30, 30])
    with pytest.raises(ValueError):
        anon.anonymize(img, [face], method="sparkle")


# -- job behaviour without the face engine ----------------------------------------------

@pytest.fixture
def plain_library(client, app_services, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    rng = np.random.default_rng(3)
    with app_services.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'lib')", (str(lib),))
        for i in range(1, 7):
            path = lib / f"P{i}.jpg"
            Image.fromarray(rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)).save(path, exif=gps_exif())
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, width, height, status) "
                         "VALUES (?,1,?,?, 'photo', 1, 1, 160, 120, 'indexed')", (i, str(path), path.name))
    return app_services, lib


def test_job_zip_strips_metadata_and_leaves_originals(client, plain_library):
    s, lib = plain_library
    before = {p.name: sha(p) for p in lib.iterdir()}
    job = client.post("/api/share", json={"media_ids": [1, 2, 3]}).json()
    done = s.jobs.wait(job["id"], timeout=60)
    assert done["status"] == "completed", done
    r = client.get(f"/api/share/{job['id']}/download")
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["P1.jpg", "P2.jpg", "P3.jpg"]
    data = zipfile.ZipFile(io.BytesIO(r.content)).read("P1.jpg")
    assert dict(Image.open(io.BytesIO(data)).getexif()) == {}  # no EXIF at all, so no GPS
    assert {p.name: sha(p) for p in lib.iterdir()} == before
    assert client.get("/api/share/99999/download").status_code == 404
    # Keeping metadata is opt-in and still drops GPS by default.
    data, _ = s.sharing.render(1, {"strip_metadata": False})
    exif = Image.open(io.BytesIO(data)).getexif()
    assert exif.get(0x010F) == "TestCam" and not dict(exif.get_ifd(0x8825))


def test_folder_export_never_overwrites_and_rejects_library_folders(client, plain_library, tmp_path):
    s, lib = plain_library
    out = tmp_path / "share-out"
    out.mkdir()
    (out / "P1.jpg").write_bytes(b"already here")
    body = {"media_ids": [1], "options": {"destination": {"type": "folder", "path": str(out)}}}
    job = client.post("/api/share", json=body).json()
    assert s.jobs.wait(job["id"], timeout=60)["status"] == "completed"
    assert (out / "P1.jpg").read_bytes() == b"already here" and (out / "P1-1.jpg").is_file()
    for bad in (str(lib), str(lib / "sub"), str(s.config.data_dir / "x"), "relative/path"):
        r = client.post("/api/share", json={"media_ids": [1], "options": {"destination": {"type": "folder", "path": bad}}})
        assert r.status_code == 400, bad


def test_export_resumes_after_cancel_without_redoing_finished_items(plain_library, monkeypatch):
    s, _ = plain_library
    calls = []
    real = s.sharing.render

    def counting(media_id, options, **kw):
        calls.append(media_id)
        return real(media_id, options, **kw)

    monkeypatch.setattr(s.sharing, "render", counting)
    state: dict = {}
    ticks = {"n": 0}

    def checkpoint():
        ticks["n"] += 1
        if ticks["n"] == 4:
            raise Cancelled("stop")

    with pytest.raises(Cancelled):
        s.sharing.run(7, {"media_ids": [1, 2, 3, 4, 5, 6]}, state, checkpoint=checkpoint,
                      progress=lambda force=False, **f: state.update(f))
    assert calls == [1, 2, 3] and len(state["done"]) == 3
    result = s.sharing.run(7, {"media_ids": [1, 2, 3, 4, 5, 6]}, state, checkpoint=lambda: None,
                           progress=lambda force=False, **f: state.update(f))
    assert calls == [1, 2, 3, 4, 5, 6]  # nothing rendered twice
    assert result["exported"] == 6 and Path(result["destination"]).name == "share-7.zip"
    assert len(zipfile.ZipFile(result["destination"]).namelist()) == 6


def test_people_listing_and_videos_are_skipped_by_default(client, plain_library):
    s, lib = plain_library
    with s.db.connect() as conn:
        conn.execute("INSERT INTO people(id, name) VALUES (1, 'Ada')")
        conn.execute("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha) VALUES "
                     "(1, 1, '[10,10,40,40]', 0.9, 0, 'x'), (2, NULL, '[10,10,40,40]', 0.9, 0, 'x')")
        conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status) VALUES "
                     "(50, 1, ?, 'clip.mp4', 'video', 1, 1, 'indexed')", (str(lib / "clip.mp4"),))
    (lib / "clip.mp4").write_bytes(b"not really a video")
    info = client.post("/api/share/people", json={"media_ids": [1, 2, 50]}).json()
    assert info["people"][0]["display_name"] == "Ada" and info["unknown_faces"] == 1 and info["videos"] == 1
    job = client.post("/api/share", json={"media_ids": [1, 50]}).json()
    done = json.loads(s.jobs.wait(job["id"], timeout=60)["progress"])
    assert done["skipped"] == {"50": "videos are not anonymized"} and done["exported"] == 1
    preview = client.post("/api/share/preview", json={"media_id": 1})
    assert preview.status_code == 200 and preview.headers["x-faces-anonymized"] == "1"


# -- DoD with the real engine --------------------------------------------------------------

@pytest.fixture
def face_library(make_config, tmp_path):
    from backend.services.container import Services

    lib = tmp_path / "faces"
    lib.mkdir()
    group = Image.open(INSIGHT / "t1.jpg").convert("RGB")
    # The sample is a tight 112 px crop; give it room so the detector sees a face in a scene.
    crop = Image.open(INSIGHT / "Tom_Hanks_54745.png").convert("RGB").resize((300, 300), Image.Resampling.LANCZOS)
    tom = Image.new("RGB", (640, 640), (70, 80, 95))
    tom.paste(crop, (170, 150))
    group.save(lib / "group.jpg", quality=95, exif=gps_exif())
    tom.save(lib / "tom.jpg", quality=95, exif=gps_exif())
    both = Image.new("RGB", (group.width + tom.width, max(group.height, tom.height)), (90, 90, 90))
    both.paste(group, (0, 0))
    both.paste(tom, (group.width, 0))
    both.save(lib / "both.jpg", quality=95, exif=gps_exif())
    s = Services(make_config(model_dir=BUFFALO.parent, watch=False))
    with s.db.connect() as conn:
        lid = conn.execute("INSERT INTO libraries(path, name) VALUES (?, 'faces')", (str(lib.resolve()),)).lastrowid
    job = s.worker.start(lid)
    s.worker._thread.join(600)
    assert s.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "completed"
    yield s, lib
    s.close()


def detections(s, data: bytes):
    bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return s.engine.detect(bgr)


@pytest.mark.ai
@needs_engine
@pytest.mark.parametrize("method", anon.METHODS)
def test_every_targeted_face_is_unrecognisable_and_metadata_is_gone(face_library, method):
    s, lib = face_library
    before = {p.name: sha(p) for p in lib.iterdir()}
    threshold = float(s.db.settings().get("matching_threshold", 0.48))
    faces = s.db.all("SELECT f.id, f.media_id, f.embedding_offset, f.embedding_sha, m.name FROM faces f JOIN media m ON m.id=f.media_id")
    assert len(faces) >= 14  # 6 + 1 + 7
    checked = 0
    for media in s.db.all("SELECT id, name FROM media"):
        data, report = s.sharing.render(media["id"], {"method": method, "strength": 0.0})  # weakest setting
        assert report["verified"]
        out = detections(s, data)
        for face in (f for f in faces if f["media_id"] == media["id"]):
            original = s.store.read(face["embedding_offset"], face["embedding_sha"])
            best = max((float(d["embedding"] @ original) for d in out), default=-1.0)
            assert best < threshold, (media["name"], method, best)
            checked += 1
        assert dict(Image.open(io.BytesIO(data)).getexif()) == {}
    assert checked == len(faces)
    assert {p.name: sha(p) for p in lib.iterdir()} == before  # originals unchanged
    print(f"{method}: {checked}/{checked} targeted faces below the match threshold")


@pytest.mark.ai
@needs_engine
def test_kept_person_stays_visible_while_everyone_else_is_anonymized(face_library, tmp_path):
    s, lib = face_library
    tom_media = s.db.one("SELECT id FROM media WHERE name='tom.jpg'")["id"]
    tom_person = s.db.one("SELECT person_id FROM faces WHERE media_id=?", (tom_media,))["person_id"]
    tom_vec = s.library._centroid(tom_person)
    threshold = float(s.db.settings().get("matching_threshold", 0.48))
    both = s.db.one("SELECT id FROM media WHERE name='both.jpg'")["id"]
    data, report = s.sharing.render(both, {"keep_people": [tom_person]})
    out = detections(s, data)
    assert max(float(d["embedding"] @ tom_vec) for d in out) >= threshold  # Tom is still Tom
    others = [f for f in s.sharing._stored_faces(both) if f["person_id"] != tom_person]
    assert len(others) >= 5 and report["faces_anonymized"] >= len(others)
    for face in others:
        assert max(float(d["embedding"] @ face["embedding"]) for d in out) < threshold
    # GPS check with exiftool on a real exported file (job -> folder).
    out_dir = tmp_path / "out"
    s.jobs.start()
    job = s.jobs.enqueue("share_export", {"media_ids": [both, tom_media], "keep_people": [tom_person],
                                          "destination": {"type": "folder", "path": str(out_dir)}})
    started = time.monotonic()
    assert s.jobs.wait(job["id"], timeout=120)["status"] == "completed"
    print(f"share export of 2 photos: {time.monotonic() - started:.1f}s")
    exported = sorted(p.name for p in out_dir.iterdir())
    assert exported == ["both.jpg", "tom.jpg"]
    if shutil.which("exiftool"):
        tags = json.loads(subprocess.run(["exiftool", "-j", "-G", str(out_dir / "both.jpg")], capture_output=True, text=True).stdout)[0]
        assert not [k for k in tags if k.startswith(("EXIF:", "GPS:", "XMP:"))], tags
