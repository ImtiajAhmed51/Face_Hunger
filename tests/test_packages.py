"""Encrypted packages: container integrity, round trip, conflicts, hostile input, constant memory."""

import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from backend.ops import fhpack
from backend.services.container import Services
from tests.conftest import FakeEngine
from tests.helpers.fixtures import FAKE_TEXT, fake_vector
from tests.test_search import FakeTextEncoder

PASS = "correct horse battery"
FAST = {"n": 1 << 12}  # cheap KDF for container-only tests


# -- container ----------------------------------------------------------------------

def seal(data: bytes, passphrase=PASS, chunk=4096) -> bytes:
    out = io.BytesIO()
    with fhpack.EncryptedWriter(out, passphrase, chunk_size=chunk, scrypt=FAST) as w:
        w.write(data)
    return out.getvalue()


def test_container_round_trips_in_chunks():
    data = os.urandom(50_000)
    blob = seal(data)
    reader = fhpack.DecryptedReader(io.BytesIO(blob), PASS)
    assert reader.read(10) == data[:10] and reader.read() == data[10:]
    assert seal(b"") and fhpack.DecryptedReader(io.BytesIO(seal(b"")), PASS).read() == b""
    assert data[:64] not in blob  # actually encrypted


def test_wrong_passphrase_is_reported_before_any_payload():
    blob = seal(b"secret" * 1000)
    with pytest.raises(fhpack.WrongPassphrase, match="Wrong passphrase"):
        fhpack.DecryptedReader(io.BytesIO(blob), "not the passphrase")
    with pytest.raises(fhpack.PackageError, match="not a Face Hunger package"):
        fhpack.DecryptedReader(io.BytesIO(b"PK\x03\x04" + blob), PASS)


def test_flipping_any_single_byte_fails_integrity():
    blob = seal(os.urandom(20_000))
    rng = np.random.default_rng(0)
    positions = sorted({8, 9, 30, len(blob) - 1, len(blob) - 20, *rng.integers(0, len(blob), 60).tolist()})
    for pos in positions:
        bad = bytearray(blob)
        bad[pos] ^= 0x01
        with pytest.raises(fhpack.PackageError):
            reader = fhpack.DecryptedReader(io.BytesIO(bytes(bad)), PASS)
            reader.read()


def test_truncation_reordering_and_trailing_data_fail():
    data = os.urandom(20_000)
    blob = seal(data)
    with pytest.raises(fhpack.IntegrityError):
        fhpack.DecryptedReader(io.BytesIO(blob[:-5000]), PASS).read()
    with pytest.raises(fhpack.IntegrityError):
        fhpack.DecryptedReader(io.BytesIO(blob + b"x"), PASS).read()
    # Swap the first two sealed chunks.
    header_len = len(fhpack.MAGIC) + 4 + int.from_bytes(blob[8:12], "big")
    size = int.from_bytes(blob[header_len:header_len + 4], "big") + 4
    swapped = blob[:header_len] + blob[header_len + size:header_len + 2 * size] + blob[header_len:header_len + size] + blob[header_len + 2 * size:]
    with pytest.raises(fhpack.IntegrityError):
        fhpack.DecryptedReader(io.BytesIO(swapped), PASS).read()
    # Dropping the final chunk (a clean cut on a chunk boundary) is also detected.
    last = len(blob) - (len(data) % 4096 + 16 + 4)
    with pytest.raises(fhpack.IntegrityError, match="truncated"):
        fhpack.DecryptedReader(io.BytesIO(blob[:last]), PASS).read()


def test_absurd_kdf_parameters_are_rejected():
    blob = seal(b"x")
    hlen = int.from_bytes(blob[8:12], "big")
    header = json.loads(blob[12:12 + hlen])
    header["n"] = 1 << 30
    evil = json.dumps(header, sort_keys=True).encode()
    with pytest.raises(fhpack.IntegrityError):
        fhpack.DecryptedReader(io.BytesIO(fhpack.MAGIC + len(evil).to_bytes(4, "big") + evil + blob[12 + hlen:]), PASS)


# -- round trip ----------------------------------------------------------------------------

