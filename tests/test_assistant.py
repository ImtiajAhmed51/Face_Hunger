"""Optional VLM: off by default and never imported; rules fallback; album generation; real-model budget."""

import json
import subprocess
import sys
import textwrap
import time
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from backend.routers.search import parse_query
from tests.helpers.fixtures import FAKE_TEXT, FAKE_VISUAL, add_media, fake_vector
from tests.test_search import FakeTextEncoder

REPO = Path(__file__).resolve().parents[1]
VLM_INSTALLED = (REPO / "models" / "smolvlm2-500m" / "decoder.onnx").is_file()
INSIGHT = Path(__import__("insightface").__file__).parent / "data" / "images"


class FakeVLM:
    """Deterministic stand-in with the LocalVLM surface."""

    loaded = True

    def __init__(self):
        self.calls = []

    def generate(self, prompt, image=None, *, max_new_tokens=64):
        self.calls.append((prompt, image is not None))
        if image is not None:
            return "A dog running on a sandy beach. Extra sentence."
        if "album title" in prompt:
            return '"Summer With Family"\n\n---'
        return "Here are 3 descriptions:\n\n1. A photo of a family smiling together outdoors.\n2. A photo of children playing in a garden.\n3. People eating at a table."

    def status(self):
        return {"loaded": True, "ram_bytes": 1, "device": "fake"}

    def unload(self):
        self.loaded = False


# -- DoD: disabled means not imported, and everything still works -------------------------

def test_vlm_code_is_not_imported_when_disabled(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.config import Config
        from tests.conftest import FakeEngine
        from backend.services.container import Services
        root = {str(tmp_path)!r}
        import os; os.makedirs(root + "/fe", exist_ok=True)
        cfg = Config(data_dir=root + "/data", model_dir={str(REPO / 'models')!r}, frontend_dir=root + "/fe", allowed_roots=root, watch=False)
        app = create_app(cfg, services=Services(cfg, engine=FakeEngine()))
        with TestClient(app) as client:
            h = {{"X-LFS-Request": "1"}}
            assert client.get("/api/vlm").json()["enabled"] is False
            assert client.post("/api/search/parse", json={{"query": "beach 2021"}}, headers=h).status_code == 200
            r = client.post("/api/search/rewrite", json={{"query": "best moments last summer"}}, headers=h).json()
            assert r["source"] == "rules" and r["expansions"] == [] and r["filters"]["date_from"]
            assert client.post("/api/search/hybrid", json={{"text": "beach"}}, headers=h).status_code == 200
            assert client.post("/api/vlm/load", headers=h).status_code == 409
            assert client.post("/api/vlm/caption", json={{}}, headers=h).status_code == 409
        loaded = [m for m in sys.modules if m.startswith("backend.ml.vlm")]
        assert not loaded, loaded
        print("ok")
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr[-1500:]


# -- rules -----------------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("last summer", ("2025-06-01", "2025-08-31")),
    ("this summer", ("2026-06-01", "2026-08-31")),
    ("winter 2023", ("2023-12-01", "2024-02-29")),
    ("spring", ("2026-03-01", "2026-05-31")),
    ("autumn", ("2026-09-01", "2026-11-30")),
    ("winter", ("2025-12-01", "2026-02-28")),  # the current winter has not started on 30 Sep 2026
])
def test_seasons(query, expected, app_services):
    r = parse_query(query, today=date(2026, 9, 30))
    assert (r["date_from"], r["date_to"]) == expected


def test_rewrite_without_model_lists_editable_filters(client, app_services):
    with app_services.db.connect() as conn:
        conn.execute("INSERT INTO people(id, name, face_count) VALUES (1, 'Maya', 3)")
    r = client.post("/api/search/rewrite", json={"query": "best moments with Maya last summer at the beach"}).json()
    assert r["source"] == "rules" and r["filters"]["people"] == [1] and r["people_names"] == ["Maya"]
    assert r["filters"]["date_from"].endswith("-06-01") and "beach" in r["text"] and "best" not in r["text"]


# -- album generation with a fake model -----------------------------------------------------------

@pytest.fixture
def album_library(app_services, monkeypatch):
    s = app_services
    ids = add_media(s.db, 300)
    s.vectors.register(FAKE_TEXT).add([(i, fake_vector(i, FAKE_TEXT.dim)) for i in ids])
    # Visual vectors in 10 tight groups, so diversity has something to avoid.
    visual = s.vectors.register(FAKE_VISUAL)
    visual.add([(i, fake_vector(1000 + i % 10, FAKE_VISUAL.dim)) for i in ids])
    monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())
    for i in ids:
        Image.new("RGB", (64, 48), (i, 90, 90)).save(s.config.data_dir / "thumbnails" / f"media-{i}.jpg")
    return s


