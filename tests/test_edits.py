"""Non-destructive edits: originals untouched, revert, XMP via exiftool, external sidecar changes."""

import hashlib
import io
import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from backend import edits as model

EXIFTOOL = shutil.which("exiftool")


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quadrants(size=(120, 80)) -> Image.Image:
    img = Image.new("RGB", size, (30, 30, 30))
    w, h = size
    for (x, y), colour in {(0, 0): (255, 0, 0), (w // 2, 0): (0, 255, 0), (0, h // 2): (0, 0, 255), (w // 2, h // 2): (255, 255, 0)}.items():
        img.paste(colour, (x, y, x + w // 2, y + h // 2))
    return img


@pytest.fixture
def photo(client, app_services, tmp_path):
    """One indexed photo in a watched library; returns (services, media_id, path)."""
    library = tmp_path / "library"
    library.mkdir()
    path = library / "IMG_0001.png"
    quadrants().save(path)
    lid = client.post("/api/libraries", json={"path": str(library)}).json()["id"]
    job = client.post("/api/index", json={"library_id": lid}).json()
    deadline = time.monotonic() + 30
    while app_services.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    media = app_services.db.one("SELECT id FROM media")
    return app_services, media["id"], path


def rendered(client, media_id) -> Image.Image:
    return Image.open(io.BytesIO(client.get(f"/api/media/{media_id}/rendered").content)).convert("RGB")


def close(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and np.abs(np.asarray(a, dtype=int) - np.asarray(b, dtype=int)).mean() < 6


# -- model ---------------------------------------------------------------------

@pytest.mark.parametrize("orientation", range(1, 9))
def test_orientation_round_trips_and_matches_exif_semantics(orientation):
    edit = model.from_exif_orientation(orientation)
    assert model.to_exif_orientation(edit) == orientation
    img = quadrants()
    tagged = img.copy()
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    tagged.save(buf, format="PNG", exif=exif.tobytes())
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(buf.getvalue()))).convert("RGB")
    assert close(model.apply(img, edit), expected)


def test_flip_v_folds_into_orientation():
    assert model.to_exif_orientation({"rotation": 0, "flip_v": True}) == 4
    assert model.to_exif_orientation({"rotation": 90, "flip_h": True, "flip_v": True}) == model.to_exif_orientation({"rotation": 270})


def test_normalize_validates():
    assert model.normalize({"rotation": 450})["rotation"] == 90
    assert model.normalize({"crop": {"x": 0, "y": 0, "w": 1, "h": 1}})["crop"] is None
    assert model.normalize({"label": "red"})["label"] == "Red"
    for bad in ({"rotation": 45}, {"rating": 9}, {"label": "Pink"}, {"flag": "maybe"}, {"crop": {"x": 0.5, "y": 0.5, "w": 0.001, "h": 0.5}}):
        with pytest.raises(model.EditError):
            model.normalize(bad)


def test_xmp_preserves_another_applications_properties():
    foreign = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
      <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
        <dc:creator><rdf:Seq><rdf:li>Someone</rdf:li></rdf:Seq></dc:creator><xmp:Rating>2</xmp:Rating>
      </rdf:Description></rdf:RDF></x:xmpmeta>'''
    assert model.parse_xmp(foreign)["rating"] == 2
    out = model.write_xmp({"rating": 5, "label": "Blue"}, foreign)
    assert b"Someone" in out and out.count(b"Rating") == 1
    assert model.parse_xmp(out) == {**model.EMPTY, "rating": 5, "label": "Blue"}
    assert model.parse_xmp(b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                           b'<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="-1"/></rdf:RDF></x:xmpmeta>')["flag"] == "reject"


# -- DoD: originals, revert ----------------------------------------------------------

def test_original_is_byte_identical_after_any_edit_sequence_and_revert_restores_view(client, photo):
    s, mid, path = photo
    before_hash, before_stat = sha(path), path.stat()
    original_view = rendered(client, mid)
    steps = [{"rotation": 90}, {"flip_h": True}, {"crop": {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.5}}, {"rating": 4},
             {"label": "Green"}, {"flag": "pick"}, {"rotation": 270, "flip_v": True}, {"crop": None}, {"rating": 0}]
    for step in steps:
        r = client.patch(f"/api/media/{mid}/edits", json=step)
        assert r.status_code == 200, r.text
        assert sha(path) == before_hash
    detail = client.get(f"/api/media/{mid}/edits").json()
    assert detail["edit"]["rotation"] == 270 and detail["edit"]["flip_v"] and detail["edit"]["label"] == "Green"
    assert len(detail["history"]) == len(steps)
    assert not close(rendered(client, mid), original_view) or rendered(client, mid).size != original_view.size
    # Thumbnails, exports and a re-index all leave the original alone and keep the edit.
    assert client.get(f"/api/media/{mid}/thumbnail").status_code == 200
    z = zipfile.ZipFile(io.BytesIO(client.post("/api/export", json={"media_ids": [mid], "apply_edits": True}).content))
    edited_name = next(n for n in z.namelist() if n.endswith("-edited.jpg"))
    assert Image.open(io.BytesIO(z.read(edited_name))).size == (80, 120)  # rotated
    job = client.post("/api/index", json={"library_id": 1, "force": True}).json()
    deadline = time.monotonic() + 30
    while s.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.get(f"/api/media/{mid}/edits").json()["edit"] == detail["edit"]  # re-scan never overwrites edits
    # Undo one step, then revert everything.
    last = detail["history"][0]
    undone = client.post(f"/api/media/{mid}/edits/revert", json={"history_id": last["id"]}).json()
    assert undone["edit"] == last["before"]
    reverted = client.post(f"/api/media/{mid}/edits/revert").json()
    assert reverted["edit"] == model.EMPTY and reverted["edited"] is False
    assert close(rendered(client, mid), original_view)
    assert sha(path) == before_hash and path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert client.patch(f"/api/media/{mid}/edits", json={"rotation": 33}).status_code == 400


def test_edited_view_matches_expected_pixels(client, photo):
    _, mid, _ = photo
    client.patch(f"/api/media/{mid}/edits", json={"rotation": 90, "crop": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}})
    out = rendered(client, mid)
    assert out.size == (40, 60)
    # Rotating 90 degrees clockwise brings the blue (bottom-left) quadrant to the top-left.
    assert np.asarray(out)[30, 20].tolist()[2] > 200 and np.asarray(out)[30, 20].tolist()[0] < 60
    thumb = Image.open(io.BytesIO(client.get(f"/api/media/{mid}/thumbnail").content))
    assert thumb.size == (40, 60)
    row = client.get("/api/media?limit=5").json()["items"][0]
    assert row["edited"] is True and row["edit_version"] == 1


# -- DoD: XMP + exiftool ---------------------------------------------------------------

def test_xmp_sidecar_is_read_back_by_exiftool(client, photo):
    s, mid, path = photo
    client.patch(f"/api/media/{mid}/edits", json={"rotation": 90, "rating": 4, "label": "Red", "flag": "pick",
                                                   "crop": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5}})
    sidecar = Path(client.get(f"/api/media/{mid}/edits").json()["sidecar"])
    assert sidecar == path.with_name(path.name + ".xmp") and sidecar.is_file()
    data = sidecar.read_bytes()
    # Schema checks that hold everywhere: a well-formed packet whose properties parse back.
    assert data.startswith(b"<?xpacket begin=") and b'<?xpacket end="w"?>' in data
    assert model.parse_xmp(data) == {"rotation": 90, "flip_h": False, "flip_v": False, "rating": 4, "label": "Red",
                                     "flag": "pick", "crop": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5}}
    side_json = json.loads((s.config.data_dir / "sidecars" / f"{mid}.json").read_text())
    assert side_json["edit"]["flag"] == "pick" and side_json["original"] == str(path)
    if not EXIFTOOL:
        pytest.skip("exiftool not installed: schema-validated only")
    tags = json.loads(subprocess.run([EXIFTOOL, "-j", "-n", "-XMP:all", str(sidecar)], capture_output=True, text=True,
                                     check=True).stdout)[0]
    assert tags["Rating"] == 4 and tags["Label"] == "Red" and tags["Orientation"] == 6
    assert tags["HasCrop"] is True
    assert (tags["CropLeft"], tags["CropTop"], tags["CropRight"], tags["CropBottom"]) == pytest.approx((0.1, 0.2, 0.7, 0.7))
    assert subprocess.run([EXIFTOOL, "-validate", "-warning", "-a", str(sidecar)], capture_output=True, text=True).returncode == 0


# -- DoD: external change detected by the watcher within 5 s ------------------------------

def test_external_sidecar_change_is_detected_within_5s(client, photo):
    s, mid, path = photo
    client.patch(f"/api/media/{mid}/edits", json={"rating": 2})
    sidecar = path.with_name(path.name + ".xmp")
    time.sleep(1.2)  # let our own write settle (it is recognised by hash and ignored)
    assert client.get(f"/api/media/{mid}/edits").json()["edit"]["rating"] == 2
    # Another application rates it 5 stars and labels it Purple.
    external = model.write_xmp({"rating": 5, "label": "Purple"}, sidecar.read_bytes())
    started = time.monotonic()
    sidecar.write_bytes(external)
    edit = {}
    while time.monotonic() - started < 5:
        edit = client.get(f"/api/media/{mid}/edits").json()
        if edit["edit"]["rating"] == 5:
            break
        time.sleep(0.05)
    elapsed = time.monotonic() - started
    assert edit["edit"]["rating"] == 5 and edit["edit"]["label"] == "Purple", f"not detected after {elapsed:.1f}s"
    assert edit["history"][0]["source"] == "external"
    print(f"external sidecar change detected in {elapsed:.2f}s")
    assert sidecar.read_bytes() == external  # we did not rewrite the other application's file


def test_sidecar_from_another_app_is_imported_at_index_time(client, app_services, tmp_path):
    library = tmp_path / "lib2"
    library.mkdir()
    quadrants().save(library / "A.jpg")
    (library / "A.xmp").write_bytes(model.write_xmp({"rating": 3, "label": "Yellow"}))  # Lightroom-style name
    lid = client.post("/api/libraries", json={"path": str(library)}).json()["id"]
    job = client.post("/api/index", json={"library_id": lid}).json()
    deadline = time.monotonic() + 30
    while app_services.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    mid = app_services.db.one("SELECT id FROM media")["id"]
    detail = client.get(f"/api/media/{mid}/edits").json()
    assert detail["edit"]["rating"] == 3 and detail["edit"]["label"] == "Yellow"
    assert detail["sidecar"].endswith("A.xmp")
    client.patch(f"/api/media/{mid}/edits", json={"rating": 4})
    assert not (library / "A.jpg.xmp").exists()  # the existing sidecar is updated, not duplicated


# -- batch, filters, audit ------------------------------------------------------------------

def test_batch_rating_filters_and_audit_undo(client, photo):
    s, mid, path = photo
    r = client.post("/api/edits/batch", json={"media_ids": [mid], "rating": 5, "label": "Blue"}).json()
    assert r["changed"] == 1
    assert client.get("/api/media?rating_min=4").json()["total"] == 1
    assert client.get("/api/media?label=blue").json()["total"] == 1
    assert client.get("/api/media?flag=unflagged").json()["total"] == 1
    assert client.get("/api/media?flag=pick").json()["total"] == 0
    assert client.post(f"/api/audit/{r['audit_id']}/undo").status_code == 200
    assert client.get(f"/api/media/{mid}/edits").json()["edit"] == model.EMPTY
    assert client.post("/api/edits/batch", json={"media_ids": [mid]}).status_code == 400
