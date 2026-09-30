"""Format pipeline: every still format decodes upright, indexes end to end, and RAW previews are fast."""

import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from backend import imaging

RAW_DIR = Path(__file__).parent / "fixtures" / "raw"
RAW_EXTS = ("cr2", "cr3", "nef", "arw", "dng", "raf", "orf")
have_raw = pytest.mark.skipif(not all((RAW_DIR / f"sample.{e}").is_file() for e in RAW_EXTS),
                              reason="run scripts/fetch_test_fixtures.py for RAW samples")
FACE_PHOTO = Path(__import__("insightface").__file__).parent / "data" / "images" / "t1.jpg"
BUFFALO = Path(__file__).resolve().parents[1] / "models" / "buffalo_l"

# PIL's exif_transpose method per orientation, and its inverse (to build the stored pixels).
TRANSPOSE = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
             6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}
INVERSE = {**TRANSPOSE, 6: "ROTATE_90", 8: "ROTATE_270"}


def reference() -> Image.Image:
    """A 60x40 image whose corners are all different colours (so any rotation/flip shows)."""
    img = Image.new("RGB", (60, 40), (40, 40, 40))
    for (x, y), colour in {(0, 0): (255, 0, 0), (30, 0): (0, 255, 0), (0, 20): (0, 0, 255), (30, 20): (255, 255, 0)}.items():
        img.paste(colour, (x, y, x + 30, y + 20))
    return img


def stored_for(orientation: int) -> Image.Image:
    img = reference()
    return img if orientation == 1 else img.transpose(getattr(Image.Transpose, INVERSE[orientation]))