def test_generated_album_is_ordinary_editable_and_diverse(client, album_library):
    s = album_library
    client.patch("/api/settings", json={"vlm_enabled": True})
    fake = FakeVLM()
    s.assistant._model = fake
    s.jobs.start()
    job = client.post("/api/albums/generate", json={"prompt": "best moments photo 7", "size": 12, "captions": 3}).json()
    done = s.jobs.wait(job["id"], timeout=60)
    assert done["status"] == "completed", done
    result = json.loads(done["progress"])
    assert result["title"] == "Summer With Family" and result["items"] == 12 and result["source"] == "vlm"
    assert result["expansions"] == ["a family smiling together outdoors", "children playing in a garden", "People eating at a table"]
    album = client.get(f"/api/albums/{result['album_id']}").json()
    items = client.get(f"/api/media?album={result['album_id']}&limit=50").json()["items"]
    assert album["item_count"] == 12 and len(items) == 12
    # Diversity: 12 picks from 10 visual groups cover (almost) all of them instead of one group.
    assert len({i["id"] % 10 for i in items}) >= 8
    # It is a normal album: rename, remove items, undo creation.
    assert client.patch(f"/api/albums/{result['album_id']}", json={"name": "Renamed"}).status_code == 200
    assert client.post(f"/api/albums/{result['album_id']}/items/remove", json={"media_ids": [items[0]["id"]]}).json()["removed"] == 1
    assert client.post(f"/api/audit/{result['audit_id']}/undo").status_code == 200
    assert client.get("/api/albums").json()["items"] == []
    # Captions were stored and feed search.
    captioned = [i for i in items if client.get(f"/api/media/{i['id']}/caption").json()["caption"]]
    assert len(captioned) >= 1
    cap = client.get(f"/api/media/{captioned[0]['id']}/caption").json()
    assert cap["caption"] == "A dog running on a sandy beach." and "dog" in cap["tags"] and "beach" in cap["tags"]
    hits = s.search.run({"text": "dog on the beach", "limit": 20})
    assert "caption" in hits["signals"] and captioned[0]["id"] in [i["id"] for i in hits["items"]]
    # Turning the VLM off releases the model.
    client.patch("/api/settings", json={"vlm_enabled": False})
    assert fake.loaded is False and s.assistant._model is None


def test_album_generation_degrades_to_rules_without_the_model(client, album_library):
    s = album_library
    s.jobs.start()
    job = client.post("/api/albums/generate", json={"prompt": "photo 12 highlights", "size": 5}).json()
    result = json.loads(s.jobs.wait(job["id"], timeout=60)["progress"])
    assert result["source"] == "rules" and result["captioned"] == 0 and result["items"] == 5
    assert result["title"] == "Photo 12 highlights"
    assert client.post("/api/albums/generate", json={"prompt": "  "}).status_code == 400


def test_phrase_cleaning_rejects_garbage():
    from backend.services.assistant import Assistant
    clean = Assistant._clean_phrases
    assert clean("1. A photo of a dog on a beach. It is happy.\n2. \"Children playing\"\n- x\n3. A photo of a dog on a beach.") == \
        ["a dog on a beach", "Children playing"]
    assert clean("Here are some:\n\n" + "word " * 40) == []


# -- DoD with the real model ---------------------------------------------------------------------------

@pytest.mark.ai
@pytest.mark.skipif(not VLM_INSTALLED, reason="run: python scripts/fetch_models.py --only smolvlm2-500m")
def test_real_vlm_album_on_10k_library_within_budget_ram_cap_and_idle_unload(make_config, monkeypatch):
    from backend.ml.vlm import RAM_CAP_BYTES, _rss
    from backend.services.container import Services
    from tests.conftest import FakeEngine

    s = Services(make_config(model_dir=REPO / "models", watch=False, model_idle_seconds=5), engine=FakeEngine())
    try:
        ids = add_media(s.db, 10_000)
        text = s.vectors.register(FAKE_TEXT)
        for start in range(0, 10_000, 2000):
            text.add([(i, fake_vector(i, FAKE_TEXT.dim)) for i in ids[start:start + 2000]])
        monkeypatch.setattr(s.models, "text_encoder", lambda: FakeTextEncoder())
        sample = Image.open(INSIGHT / "t1.jpg").convert("RGB")
        sample.thumbnail((640, 640))
        monkeypatch.setattr(s.assistant, "_image", lambda row: sample)  # every pick has a real image to caption
        with s.db.connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES ('vlm_enabled', 'true') ON CONFLICT(key) DO UPDATE SET value='true'")
        before = _rss()
        started = time.monotonic()
        result = s.assistant.generate_album("best moments at the beach with friends, photo 123", size=30, captions=8)
        elapsed = time.monotonic() - started
        status = s.assistant.status()
        assert result["items"] == 30 and result["source"] == "vlm" and result["title"] and result["captioned"] == 8
        assert status["loaded"] and 0 < status["ram_bytes"] < RAM_CAP_BYTES
        assert _rss() - before < RAM_CAP_BYTES
        assert elapsed < 120, elapsed  # documented budget on CPU
        album = s.db.one("SELECT * FROM albums WHERE id=?", (result["album_id"],))
        assert album["name"] == result["title"]
        print(f"10k album: {elapsed:.1f}s, title {result['title']!r}, {result['captioned']} captions, "
              f"VLM RAM {status['ram_bytes'] / 1e6:.0f} MB, load {status['load_seconds']}s")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and s.assistant.status()["loaded"]:
            time.sleep(0.25)
        assert s.assistant.status()["loaded"] is False  # unloaded after 5 s idle
    finally:
        s.close()