def build_library(s, root: Path, count=12):
    lib = root / "photos"
    lib.mkdir(parents=True)
    rng = np.random.default_rng(5)
    from backend.duplicates import content_hash
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'photos')", (str(lib),))
        conn.execute("INSERT INTO people(id, name) VALUES (1, 'Ada'), (2, 'Ben')")
        for i in range(1, count + 1):
            path = lib / f"IMG_{i:03d}.jpg"
            Image.fromarray(rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)).save(path)
            Image.new("RGB", (40, 30), (i * 10, 50, 50)).save(s.config.data_dir / "thumbnails" / f"media-{i}.jpg")
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, captured_at, width, height, "
                         "content_hash, status, date_source) VALUES (?,1,?,?, 'photo', ?, 1, ?, 80, 60, ?, 'indexed', 'exif')",
                         (i, str(path), path.name, path.stat().st_size, f"2022-03-{i:02d}T10:00:00", content_hash(path)))
            person = 1 if i <= 5 else 2 if i <= 8 else None
            if person:
                v = np.random.default_rng(person).standard_normal(512) + 0.2 * rng.standard_normal(512)
                off, sha = s.store.append((v / np.linalg.norm(v)).astype(np.float32))
                conn.execute("INSERT INTO faces(media_id, person_id, bbox, detection, embedding_offset, embedding_sha, quality, "
                             "review_state) VALUES (?,?, '[1,1,9,9]', 0.9, ?, ?, 0.8, 'confirmed')", (i, person, off, sha))
    s.cluster.invalidate()
    with s.db.connect() as conn:
        s.cluster.refresh(conn, [1, 2])
    space = s.vectors.register(FAKE_TEXT)
    space.add([(i, fake_vector(i, FAKE_TEXT.dim)) for i in range(1, count + 1)])
    s.library.create_album("Spring", [3, 1, 9])
    s.library.set_favorite([2, 9], True)
    s.edits.update(4, {"rotation": 90, "rating": 5, "label": "Green"})
    s.edits.update(9, {"crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "flag": "pick"})
    return lib


def describe(s, monkeypatch) -> dict:
    """Everything the DoD says must be reproduced, keyed by names (ids differ between machines)."""
    monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())
    name = {r["id"]: r["name"] for r in s.db.all("SELECT id, name FROM media")}

    def search(**q):
        return [i["name"] for i in s.search.run({"limit": 50, **q})["items"]]

    people = {p["name"]: sorted(name[f["media_id"]] for f in s.db.all("SELECT media_id FROM faces WHERE person_id=?", (p["id"],)))
              for p in s.db.all("SELECT id, name FROM people")}
    person_id = {p["name"]: p["id"] for p in s.db.all("SELECT id, name FROM people")}
    return {
        "people": people,
        "albums": {a["name"]: [name[r["media_id"]] for r in s.db.all(
            "SELECT media_id FROM album_media WHERE album_id=? ORDER BY position", (a["id"],))] for a in s.db.all("SELECT * FROM albums")},
        "favorites": sorted(name[r["media_id"]] for r in s.db.all("SELECT media_id FROM favorites")),
        "edits": {name[r["media_id"]]: s.edits.get(r["media_id"]) for r in s.db.all("SELECT media_id FROM media_edits")},
        "search_text": search(text="photo 7")[:5],
        "search_person": sorted(search(people=[person_id["Ada"]])),
        "search_date": search(date_from="2022-03-10", date_to="2022-03-12"),
        "search_name": search(name="IMG_00"),
    }


@pytest.fixture
def source(make_config, tmp_path):
    s = Services(make_config(), engine=FakeEngine())
    build_library(s, tmp_path / "src")
    yield s
    s.close()


def clean_services(tmp_path, name="other"):
    from backend.config import Config
    root = tmp_path / name
    (root / "frontend").mkdir(parents=True)
    return Services(Config(data_dir=root / "data", model_dir=root / "models", frontend_dir=root / "frontend",
                           allowed_roots=str(root), watch=False), engine=FakeEngine())


def test_round_trip_reproduces_people_albums_edits_and_search(source, tmp_path, monkeypatch):
    expected = describe(source, monkeypatch)
    result = source.packages.export({"type": "library"}, PASS, include_media=True)
    assert result["faces"] == 8 and result["people"] == 2 and result["albums"] == 1 and result["edits"] == 2 and result["files"] == 12
    package = Path(result["path"])
    raw = package.read_bytes()
    assert b"IMG_001" not in raw and b"Ada" not in raw and b"manifest" not in raw  # nothing readable without the key
    info = source.packages.inspect(package, PASS)
    assert info["manifest"]["media_count"] == 12 and info["header"]["cipher"] == "AES-256-GCM"

    target = clean_services(tmp_path)
    try:
        report = target.packages.import_package(package, PASS)
        assert report["media_new"] == 12 and report["people_new"] == 2 and report["faces"] == 8 and report["conflicts"] == []
        assert describe(target, monkeypatch) == expected
        # Originals arrived intact and are viewable; thumbnails came along.
        row = target.db.one("SELECT * FROM media WHERE name='IMG_004.jpg'")
        original = source.db.one("SELECT path FROM media WHERE name='IMG_004.jpg'")["path"]
        assert Path(row["path"]).read_bytes() == Path(original).read_bytes() and row["missing"] == 0
        assert (target.config.data_dir / "thumbnails" / f"media-{row['id']}.jpg").is_file()
        assert not list(target.packages.folder().glob("import-*"))  # staging removed
    finally:
        target.close()


def snapshot_dir(root: Path) -> dict:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file() and "-wal" not in p.name and "-shm" not in p.name}


