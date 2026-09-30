"""Quality signals, best-shot formula, resumable scoring job and the 50-group burst evaluation."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from backend import scoring

INSIGHT = Path(__import__("insightface").__file__).parent / "data" / "images"
RAW_DIR = Path(__file__).parent / "fixtures" / "raw"


def photo(seed=0, size=(768, 1024)) -> np.ndarray:
    """A textured natural-ish scene: edges, gradients, mid-grey mean."""
    base = cv2.imread(str(INSIGHT / "t1.jpg"))
    h, w = base.shape[:2]
    rng = np.random.default_rng(seed)
    y, x = int(rng.integers(0, h // 4)), int(rng.integers(0, w // 4))
    return cv2.resize(base[y:y + 3 * h // 4, x:x + 3 * w // 4], size[::-1], interpolation=cv2.INTER_AREA)


# -- signals ----------------------------------------------------------------

def test_sharpness_decreases_with_blur():
    img = photo()
    values = [scoring.image_signals(cv2.GaussianBlur(img, (0, 0), s) if s else img)["sharpness"] for s in (0, 1, 2, 4)]
    assert values == sorted(values, reverse=True) and values[0] - values[-1] > 0.25


def test_exposure_penalises_dark_bright_and_clipping():
    img = photo()
    normal = scoring.image_signals(img)["exposure"]
    dark = scoring.image_signals((img * 0.3).astype(np.uint8))["exposure"]
    bright = scoring.image_signals(np.clip(img.astype(np.float32) * 2.4, 0, 255).astype(np.uint8))["exposure"]
    assert normal > dark and normal > bright and min(normal - dark, normal - bright) > 0.2


def test_noise_does_not_masquerade_as_sharpness_and_jpeg_blocks_are_penalised():
    img = photo()
    rng = np.random.default_rng(2)
    noisy = np.clip(img + rng.normal(0, 14, img.shape), 0, 255).astype(np.uint8)
    clean, dirty = scoring.image_signals(img), scoring.image_signals(noisy)
    assert dirty["sharpness"] <= clean["sharpness"] + 0.05 and dirty["noise"] < clean["noise"]
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 8])
    blocky = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    assert scoring.blockiness(blocky) > 1.5 > scoring.blockiness(img)
    assert scoring.image_signals(blocky)["noise"] < clean["noise"]


def test_noise_estimate_tracks_added_noise():
    img = cv2.GaussianBlur(photo(), (0, 0), 1.0)
    rng = np.random.default_rng(1)
    sigmas = []
    for sigma in (0, 5, 10, 20):
        noisy = np.clip(img.astype(np.float32) + rng.normal(0, sigma, img.shape), 0, 255).astype(np.uint8)
        sigmas.append(scoring.noise_sigma(scoring._gray(noisy)))
    assert sigmas == sorted(sigmas)
    assert abs(sigmas[3] - sigmas[0] - 20) < 8  # roughly the added sigma
    assert scoring.noise(scoring._gray(img)) > 0.8


def eye_patch(open_: bool) -> np.ndarray:
    patch = np.full((28, 44), 190, np.uint8)
    if open_:
        cv2.circle(patch, (22, 14), 8, 40, -1)  # iris/pupil
    else:
        cv2.line(patch, (6, 15), (38, 16), 45, 2)  # closed lid / lashes
    return patch


def test_eye_openness_separates_open_and_closed():
    assert scoring.eye_openness(eye_patch(True)) > 0.6
    assert scoring.eye_openness(eye_patch(False)) < 0.15
    assert scoring.eye_openness(np.full((20, 30), 128, np.uint8)) is None  # flat: unusable


def face_kps(mouth_width: float) -> np.ndarray:
    return np.array([[40, 50], [80, 50], [60, 70], [60 - mouth_width / 2, 90], [60 + mouth_width / 2, 90]], float)


def test_smile_from_mouth_geometry():
    neutral, broad = scoring.smile_from_kps(face_kps(32)), scoring.smile_from_kps(face_kps(42))
    assert neutral == 0.0 and broad > 0.7


def test_eyes_open_from_landmarks_uses_the_worst_eye():
    canvas = np.full((120, 120), 190, np.uint8)
    open_eye, closed_eye = eye_patch(True), eye_patch(False)
    canvas[36:64, 18:62] = open_eye
    canvas[36:64, 58:102] = closed_eye
    kps = np.array([[40, 50], [80, 50], [60, 70], [48, 90], [72, 90]], float)
    assert scoring.eyes_open_from_kps(canvas, kps) < 0.5
    group = scoring.face_signals(cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR), [{"quality": 0.8, "landmarks": kps}])
    assert group["faces"] == 1 and group["face_quality"] == 0.8


def test_composite_renormalises_missing_signals():
    score, breakdown = scoring.composite({"sharpness": 1.0, "exposure": 1.0, "noise": 1.0})
    assert score == pytest.approx(1.0) and set(breakdown) == {"sharpness", "exposure", "noise"}
    full, _ = scoring.composite({k: 0.5 for k in scoring.WEIGHTS})
    assert full == pytest.approx(0.5)
    assert scoring.composite({}) == (0.0, {})
    assert sum(scoring.WEIGHTS.values()) == pytest.approx(1.0)


def test_aesthetic_head_roundtrip(tmp_path):
    w = np.zeros(8, np.float32)
    w[0] = 1
    head = scoring.AestheticHead(w, bias=-0.1, scale=20, model_key="m@1:8", version="t")
    head.save(tmp_path / "h.json")
    loaded = scoring.AestheticHead.load(tmp_path / "h.json")
    good, bad = np.eye(8, dtype=np.float32)[0], np.eye(8, dtype=np.float32)[1]
    assert loaded(good) > 0.99 and loaded(bad) < 0.2
    assert scoring.AestheticHead.load(tmp_path / "missing.json") is None


# -- job, versioning, API -----------------------------------------------------

@pytest.fixture
def scored_library(app_services, tmp_path):
    s = app_services
    lib = tmp_path / "lib"
    lib.mkdir()
    with s.db.connect() as conn:
        conn.execute("INSERT INTO libraries(id, path, name) VALUES (1, ?, 'lib')", (str(lib),))
    variants = {"sharp": photo(3), "blurry": cv2.GaussianBlur(photo(3), (0, 0), 3), "dark": (photo(3) * 0.25).astype(np.uint8)}
    for i, (name, img) in enumerate(variants.items(), start=1):
        path = lib / f"{name}.jpg"
        cv2.imwrite(str(path), img)
        with s.db.connect() as conn:
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status, width, height, indexed_at)"
                         " VALUES (?, 1, ?, ?, 'photo', 1, 1, 'indexed', ?, ?, '2020-01-01')",
                         (i, str(path), path.name, img.shape[1], img.shape[0]))
    s.jobs.start()
    return s, lib


def run_job(s):
    job = s.jobs.enqueue("quality_scoring", dedupe_key="quality_scoring")
    done = s.jobs.wait(job["id"], timeout=60)
    assert done["status"] == "completed", done
    return json.loads(done["progress"])


def test_scoring_job_is_incremental_and_formula_versioned(scored_library, client):
    s, lib = scored_library
    first = run_job(s)
    assert first["scored"] == 3 and first["failed"] == 0
    ranked = [i["name"] for i in client.get("/api/media?sort=best").json()["items"]]
    assert ranked[0] == "sharp.jpg"
    detail = client.get("/api/media/1/quality").json()
    assert detail["score"] > 0 and set(detail["breakdown"]) == {"sharpness", "exposure", "noise"}
    assert detail["signals"]["faces"] == 0 and detail["weights"] == scoring.WEIGHTS
    # Incremental: nothing to do on a re-run; one new file -> exactly one computation.
    assert run_job(s)["scored"] == 0
    cv2.imwrite(str(lib / "new.jpg"), photo(9))
    with s.db.connect() as conn:
        conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status, width, height)"
                     " VALUES (4, 1, ?, 'new.jpg', 'photo', 1, 1, 'indexed', 1024, 768)", (str(lib / "new.jpg"),))
    assert run_job(s)["scored"] == 1
    # A new formula re-ranks from stored signals without touching them.
    stamps = {r["media_id"]: r["computed_at"] for r in s.db.all("SELECT media_id, computed_at FROM quality_signals")}
    assert s.quality.rescore(formula_version=99, weights={"exposure": 1.0}) == 4
    best_exposed = s.db.one("SELECT media_id FROM quality_scores WHERE formula_version=99 ORDER BY score DESC LIMIT 1")
    assert best_exposed["media_id"] != 3  # the dark frame never wins on exposure
    assert stamps == {r["media_id"]: r["computed_at"] for r in s.db.all("SELECT media_id, computed_at FROM quality_signals")}
    overview = client.get("/api/quality").json()
    assert overview["coverage"]["scored"] == 4 and overview["aesthetic_head"] is False


def test_scoring_job_resumes_after_cancel(scored_library):
    s, lib = scored_library
    for i in range(5, 45):
        cv2.imwrite(str(lib / f"x{i}.jpg"), photo(i))
        with s.db.connect() as conn:
            conn.execute("INSERT INTO media(id, library_id, path, name, kind, size, mtime_ns, status, width, height)"
                         " VALUES (?, 1, ?, ?, 'photo', 1, 1, 'indexed', 1024, 768)", (i, str(lib / f"x{i}.jpg"), f"x{i}.jpg"))
    job = s.jobs.enqueue("quality_scoring")
    s.jobs.wait(job["id"], statuses=("running",))
    s.jobs.control(job["id"], "cancel")
    s.jobs.wait(job["id"])
    partial = s.db.one("SELECT COUNT(*) c FROM quality_signals")["c"]
    rest = run_job(s)
    assert partial + rest["scored"] == 43  # no media computed twice
    assert s.db.one("SELECT COUNT(*) c FROM quality_scores")["c"] == 43


# -- burst evaluation ---------------------------------------------------------

def _sources() -> list[np.ndarray]:
    """Burst sources must have a genuinely good 'best' frame: >= 400 px and not already
    JPEG-blocky (a degraded copy of a degraded image has no well-defined best)."""
    images = [cv2.imread(str(p)) for p in sorted(INSIGHT.glob("*.jpg")) + sorted(INSIGHT.glob("*.png"))]
    if RAW_DIR.is_dir():
        from backend import imaging
        images += [imaging.load_bgr(p, max_side=1600) for p in sorted(RAW_DIR.glob("sample.*"))]
    return [i for i in images if i is not None and min(i.shape[:2]) >= 400 and scoring.blockiness(i) < 1.3]


def _degrade(img: np.ndarray, how: str, rng, mild: bool) -> np.ndarray:
    f = 0.45 if mild else 1.0
    if how == "blur":
        return cv2.GaussianBlur(img, (0, 0), 0.6 + f * rng.uniform(0.8, 1.8))
    if how == "motion":
        k = int(3 + f * rng.integers(4, 12)) | 1
        kernel = np.zeros((k, k), np.float32)
        kernel[k // 2, :] = 1.0 / k
        return cv2.filter2D(img, -1, kernel)
    if how == "noise":
        return np.clip(img + rng.normal(0, 2 + f * rng.uniform(6, 16), img.shape), 0, 255).astype(np.uint8)
    if how == "dark":
        return np.clip(img * (1 - f * rng.uniform(0.5, 0.7)), 0, 255).astype(np.uint8)
    if how == "bright":
        return np.clip(img.astype(np.float32) * (1 + f * rng.uniform(0.9, 1.5)) + 10, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(40 - f * 30)])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def burst_accuracy(groups: int = 50, mild: bool = False, seed: int = 11) -> float:
    """Each group: one clean frame + 4 degraded siblings; the clean frame is the labelled best."""
    rng = np.random.default_rng(seed)
    sources = _sources()
    hits = 0
    for g in range(groups):
        src = sources[g % len(sources)]
        h, w = src.shape[:2]
        ch, cw = int(h * rng.uniform(0.6, 0.9)), int(w * rng.uniform(0.6, 0.9))
        y, x = int(rng.integers(0, h - ch + 1)), int(rng.integers(0, w - cw + 1))
        clean = src[y:y + ch, x:x + cw]
        kinds = rng.choice(["blur", "motion", "noise", "dark", "bright", "jpeg"], size=4, replace=False)
        frames = [clean] + [_degrade(clean, k, rng, mild) for k in kinds]
        order = rng.permutation(len(frames))
        scores = [scoring.composite(scoring.image_signals(frames[i]))[0] for i in order]
        hits += int(order[int(np.argmax(scores))] == 0)
    return hits / groups


@pytest.mark.skipif(len(_sources()) < 3, reason="needs the RAW samples (scripts/fetch_test_fixtures.py) as clean sources")
def test_best_shot_matches_labelled_best_in_burst_groups():
    strong = burst_accuracy(50, mild=False)
    mild = burst_accuracy(50, mild=True, seed=12)
    print(f"burst accuracy: typical degradations {strong:.0%}, mild degradations {mild:.0%}")
    assert strong >= 0.70
    assert mild >= 0.5


def test_aesthetic_head_is_built_from_prompt_directions():
    from scripts.build_aesthetic_head import build_head

    class Encoder:
        class spec:
            key = "fake@1:4"

        def embed_texts(self, texts):
            good = [t for t in texts if "blurry" not in t and "bad" not in t]
            return np.array([[1.0, 0.1, 0, 0] if t in good else [0.0, 0.1, 1.0, 0] for t in texts], np.float32)

    head = build_head(Encoder(), positive=["nice"], negative=["blurry"])
    assert head.model_key == "fake@1:4"
    assert head(np.array([1, 0, 0, 0], np.float32)) > 0.99 > 0.01 > head(np.array([0, 0, 1, 0], np.float32))