def save(img: Image.Image, path: Path, orientation: int) -> None:
    exif = Image.Exif()
    exif[0x0112] = orientation
    fmt = {".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".tif": "TIFF", ".avif": "AVIF", ".heic": "HEIF"}[path.suffix]
    kwargs = {"quality": 95} if fmt in ("JPEG", "WEBP", "AVIF", "HEIF") else {}
    if fmt == "WEBP":
        kwargs["lossless"] = True
    img.save(path, format=fmt, exif=exif.tobytes(), **kwargs)


def corners(bgr: np.ndarray) -> list[tuple[int, int, int]]:
    h, w = bgr.shape[:2]
    pts = [(h // 4, w // 4), (h // 4, 3 * w // 4), (3 * h // 4, w // 4), (3 * h // 4, 3 * w // 4)]
    return [tuple(int(c) for c in bgr[y, x, ::-1]) for y, x in pts]


EXPECTED = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


imaging._pil()  # register HEIF/AVIF plugins before probing encoders


def _pillow_encodes_avif() -> bool:
    import io
    try:
        Image.new("RGB", (8, 8)).save(io.BytesIO(), format="AVIF")
        return True
    except Exception:
        return False


def make_avif(source: Image.Image, target: Path) -> None:
    """AVIF via Pillow when it has an encoder, else ffmpeg's SVT-AV1 (both available offline)."""
    if _pillow_encodes_avif():
        source.save(target, format="AVIF", quality=90)
        return
    import subprocess
    png = target.with_suffix(".src.png")
    source.save(png)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(png), "-c:v", "libsvtav1",
                    "-frames:v", "1", "-pix_fmt", "yuv420p", str(target)], check=True, capture_output=True)
    png.unlink()


AVIF_EXIF = pytest.param(".avif", marks=pytest.mark.skipif(not _pillow_encodes_avif(),
                         reason="this Pillow build decodes AVIF but has no encoder to write EXIF test files"))


@pytest.mark.parametrize("suffix", [".jpg", ".png", ".webp", ".tif", AVIF_EXIF, ".heic"])
@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_exif_orientations_decode_upright(tmp_path, suffix, orientation):
    imaging._pil()  # registers HEIF
    path = tmp_path / f"o{orientation}{suffix}"
    save(stored_for(orientation), path, orientation)
    bgr = imaging.load_bgr(path)
    assert bgr.shape[:2] == (40, 60), (suffix, orientation, bgr.shape)
    for got, want in zip(corners(bgr), EXPECTED):
        assert np.abs(np.array(got) - want).max() < 60, (suffix, orientation, corners(bgr))
    assert imaging.dimensions(path) == (60, 40)
    # Reduced (draft) decodes stay upright too.
    assert imaging.open_image(path, max_side=30).size == (30, 20)


def test_avif_decodes(tmp_path):
    ref = reference().resize((120, 80), Image.Resampling.NEAREST)
    make_avif(ref, tmp_path / "ref.avif")
    bgr = imaging.load_bgr(tmp_path / "ref.avif")
    assert bgr.shape[:2] == (80, 120)
    for got, want in zip(corners(bgr), EXPECTED):
        assert np.abs(np.array(got) - want).max() < 70, corners(bgr)


def test_thumbnails_and_face_crops_use_upright_pixels(tmp_path):
    from backend.media_processing import thumbnail_bytes
    path = tmp_path / "o6.jpg"
    save(stored_for(6), path, 6)
    thumb = Image.open(__import__("io").BytesIO(thumbnail_bytes(imaging.load_bgr(path), max_size=64)))
    assert thumb.size == (60, 40)
    # A crop of the top-left quarter in upright coordinates is red.
    crop = Image.open(__import__("io").BytesIO(thumbnail_bytes(imaging.load_bgr(path), bbox=(2, 2, 20, 12), max_size=64)))
    assert np.asarray(crop.convert("RGB"))[4, 4].tolist()[0] > 200


@have_raw
@pytest.mark.parametrize("ext", RAW_EXTS)
def test_raw_formats_decode_with_metadata(ext):
    path = RAW_DIR / f"sample.{ext}"
    bgr = imaging.load_bgr(path)
    assert bgr.ndim == 3 and min(bgr.shape[:2]) >= 400
    assert max(bgr.shape[:2]) <= imaging.RAW_INDEX_MAX * 1.1
    tags = imaging.exif(path)
    assert tags.get(36867) or tags.get(306), f"{ext}: no capture date"
    w, h = imaging.dimensions(path)
    assert (w > h) == (bgr.shape[1] > bgr.shape[0]), f"{ext}: orientation disagrees with sensor size"


@have_raw
def test_45mp_raw_preview_under_300ms_after_index(client, app_services, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(RAW_DIR / "sample.nef", lib / "z7.nef")  # Nikon Z 7, 45.7 MP
    lid = client.post("/api/libraries", json={"path": str(lib)}).json()["id"]
    app_services.jobs.stop()
    app_services.watcher.stop()
    job = client.post("/api/index", json={"library_id": lid}).json()
    deadline = time.monotonic() + 60
    while app_services.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    media = app_services.db.one("SELECT * FROM media WHERE name='z7.nef'")
    assert media["status"] == "indexed" and (media["width"], media["height"]) == (4128, 2752)
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        r = client.get(f"/api/media/{media['id']}/preview")
        timings.append((time.perf_counter() - started) * 1000)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert Image.open(__import__("io").BytesIO(r.content)).size == (2560, 1707)
    assert max(timings) < 300, timings
    # Full-quality demosaic is on demand only, and cached separately.
    assert not imaging.preview_path(app_services.config.data_dir, media["id"], full=True).exists()
    print(f"45MP NEF preview after index: max {max(timings):.1f} ms")


def build_fixture_library(root: Path, with_raw: bool) -> list[str]:
    root.mkdir()
    imaging._pil()
    source = Image.open(FACE_PHOTO).convert("RGB")
    names = []
    for suffix, fmt in ((".jpg", "JPEG"), (".png", "PNG"), (".webp", "WEBP"), (".heic", "HEIF"),
                        (".avif", "AVIF"), (".tif", "TIFF")):
        name = f"group{suffix}"
        if fmt == "AVIF":
            make_avif(source, root / name)
        else:
            source.save(root / name, format=fmt, **({"quality": 92} if fmt in ("JPEG", "WEBP", "HEIF") else {}))
        names.append(name)
    if with_raw:
        for ext in RAW_EXTS:
            shutil.copy(RAW_DIR / f"sample.{ext}", root / f"camera.{ext}")
            names.append(f"camera.{ext}")
    return names


def mean_colour_embedder():
    from backend.ml.models import MediaEmbedder
    from tests.helpers.fixtures import FAKE_VISUAL

    def embed(images):
        out = []
        for im in images:
            small = np.asarray(Image.fromarray(im[:, :, ::-1]).resize((4, 8)), dtype=np.float32).reshape(-1)[:32] + 1
            out.append(small / np.linalg.norm(small))
        return np.stack(out)

    return MediaEmbedder(FAKE_VISUAL, embed)


@pytest.mark.ai
@pytest.mark.skipif(not (BUFFALO / "det_10g.onnx").is_file(), reason="buffalo_l models not installed")
def test_fixture_library_of_every_format_indexes_end_to_end(make_config, tmp_path):
    """Real face engine: thumbnails, faces and embeddings for one file of each format."""
    from backend.services.container import Services
    from backend.vectors.spaces import run_backfill

    with_raw = all((RAW_DIR / f"sample.{e}").is_file() for e in RAW_EXTS)
    lib = tmp_path / "lib"
    names = build_fixture_library(lib, with_raw)
    s = Services(make_config(model_dir=BUFFALO.parent, watch=False))
    try:
        with s.db.connect() as conn:
            lid = conn.execute("INSERT INTO libraries(path, name) VALUES (?, 'fx')", (str(lib.resolve()),)).lastrowid
        job = s.worker.start(lid)
        s.worker._thread.join(600)
        done = s.db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
        assert done["status"] == "completed", done["error"]
        rows = {r["name"]: r for r in s.db.all("SELECT * FROM media")}
        assert set(rows) == set(names)
        for name, row in rows.items():
            assert row["status"] == "indexed", (name, row["error"])
            assert (s.config.data_dir / "thumbnails" / f"media-{row['id']}.jpg").is_file(), name
            assert row["content_hash"] and row["phash"], name
        faces = {r["name"]: r["c"] for r in s.db.all(
            "SELECT m.name, COUNT(f.id) c FROM media m LEFT JOIN faces f ON f.media_id=m.id GROUP BY m.id")}
        for name in names:
            if name.startswith("group"):
                assert faces[name] >= 5, (name, faces[name])  # t1.jpg shows 6 people
        # Face embeddings exist and all read back with valid checksums.
        for face in s.db.all("SELECT embedding_offset, embedding_sha FROM faces"):
            s.store.read(face["embedding_offset"], face["embedding_sha"])
        # Media embeddings: every format decodes through the embedding pipeline.
        embedder = mean_colour_embedder()
        s.extra_embedders[embedder.spec.key] = embedder
        space = s.vectors.register(embedder.spec)
        result = run_backfill(space, embedder, batch_size=4)
        assert result["failed"] == 0 and space.count() == len(names)
        # Non-web formats got a cached viewer preview at index time.
        for name, row in rows.items():
            cached = imaging.preview_path(s.config.data_dir, row["id"]).is_file()
            assert cached == (Path(name).suffix.lower() in imaging.NON_WEB_SUFFIXES), name
    finally:
        s.close()