def test_tampering_and_wrong_passphrase_leave_no_partial_data(source, tmp_path):
    package = Path(source.packages.export({"type": "library"}, PASS, include_media=True)["path"])
    target = clean_services(tmp_path)
    try:
        before_rows = {t: target.db.one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in ("media", "people", "faces", "albums", "libraries")}
        before_files = snapshot_dir(target.config.data_dir)
        with pytest.raises(fhpack.WrongPassphrase, match="Wrong passphrase"):
            target.packages.import_package(package, "wrong passphrase")
        data = bytearray(package.read_bytes())
        for position in (len(data) // 2, len(data) - 3, 200):
            bad = tmp_path / f"bad-{position}.fhpack"
            flipped = bytearray(data)
            flipped[position] ^= 0x40
            bad.write_bytes(bytes(flipped))
            with pytest.raises(fhpack.PackageError) as err:
                target.packages.import_package(bad, PASS)
            assert "Integrity" in str(err.value) or "damaged" in str(err.value) or "Wrong" in str(err.value)
        assert {t: target.db.one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in before_rows} == before_rows
        assert snapshot_dir(target.config.data_dir) == before_files  # no staging, no media, no vectors
    finally:
        target.close()


def test_conflict_policies(source, tmp_path, monkeypatch):
    package = Path(source.packages.export({"type": "library"}, PASS, include_media=True)["path"])
    target = clean_services(tmp_path)
    try:
        target.packages.import_package(package, PASS)
        media_before = target.db.one("SELECT COUNT(*) c FROM media")["c"]
        mid = target.db.one("SELECT id FROM media WHERE name='IMG_004.jpg'")["id"]
        target.edits.update(mid, {"rating": 1})  # diverge from the package
        # Default: keep both.
        with target.db.connect() as conn:  # an item without a hash is matched by name + size, not duplicated
            conn.execute("UPDATE media SET content_hash=NULL WHERE name='IMG_012.jpg'")
        both = target.packages.import_package(package, PASS)
        assert both["policy"] == "keep_both" and both["media_new"] == 0 and both["media_matched"] == 12 and both["faces"] == 0
        assert target.db.one("SELECT COUNT(*) c FROM media")["c"] == media_before  # same photos are never duplicated
        assert sorted(r["name"] for r in target.db.all("SELECT name FROM people")) == ["Ada", "Ada (imported)", "Ben", "Ben (imported)"]
        assert sorted(r["name"] for r in target.db.all("SELECT name FROM albums")) == ["Spring", "Spring (imported)"]
        assert target.edits.get(mid)["rating"] == 1  # ours stays current ...
        assert target.edits.history(mid)[0]["summary"].startswith("imported edit")  # ... theirs is in the history
        assert {c["type"] for c in both["conflicts"]} == {"person", "album", "edit"}
        # Skip: nothing new.
        skip = target.packages.import_package(package, PASS, conflict="skip")
        assert skip["people_new"] == 0 and skip["albums"] == 0 and target.edits.get(mid)["rating"] == 1
        assert target.db.one("SELECT COUNT(*) c FROM albums")["c"] == 2
        # Overwrite: package wins.
        over = target.packages.import_package(package, PASS, conflict="overwrite")
        assert target.edits.get(mid) == source.edits.get(4) and over["faces"] == 8
        assert target.db.one("SELECT COUNT(*) c FROM faces")["c"] == 8  # replaced, not duplicated
        with pytest.raises(fhpack.PackageError):
            target.packages.import_package(package, PASS, conflict="merge-ish")
    finally:
        target.close()


def test_person_scope_and_api_never_store_the_passphrase(client, app_services, tmp_path):
    build_library(app_services, tmp_path / "src")
    r = client.post("/api/packages/export", json={"scope": {"type": "person", "person_id": 1}, "passphrase": PASS, "include_media": True})
    assert r.status_code == 200 and "secret" not in json.dumps(r.json())
    done = app_services.jobs.wait(r.json()["id"], timeout=60)
    assert done["status"] == "completed", done
    assert PASS not in json.dumps(app_services.db.all("SELECT * FROM jobs"))
    progress = json.loads(done["progress"])
    assert progress["media"] == 5 and progress["people"] == 1 and progress["faces"] == 5
    listed = client.get("/api/packages").json()
    assert listed["items"][0]["name"] == progress["name"] and "secret" not in json.dumps(listed)
    assert client.get(f"/api/packages/files/{progress['name']}").status_code == 200
    assert client.get("/api/packages/files/..%2Findex.sqlite").status_code == 404
    assert client.post("/api/packages/inspect", json={"source": progress["name"], "passphrase": "nope nope"}).status_code == 403
    assert client.post("/api/packages/import", json={"source": progress["name"], "passphrase": "nope nope"}).status_code == 403
    assert client.post("/api/packages/export", json={"passphrase": "short"}).status_code == 400
    ok = client.post("/api/packages/import", json={"source": progress["name"], "passphrase": PASS, "conflict": "skip"})
    assert app_services.jobs.wait(ok.json()["id"], timeout=60)["status"] == "completed"


# -- hostile packages ----------------------------------------------------------------------

def craft(tmp_path, members: dict, name="evil.fhpack") -> Path:
    path = tmp_path / name
    with path.open("wb") as raw, fhpack.EncryptedWriter(raw, PASS, scrypt=FAST) as enc, tarfile.open(fileobj=enc, mode="w|") as tar:
        for member, data in members.items():
            if isinstance(data, tarfile.TarInfo):
                tar.addfile(data)
                continue
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize("member", ["../escape.txt", "/etc/cron.d/x", "records/../../escape.jsonl", "media/1/../../x.jpg",
                                    "media/1/sub/dir/x.jpg", "records/media.sqlite", "thumbnails/abc.jpg"])
def test_path_traversal_members_are_rejected(app_services, tmp_path, member):
    manifest = json.dumps({"format": "face-hunger-package", "version": 1}).encode()
    package = craft(tmp_path, {"manifest.json": manifest, member: b"pwned"})
    before = snapshot_dir(tmp_path)
    with pytest.raises(fhpack.IntegrityError, match="unexpected entry"):
        app_services.packages.import_package(package, PASS)
    assert snapshot_dir(tmp_path) == before
    assert not (tmp_path / "escape.txt").exists()


def test_links_and_foreign_formats_are_rejected(app_services, tmp_path):
    link = tarfile.TarInfo("media/1/link.jpg")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    with pytest.raises(fhpack.IntegrityError):
        app_services.packages.import_package(craft(tmp_path, {"manifest.json": b"{}", "x": link}), PASS)
    with pytest.raises(fhpack.PackageError, match="Not a Face Hunger package"):
        app_services.packages.import_package(craft(tmp_path, {"manifest.json": b'{"format": "other"}'}, "other.fhpack"), PASS)


# -- constant memory ------------------------------------------------------------------------

SCRIPT = textwrap.dedent("""
    import json, os, resource, sys
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    from backend.config import Config
    from backend.services.container import Services
    from tests.conftest import FakeEngine
    root, megabytes = Path(sys.argv[1]), int(sys.argv[2])
    (root / "frontend").mkdir(parents=True, exist_ok=True)
    lib = root / "lib"; lib.mkdir(exist_ok=True)
    s = Services(Config(data_dir=root / "data", model_dir=root / "models", frontend_dir=root / "frontend",
                        allowed_roots=str(root), watch=False), engine=FakeEngine())
    block = os.urandom(1 << 20)
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'lib')", (str(lib),))
        for i in range(megabytes // 16):
            path = lib / f"big_{{i}}.jpg"
            with path.open("wb") as f:
                for _ in range(16):
                    f.write(block)
            conn.execute("INSERT INTO media(library_id, path, name, kind, size, mtime_ns, status) VALUES (1,?,?,'photo',?,1,'indexed')",
                         (str(path), path.name, 16 << 20))
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = s.packages.export({{"type": "library"}}, "correct horse battery", include_media=True)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024  # bytes on macOS, KiB on Linux
    print(json.dumps({{"base_mb": base * scale / 1e6, "peak_mb": peak * scale / 1e6, "package_mb": result["bytes"] / 1e6}}))
    s.close()
""")


def run_export(tmp_path, megabytes: int) -> dict:
    script = tmp_path / "export_probe.py"
    script.write_text(SCRIPT.format(repo=str(Path(__file__).resolve().parents[1])))
    out = subprocess.run([sys.executable, str(script), str(tmp_path / f"probe-{megabytes}"), str(megabytes)],
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_export_streams_with_constant_memory(tmp_path):
    """A 50 GB export must not need 50 GB of RAM: 10x more data may not grow the peak."""
    small = run_export(tmp_path, 64)
    large = run_export(tmp_path, 640)
    growth_small = small["peak_mb"] - small["base_mb"]
    growth_large = large["peak_mb"] - large["base_mb"]
    print(f"export peak RSS growth: {growth_small:.0f} MB for {small['package_mb']:.0f} MB, "
          f"{growth_large:.0f} MB for {large['package_mb']:.0f} MB")
    assert large["package_mb"] > 630
    assert growth_large < 120, large               # scrypt (32 MB) + one chunk + buffers, not the payload
    assert growth_large - growth_small < 40        # 10x the data, same memory
